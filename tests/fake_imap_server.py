from __future__ import annotations

import imaplib
import socket
import ssl
from collections import deque
from dataclasses import dataclass, field


class FakeImapFile:
    def __init__(self, sock: FakeImapSocket) -> None:
        self._sock = sock

    def readline(self, _limit: int = -1) -> bytes:
        if self._sock.read_error is not None:
            raise self._sock.read_error
        if not self._sock.responses:
            return b""
        return self._sock.responses.popleft()

    def close(self) -> None:
        pass


def _parse_wire_line(line: str) -> tuple[str, str, list[tuple[str, bool]]]:
    pieces = line.split(" ", 2)
    if len(pieces) < 2 or not pieces[0] or not pieces[1]:
        raise AssertionError(f"invalid IMAP command line: {line!r}")
    tag, command = pieces[:2]
    remainder = pieces[2] if len(pieces) == 3 else ""
    arguments: list[tuple[str, bool]] = []
    index = 0
    while index < len(remainder):
        while index < len(remainder) and remainder[index] == " ":
            index += 1
        if index == len(remainder):
            break
        quoted = remainder[index] == '"'
        value: list[str] = []
        if quoted:
            index += 1
            while index < len(remainder):
                character = remainder[index]
                index += 1
                if character == '"':
                    break
                if character == "\\":
                    if index == len(remainder) or remainder[index] not in {'"', "\\"}:
                        raise AssertionError(f"invalid IMAP quoted escape: {line!r}")
                    character = remainder[index]
                    index += 1
                value.append(character)
            else:
                raise AssertionError(f"unterminated IMAP quoted string: {line!r}")
            if index < len(remainder) and remainder[index] != " ":
                raise AssertionError(f"invalid IMAP quoted boundary: {line!r}")
        else:
            start = index
            while index < len(remainder) and remainder[index] != " ":
                index += 1
            value.extend(remainder[start:index])
        arguments.append(("".join(value), quoted))
    return tag, command.upper(), arguments


def _require_quoted_arguments(
    command: str,
    arguments: list[tuple[str, bool]],
    *indexes: int,
) -> None:
    if any(index >= len(arguments) or not arguments[index][1] for index in indexes):
        raise AssertionError(f"{command} mailbox arguments must be quoted: {arguments!r}")


@dataclass
class FakeImapSocket:
    server: FakeImapServer
    peer_host: str
    read_error: BaseException | None = None
    responses: deque[bytes] = field(default_factory=lambda: deque([b"* OK fake IMAP ready\r\n"]))
    timeout_values: list[float] = field(default_factory=list)
    closed: bool = False
    tls_active: bool = False

    def getpeername(self) -> tuple[str, int]:
        return self.peer_host, self.server.port

    def makefile(self, _mode: str) -> FakeImapFile:
        return FakeImapFile(self)

    def recv(self, _size: int) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        if not self.responses:
            return b""
        return self.responses.popleft()

    def settimeout(self, value: float) -> None:
        self.timeout_values.append(value)

    def sendall(self, data: bytes) -> None:
        line = data.decode("ascii", errors="replace").rstrip("\r\n")
        self.server.wire_lines.append(line)
        tag, command, arguments = _parse_wire_line(line)
        self.server.commands.append(command)
        self.server.parsed_commands.append((command, tuple(arguments)))

        if command == "CAPABILITY":
            capabilities = "IMAP4rev1 STARTTLS UIDPLUS MOVE"
            self.responses.extend(
                [
                    f"* CAPABILITY {capabilities}\r\n".encode(),
                    f"{tag} OK CAPABILITY completed\r\n".encode(),
                ]
            )
        elif command == "STARTTLS":
            self.responses.append(f"{tag} OK Begin TLS\r\n".encode())
        elif command == "LOGIN":
            login_error = self.server.login_errors.get(self.peer_host)
            if login_error is not None:
                raise login_error
            if self.server.require_tls_before_login and not self.tls_active:
                self.responses.append(f"{tag} NO TLS required\r\n".encode())
            elif self.server.reject_login:
                self.responses.append(f"{tag} NO invalid credentials\r\n".encode())
            else:
                self.responses.append(f"{tag} OK LOGIN completed\r\n".encode())
        elif command == "LIST":
            if len(arguments) != 2:
                raise AssertionError(f"LIST requires two arguments: {arguments!r}")
            _require_quoted_arguments(command, arguments, 0, 1)
            rows = self.server.list_rows or [b'* LIST (\\HasNoChildren) "/" "Projekti/&AN8-"\r\n']
            self.responses.extend([*rows, f"{tag} OK LIST completed\r\n".encode()])
        elif command in {"EXAMINE", "SELECT"}:
            if len(arguments) != 1:
                raise AssertionError(f"{command} requires one argument: {arguments!r}")
            _require_quoted_arguments(command, arguments, 0)
            access_mode = "READ-ONLY" if command == "EXAMINE" else "READ-WRITE"
            self.responses.extend(
                [
                    b"* 0 EXISTS\r\n",
                    b"* 0 RECENT\r\n",
                    b"* OK [UIDVALIDITY 7] stable UIDs\r\n",
                    f"{tag} OK [{access_mode}] {command} completed\r\n".encode(),
                ]
            )
        elif command == "UID" and arguments and arguments[0][0] in {"MOVE", "COPY"}:
            if len(arguments) != 3:
                raise AssertionError(f"UID {arguments[0][0]} requires three arguments")
            _require_quoted_arguments(f"UID {arguments[0][0]}", arguments, 2)
            self.responses.append(f"{tag} OK UID {arguments[0][0]} completed\r\n".encode())
        elif command == "LOGOUT":
            self.responses.extend(
                [b"* BYE signing off\r\n", f"{tag} OK LOGOUT completed\r\n".encode()]
            )
        else:
            self.responses.append(f"{tag} BAD unsupported command\r\n".encode())

    def shutdown(self, _how: int = socket.SHUT_RDWR) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeTlsContext:
    server: FakeImapServer
    minimum_version: ssl.TLSVersion | None = None

    def wrap_socket(
        self,
        sock: FakeImapSocket,
        *,
        server_hostname: str | None = None,
    ) -> FakeImapSocket:
        self.server.server_names.append(server_hostname)
        if self.server.certificate_error:
            raise ssl.SSLCertVerificationError("fake certificate verification failed")
        sock.tls_active = True
        return sock


@dataclass
class FakeImapServer:
    port: int = 993
    peer_host: str | None = None
    certificate_error: bool = False
    reject_login: bool = False
    require_tls_before_login: bool = False
    dial_errors: dict[str, BaseException] = field(default_factory=dict)
    login_errors: dict[str, imaplib.IMAP4.abort] = field(default_factory=dict)
    read_error: BaseException | None = None
    list_rows: list[bytes] = field(default_factory=list)
    dialed_hosts: list[str] = field(default_factory=list)
    dial_timeouts: list[float] = field(default_factory=list)
    server_names: list[str | None] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    wire_lines: list[str] = field(default_factory=list)
    parsed_commands: list[tuple[str, tuple[tuple[str, bool], ...]]] = field(default_factory=list)
    sockets: list[FakeImapSocket] = field(default_factory=list)
    contexts: list[FakeTlsContext] = field(default_factory=list)

    def dial(self, host: str, port: int, timeout: float) -> FakeImapSocket:
        self.dialed_hosts.append(host)
        self.dial_timeouts.append(timeout)
        error = self.dial_errors.get(host)
        if error is not None:
            raise error
        sock = FakeImapSocket(
            server=self,
            peer_host=self.peer_host or host,
            read_error=self.read_error,
        )
        self.sockets.append(sock)
        return sock

    def tls_context(self) -> FakeTlsContext:
        context = FakeTlsContext(self)
        self.contexts.append(context)
        return context
