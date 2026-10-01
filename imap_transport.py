from __future__ import annotations

import base64
import binascii
import imaplib
import ipaddress
import re
import socket
import ssl
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from flowsteward_extension_sdk import PinnedPeerError, resolve_pinned_ips
from imap_config import ImapConnectionConfig
from imap_errors import ImapExtensionError

_MESSAGE_THE_IMAP_SERVER_RETURNED_INVALID_DATA = "The IMAP server returned invalid data"
_MESSAGE_INVALID_IMAP_SEARCH_ITEM = "invalid IMAP search item"

MessageSet = int | Iterable[int]
Dialer = Callable[[str, int, float], socket.socket]
Resolver = Callable[..., Sequence[str]]
TlsContextFactory = Callable[[], ssl.SSLContext]

_LIST_RESPONSE = re.compile(
    rb"^\((?P<flags>[^)]*)\) (?P<delimiter>NIL|\"(?:[^\"\\]|\\.)*\") (?P<name>.+)$"
)
_FETCH_UID = re.compile(rb"\bUID (?P<uid>\d+)\b", re.IGNORECASE)
_FETCH_SIZE = re.compile(rb"\bRFC822\.SIZE (?P<size>\d+)\b", re.IGNORECASE)
_FETCH_FLAGS = re.compile(rb"\bFLAGS \((?P<flags>[^)]*)\)", re.IGNORECASE)
_FETCH_BODYSTRUCTURE = re.compile(rb"\bBODYSTRUCTURE\s+", re.IGNORECASE)
_FETCH_LITERAL_SELECTOR = re.compile(
    rb"(?P<selector>BODY(?:\.PEEK)?\[[^]]+\])(?:<\d+>)?\s*\{\d+\}$", re.IGNORECASE
)
MAX_BODYSTRUCTURE_RESPONSE_BYTES = 512 * 1024
MAX_BODYSTRUCTURE_DEPTH = 32
MAX_BODYSTRUCTURE_NODES = 2_048
MAX_BODYSTRUCTURE_TOKENS = 10_000
MAX_FETCH_LITERAL_BYTES = 64 * 1024
MAX_FETCH_RESPONSE_LINE_BYTES = MAX_BODYSTRUCTURE_RESPONSE_BYTES + MAX_FETCH_LITERAL_BYTES
MAX_MAILBOX_LIST_ROWS = 1_000
MAX_MAILBOX_LIST_RESPONSE_BYTES = 1024 * 1024


class _MailboxListLimitExceeded(imaplib.IMAP4.abort):
    pass


@dataclass
class _ImapParseBudget:
    nodes: int = 0
    tokens: int = 0

    def consume(self, *, container: bool = False) -> None:
        self.tokens += 1
        if container:
            self.nodes += 1
        if self.tokens > MAX_BODYSTRUCTURE_TOKENS or self.nodes > MAX_BODYSTRUCTURE_NODES:
            raise ValueError("IMAP value exceeds parser budget")


class MailboxClient(Protocol):
    def capabilities(self) -> frozenset[bytes]: ...

    def list_folders(
        self, directory: str = "", pattern: str = "*"
    ) -> list[tuple[tuple[bytes, ...], bytes | None, str]]: ...

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, object]: ...

    def search(
        self, criteria: Sequence[object] | str = "ALL", charset: str | None = None
    ) -> list[int]: ...

    def fetch(
        self,
        messages: MessageSet,
        data: Sequence[str] | str,
        modifiers: Sequence[str] | None = None,
    ) -> dict[int, dict[bytes, object]]: ...

    def add_flags(
        self, messages: MessageSet, flags: Sequence[str], silent: bool = False
    ) -> dict[int, tuple[bytes, ...]] | None: ...

    def remove_flags(
        self, messages: MessageSet, flags: Sequence[str], silent: bool = False
    ) -> dict[int, tuple[bytes, ...]] | None: ...

    def move(self, messages: MessageSet, folder: str) -> bytes: ...

    def copy(self, messages: MessageSet, folder: str) -> bytes: ...

    def uid_expunge(self, messages: MessageSet) -> bytes: ...

    def logout(self) -> None: ...


class _PinnedIMAP4(imaplib.IMAP4):
    def __init__(
        self,
        original_host: str,
        port: int,
        *,
        pin: str,
        dialer: Dialer,
        connect_timeout: float,
        read_timeout: float,
        implicit_tls: bool,
        tls_context: ssl.SSLContext,
    ) -> None:
        self._pin = pin
        self._dialer = dialer
        self._read_timeout = read_timeout
        self._implicit_tls = implicit_tls
        self._tls_context = tls_context
        self._list_budget_active = False
        self._list_limit_exceeded = False
        self._list_response_rows = 0
        self._list_response_bytes = 0
        try:
            super().__init__(original_host, port, timeout=connect_timeout)
        except Exception:
            self._force_close()
            raise

    def open(self, host: str = "", port: int = 143, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        raw_socket: socket.socket | None = None
        try:
            raw_socket = self._dialer(self._pin, port, float(timeout or 0))
            _assert_selected_peer(raw_socket, self._pin)
            if self._implicit_tls:
                raw_socket = self._tls_context.wrap_socket(
                    raw_socket,
                    server_hostname=host,
                )
            raw_socket.settimeout(self._read_timeout)
            self.sock = raw_socket
            _set_imap_file(self, raw_socket.makefile("rb"))
        except Exception:
            if raw_socket is not None:
                raw_socket.close()
            raise

    def read(self, size: int) -> bytes:
        if size > MAX_FETCH_LITERAL_BYTES:
            raise self.abort("IMAP literal exceeds the bounded fetch size")
        return super().read(size)

    def readline(self) -> bytes:
        line = _active_imap_file(self).readline(MAX_FETCH_RESPONSE_LINE_BYTES + 1)
        if len(line) > MAX_FETCH_RESPONSE_LINE_BYTES:
            raise self.error("IMAP response line exceeds the bounded size")
        return line

    def list(self, directory: str = '""', pattern: str = "*") -> tuple[str, list[Any]]:
        self._list_budget_active = True
        self._list_limit_exceeded = False
        self._list_response_rows = 0
        self._list_response_bytes = 0
        try:
            return super().list(directory, pattern)
        finally:
            self._list_budget_active = False

    def _append_untagged(self, typ: str, dat: Any) -> None:
        if self._list_budget_active and typ == "LIST":
            self._list_response_rows += 1
            self._list_response_bytes += len(b"* LIST \r\n") + _imap_response_size(dat)
            if (
                self._list_response_rows > MAX_MAILBOX_LIST_ROWS
                or self._list_response_bytes > MAX_MAILBOX_LIST_RESPONSE_BYTES
            ):
                self._list_limit_exceeded = True
                raise _MailboxListLimitExceeded("IMAP mailbox list exceeds its bounded size")
        super()._append_untagged(typ, dat)

    def _force_close(self) -> None:
        file_handles = {getattr(self, "_file", None)}
        if not isinstance(vars(imaplib.IMAP4).get("file"), property):
            file_handles.add(getattr(self, "file", None))
        for file_handle in file_handles - {None}:
            with suppress(OSError):
                file_handle.close()
        sock = getattr(self, "sock", None)
        if sock is not None:
            with suppress(OSError):
                sock.close()


class _StdlibMailboxClient:
    def __init__(self, connection: _PinnedIMAP4) -> None:
        self._connection = connection
        self._logged_out = False

    def capabilities(self) -> frozenset[bytes]:
        return frozenset(capability.encode("ascii") for capability in self._connection.capabilities)

    def list_folders(
        self, directory: str = "", pattern: str = "*"
    ) -> list[tuple[tuple[bytes, ...], bytes | None, str]]:
        try:
            status, rows = self._connection.list(
                _quote_mailbox_name(directory),
                _quote_mailbox_name(pattern),
            )
        except imaplib.IMAP4.abort as exc:
            if not self._connection._list_limit_exceeded:
                raise
            self._logged_out = True
            self._connection._force_close()
            raise ImapExtensionError(
                "mailbox_list_too_large",
                "The IMAP server mailbox list exceeds its safe limit",
            ) from exc
        _require_ok(status, "list folders")
        return [_parse_list_response(row) for row in rows or [] if isinstance(row, bytes)]

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, object]:
        status, _rows = self._connection.select(_quote_mailbox_name(folder), readonly=readonly)
        _require_ok(status, "select folder")
        result: dict[bytes, object] = {}
        for name, values in self._connection.untagged_responses.items():
            key = name.encode("ascii")
            if name in {"EXISTS", "RECENT", "UIDNEXT", "UIDVALIDITY", "HIGHESTMODSEQ"}:
                if values:
                    result[key] = int(values[-1])
            elif name == "FLAGS" and values:
                result[key] = tuple(values[-1].strip(b"()").split())
            elif name == "READ-WRITE":
                result[key] = True
        return result

    def search(
        self, criteria: Sequence[object] | str = "ALL", charset: str | None = None
    ) -> list[int]:
        items: Sequence[object] = [criteria] if isinstance(criteria, str) else criteria
        encoded = tuple(_encode_search_item(item) for item in items)
        status, rows = self._connection.uid("SEARCH", charset, *encoded)
        _require_ok(status, "search")
        return [int(value) for row in rows or [] if row for value in row.split()]

    def fetch(
        self,
        messages: MessageSet,
        data: Sequence[str] | str,
        modifiers: Sequence[str] | None = None,
    ) -> dict[int, dict[bytes, object]]:
        message_ids = _join_message_ids(messages)
        selectors = [data] if isinstance(data, str) else list(data)
        fetch_items = f"({' '.join(_safe_atom(value) for value in selectors)})"
        arguments: list[str] = [message_ids, fetch_items]
        if modifiers:
            arguments.append(f"({' '.join(_safe_atom(value) for value in modifiers)})")
        status, rows = self._connection.uid("FETCH", *arguments)
        _require_ok(status, "fetch")
        return _parse_fetch_rows(rows or [])

    def add_flags(
        self, messages: MessageSet, flags: Sequence[str], silent: bool = False
    ) -> dict[int, tuple[bytes, ...]] | None:
        return self._store_flags(messages, "+FLAGS", flags, silent)

    def remove_flags(
        self, messages: MessageSet, flags: Sequence[str], silent: bool = False
    ) -> dict[int, tuple[bytes, ...]] | None:
        return self._store_flags(messages, "-FLAGS", flags, silent)

    def move(self, messages: MessageSet, folder: str) -> bytes:
        return self._uid_command("MOVE", _join_message_ids(messages), _quote_mailbox_name(folder))

    def copy(self, messages: MessageSet, folder: str) -> bytes:
        return self._uid_command("COPY", _join_message_ids(messages), _quote_mailbox_name(folder))

    def uid_expunge(self, messages: MessageSet) -> bytes:
        return self._uid_command("EXPUNGE", _join_message_ids(messages))

    def logout(self) -> None:
        if self._logged_out:
            return
        self._logged_out = True
        try:
            self._connection.logout()
        finally:
            self._connection._force_close()

    def _store_flags(
        self,
        messages: MessageSet,
        operation: str,
        flags: Sequence[str],
        silent: bool,
    ) -> dict[int, tuple[bytes, ...]] | None:
        suffix = ".SILENT" if silent else ""
        flag_list = "(" + " ".join(_safe_flag(flag) for flag in flags) + ")"
        status, rows = self._connection.uid(
            "STORE", _join_message_ids(messages), operation + suffix, flag_list
        )
        _require_ok(status, "store flags")
        if silent:
            return None
        return _parse_flag_rows(rows or [])

    def _uid_command(self, command: str, *arguments: str | bytes) -> bytes:
        status, rows = self._connection.uid(command, *arguments)
        _require_ok(status, command.lower())
        return b" ".join(row for row in rows or [] if isinstance(row, bytes))


@contextmanager
def open_mailbox_client(
    config: ImapConnectionConfig,
    *,
    resolver: Resolver = resolve_pinned_ips,
    dialer: Dialer = lambda host, port, timeout: socket.create_connection(
        (host, port), timeout=timeout
    ),
    tls_context_factory: TlsContextFactory = ssl.create_default_context,
) -> Iterator[MailboxClient]:
    """Open one authenticated IMAP session using a single pinned DNS result set."""
    try:
        pins = tuple(resolver(config.imap_host, port=config.imap_port, purpose="IMAP server"))
    except PinnedPeerError as exc:
        raise _mapped_transport_error(exc) from exc
    if not pins:
        raise ImapExtensionError("network_blocked", "The IMAP server has no approved address")

    client: _StdlibMailboxClient | None = None
    last_error: BaseException | None = None
    for pin in pins:
        try:
            client = _open_pinned_client(
                config,
                pin,
                dialer=dialer,
                tls_context_factory=tls_context_factory,
            )
            try:
                client._connection.login(config.username, config.password)
            except imaplib.IMAP4.abort:
                raise
            except imaplib.IMAP4.error as exc:
                _safe_logout(client)
                raise ImapExtensionError(
                    "authentication_failed",
                    "The IMAP server rejected the mailbox credentials",
                ) from exc
            break
        except ImapExtensionError:
            raise
        except (OSError, imaplib.IMAP4.error, PinnedPeerError) as exc:
            last_error = exc
            if client is not None:
                _safe_logout(client)
            client = None

    if client is None:
        raise _mapped_transport_error(last_error)

    try:
        yield client
    finally:
        _safe_logout(client)


def _open_pinned_client(
    config: ImapConnectionConfig,
    pin: str,
    *,
    dialer: Dialer,
    tls_context_factory: TlsContextFactory,
) -> _StdlibMailboxClient:
    context = tls_context_factory()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = _PinnedIMAP4(
        config.imap_host,
        config.imap_port,
        pin=pin,
        dialer=dialer,
        connect_timeout=config.connect_timeout,
        read_timeout=config.read_timeout,
        implicit_tls=config.tls_mode == "ssl",
        tls_context=context,
    )
    client = _StdlibMailboxClient(connection)
    if config.tls_mode == "starttls":
        try:
            connection.starttls(context)
            connection.sock.settimeout(config.read_timeout)
        except Exception:
            connection._force_close()
            raise
    return client


def _assert_selected_peer(sock: socket.socket, selected_pin: str) -> None:
    peer = sock.getpeername()
    peer_host = str(peer[0]).split("%", 1)[0]
    selected_host = str(selected_pin).split("%", 1)[0]
    try:
        matches = ipaddress.ip_address(peer_host) == ipaddress.ip_address(selected_host)
    except ValueError as exc:
        raise PinnedPeerError("IMAP server peer address is invalid") from exc
    if not matches:
        raise PinnedPeerError("IMAP server peer does not match the selected address")


def _set_imap_file(connection: _PinnedIMAP4, file_handle: Any) -> None:
    connection._file = file_handle
    file_descriptor = vars(imaplib.IMAP4).get("file")
    if not isinstance(file_descriptor, property) or file_descriptor.fset is not None:
        connection.file = file_handle


def _active_imap_file(connection: _PinnedIMAP4) -> Any:
    file_descriptor = vars(imaplib.IMAP4).get("file")
    if isinstance(file_descriptor, property):
        return connection._file
    return connection.file


def _imap_response_size(value: Any) -> int:
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, tuple):
        return sum(_imap_response_size(item) for item in value)
    return 0


def _safe_logout(client: _StdlibMailboxClient) -> None:
    with suppress(OSError, imaplib.IMAP4.error):
        client.logout()


def _mapped_transport_error(error: BaseException | None) -> ImapExtensionError:
    if isinstance(error, PinnedPeerError):
        return ImapExtensionError("network_blocked", "The IMAP server address is not allowed")
    if isinstance(error, (ssl.SSLCertVerificationError, ssl.SSLError)):
        return ImapExtensionError(
            "tls_verification_failed",
            "The IMAP server TLS certificate could not be verified",
        )
    if isinstance(error, (TimeoutError, socket.timeout)):
        return ImapExtensionError("connection_timeout", "The IMAP server connection timed out")
    return ImapExtensionError("connection_failed", "The IMAP server connection failed")


def _encode_mailbox_name(value: str) -> bytes:
    output = bytearray()
    non_ascii: list[str] = []

    def flush() -> None:
        if not non_ascii:
            return
        encoded = base64.b64encode("".join(non_ascii).encode("utf-16be"))
        output.extend(b"&" + encoded.rstrip(b"=").replace(b"/", b",") + b"-")
        non_ascii.clear()

    for character in value:
        if " " <= character <= "~":
            flush()
            output.extend(b"&-" if character == "&" else character.encode("ascii"))
        else:
            non_ascii.append(character)
    flush()
    return bytes(output)


def _quote_mailbox_name(value: str) -> bytes:
    encoded = _encode_mailbox_name(value)
    escaped = encoded.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
    return b'"' + escaped + b'"'


def _decode_mailbox_name(value: bytes) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index : index + 1] != b"&":
            output.append(chr(value[index]))
            index += 1
            continue
        end = value.find(b"-", index)
        if end < 0:
            raise ValueError("invalid modified UTF-7 mailbox name")
        encoded = value[index + 1 : end]
        if not encoded:
            output.append("&")
        else:
            padding = b"=" * (-len(encoded) % 4)
            try:
                output.append(
                    base64.b64decode(encoded.replace(b",", b"/") + padding).decode("utf-16be")
                )
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise ValueError("invalid modified UTF-7 mailbox name") from exc
        index = end + 1
    return "".join(output)


def _parse_list_response(row: bytes) -> tuple[tuple[bytes, ...], bytes | None, str]:
    match = _LIST_RESPONSE.fullmatch(row)
    if match is None:
        raise ImapExtensionError(
            "connection_failed", _MESSAGE_THE_IMAP_SERVER_RETURNED_INVALID_DATA
        )
    flags = tuple(match.group("flags").split())
    raw_delimiter = match.group("delimiter")
    delimiter = None if raw_delimiter == b"NIL" else _unquote(raw_delimiter)
    return flags, delimiter, _decode_mailbox_name(_unquote(match.group("name")))


def _unquote(value: bytes) -> bytes:
    if len(value) >= 2 and value[:1] == value[-1:] == b'"':
        return value[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return value


def _join_message_ids(messages: MessageSet) -> str:
    values = [messages] if isinstance(messages, int) else list(messages)
    if not values or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values
    ):
        raise ValueError("message ids must be positive integers")
    return ",".join(str(value) for value in values)


def _safe_atom(value: str) -> str:
    if not isinstance(value, str) or not value or any(character in value for character in "\r\n"):
        raise ValueError("invalid IMAP argument")
    return value


def _safe_flag(value: str) -> str:
    value = _safe_atom(value)
    if not re.fullmatch(r"\\?[A-Za-z0-9_-]+", value):
        raise ValueError("invalid IMAP flag")
    return value


def _encode_search_item(value: object) -> bytes:
    if isinstance(value, bool):
        raise ValueError(_MESSAGE_INVALID_IMAP_SEARCH_ITEM)
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, date):
        return value.strftime("%d-%b-%Y").encode("ascii")
    if isinstance(value, bytes):
        if b"\r" in value or b"\n" in value:
            raise ValueError(_MESSAGE_INVALID_IMAP_SEARCH_ITEM)
        return value
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        raise ValueError(_MESSAGE_INVALID_IMAP_SEARCH_ITEM)
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("non-ASCII search values require a declared charset") from exc
    if re.fullmatch(rb"[A-Za-z0-9_.\\-]+", encoded):
        return encoded
    return b'"' + encoded.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def _parse_fetch_rows(rows: list[Any]) -> dict[int, dict[bytes, object]]:
    parsed: dict[int, dict[bytes, object]] = {}
    current_uid: int | None = None
    for row in rows:
        metadata: bytes
        literal: bytes | None = None
        if isinstance(row, tuple) and len(row) == 2 and isinstance(row[0], bytes):
            metadata, literal = row
        elif isinstance(row, bytes):
            metadata = row
        else:
            continue
        match = _FETCH_UID.search(metadata)
        if match is None:
            if current_uid is not None and _FETCH_BODYSTRUCTURE.search(metadata) is not None:
                _parse_bodystructure_into(parsed[current_uid], metadata)
            continue
        uid = int(match.group("uid"))
        current_uid = uid
        values = parsed.setdefault(uid, {b"UID": uid})
        size_match = _FETCH_SIZE.search(metadata)
        if size_match is not None:
            values[b"RFC822.SIZE"] = int(size_match.group("size"))
        flags_match = _FETCH_FLAGS.search(metadata)
        if flags_match is not None:
            values[b"FLAGS"] = tuple(flags_match.group("flags").split())
        if _FETCH_BODYSTRUCTURE.search(metadata) is not None:
            _parse_bodystructure_into(values, metadata)
        if literal is not None:
            if len(literal) > MAX_FETCH_LITERAL_BYTES:
                raise ImapExtensionError(
                    "connection_failed", _MESSAGE_THE_IMAP_SERVER_RETURNED_INVALID_DATA
                )
            selector_match = _FETCH_LITERAL_SELECTOR.search(metadata)
            if selector_match is None:
                raise ImapExtensionError(
                    "connection_failed", _MESSAGE_THE_IMAP_SERVER_RETURNED_INVALID_DATA
                )
            values[_canonical_response_selector(selector_match.group("selector"))] = literal
    return parsed


def _canonical_response_selector(selector: bytes) -> bytes:
    canonical = selector.upper()
    return re.sub(rb"^BODY\.PEEK\[", b"BODY[", canonical)


def _parse_bodystructure_into(values: dict[bytes, object], metadata: bytes) -> None:
    bodystructure_match = _FETCH_BODYSTRUCTURE.search(metadata)
    if bodystructure_match is None:
        return
    if len(metadata) - bodystructure_match.end() > MAX_BODYSTRUCTURE_RESPONSE_BYTES:
        raise ImapExtensionError(
            "connection_failed", _MESSAGE_THE_IMAP_SERVER_RETURNED_INVALID_DATA
        )
    try:
        bodystructure, _end = _parse_imap_value(
            metadata,
            bodystructure_match.end(),
            budget=_ImapParseBudget(),
            depth=0,
        )
    except (RecursionError, ValueError) as exc:
        raise ImapExtensionError(
            "connection_failed", _MESSAGE_THE_IMAP_SERVER_RETURNED_INVALID_DATA
        ) from exc
    if not isinstance(bodystructure, tuple):
        raise ImapExtensionError(
            "connection_failed", _MESSAGE_THE_IMAP_SERVER_RETURNED_INVALID_DATA
        )
    values[b"BODYSTRUCTURE"] = bodystructure


def _parse_imap_value(
    value: bytes,
    index: int,
    *,
    budget: _ImapParseBudget,
    depth: int,
) -> tuple[object, int]:
    if depth > MAX_BODYSTRUCTURE_DEPTH:
        raise ValueError("IMAP value exceeds nesting depth")
    index = _skip_imap_space(value, index)
    if index >= len(value):
        raise ValueError("missing IMAP value")
    character = value[index : index + 1]
    if character == b"(":
        budget.consume(container=True)
        items: list[object] = []
        index += 1
        while True:
            index = _skip_imap_space(value, index)
            if index >= len(value):
                raise ValueError("unterminated IMAP list")
            if value[index : index + 1] == b")":
                return tuple(items), index + 1
            item, index = _parse_imap_value(
                value,
                index,
                budget=budget,
                depth=depth + 1,
            )
            items.append(item)
    if character == b'"':
        budget.consume()
        index += 1
        output = bytearray()
        while index < len(value):
            character = value[index : index + 1]
            index += 1
            if character == b'"':
                return bytes(output), index
            if character == b"\\":
                if index >= len(value):
                    raise ValueError("invalid IMAP quote escape")
                character = value[index : index + 1]
                index += 1
            output.extend(character)
        raise ValueError("unterminated IMAP quoted value")
    end = index
    while end < len(value) and value[end : end + 1] not in b" ()":
        end += 1
    if end == index:
        raise ValueError("invalid IMAP value")
    budget.consume()
    atom = value[index:end]
    return (None if atom.upper() == b"NIL" else atom), end


def _skip_imap_space(value: bytes, index: int) -> int:
    while index < len(value) and value[index : index + 1] == b" ":
        index += 1
    return index


def _parse_flag_rows(rows: list[Any]) -> dict[int, tuple[bytes, ...]]:
    result: dict[int, tuple[bytes, ...]] = {}
    pattern = re.compile(rb"\bUID (\d+)\b.*?\bFLAGS \(([^)]*)\)")
    for row in rows:
        if not isinstance(row, bytes):
            continue
        match = pattern.search(row)
        if match:
            result[int(match.group(1))] = tuple(match.group(2).split())
    return result


def _require_ok(status: str, operation: str) -> None:
    if status != "OK":
        raise imaplib.IMAP4.error(f"{operation} failed")
