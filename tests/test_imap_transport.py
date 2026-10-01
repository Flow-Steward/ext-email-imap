from __future__ import annotations

import imaplib
import socket
import ssl
import sys
from dataclasses import replace
from pathlib import Path

import pytest

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SDK_PARENT = Path(__file__).resolve().parents[3] / "core/infrastructure/extension_sdk/public"
for import_root in (BUNDLE_ROOT, SDK_PARENT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from fake_imap_server import FakeImapServer  # noqa: E402
from imap_config import ImapConnectionConfig  # noqa: E402
from imap_errors import ImapExtensionError  # noqa: E402
from imap_transport import _parse_fetch_rows, _PinnedIMAP4, open_mailbox_client  # noqa: E402

BODYSTRUCTURE_RESPONSE_LIMIT = 512 * 1024
BODYSTRUCTURE_DEPTH_LIMIT = 32
BODYSTRUCTURE_NODE_LIMIT = 2_048
BODYSTRUCTURE_TOKEN_LIMIT = 10_000
FETCH_LITERAL_LIMIT = 64 * 1024
FETCH_RESPONSE_LINE_LIMIT = BODYSTRUCTURE_RESPONSE_LIMIT + FETCH_LITERAL_LIMIT


def _config(*, tls_mode: str = "ssl") -> ImapConnectionConfig:
    return ImapConnectionConfig(
        provider_preset="manual",
        imap_host="imap.example.test",
        imap_port=993 if tls_mode == "ssl" else 143,
        tls_mode=tls_mode,
        username="operator@example.test",
        password="secret",
        connect_timeout=4.5,
        read_timeout=8.5,
    )


def _open(server: FakeImapServer, *, tls_mode: str = "ssl", resolver=None):
    chosen_resolver = resolver or (lambda *_a, **_k: ["203.0.113.10"])
    return open_mailbox_client(
        _config(tls_mode=tls_mode),
        resolver=chosen_resolver,
        dialer=server.dial,
        tls_context_factory=server.tls_context,
    )


def _two_pins(*_args, **_kwargs) -> list[str]:
    return ["203.0.113.10", "203.0.113.20"]


def test_ssl_dials_pin_but_verifies_original_host() -> None:
    server = FakeImapServer()

    with _open(server) as client:
        assert b"IMAP4REV1" in client.capabilities()

    assert server.dialed_hosts == ["203.0.113.10"]
    assert server.server_names == ["imap.example.test"]


def test_fetch_parser_preserves_summary_metadata_and_nested_bodystructure() -> None:
    parsed = _parse_fetch_rows(
        [
            b"* 4 FETCH (UID 17 RFC822.SIZE 1234 FLAGS (\\Seen $Forwarded) BODYSTRUCTURE "
            b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 10 1 NIL NIL NIL) '
            b'("APPLICATION" "PDF" ("NAME" "invoice.pdf") NIL NIL "BASE64" 100 NIL '
            b'("ATTACHMENT" ("FILENAME" "invoice.pdf")) NIL NIL) '
            b'"MIXED" ("BOUNDARY" "x") NIL NIL))'
        ]
    )

    assert parsed == {
        17: {
            b"UID": 17,
            b"RFC822.SIZE": 1234,
            b"FLAGS": (b"\\Seen", b"$Forwarded"),
            b"BODYSTRUCTURE": (
                (
                    b"TEXT",
                    b"PLAIN",
                    (b"CHARSET", b"UTF-8"),
                    None,
                    None,
                    b"7BIT",
                    b"10",
                    b"1",
                    None,
                    None,
                    None,
                ),
                (
                    b"APPLICATION",
                    b"PDF",
                    (b"NAME", b"invoice.pdf"),
                    None,
                    None,
                    b"BASE64",
                    b"100",
                    None,
                    (b"ATTACHMENT", (b"FILENAME", b"invoice.pdf")),
                    None,
                    None,
                ),
                b"MIXED",
                (b"BOUNDARY", b"x"),
                None,
                None,
            ),
        }
    }


def test_fetch_parser_attaches_bodystructure_trailer_after_header_literal_to_same_uid() -> None:
    parsed = _parse_fetch_rows(
        [
            (
                b"17 (UID 17 RFC822.SIZE 1234 FLAGS (\\Seen) "
                b"BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)] {45}",
                b"Date: Tue, 1 Jan 2026 00:00:00 +0000\r\n\r\n",
            ),
            b' BODYSTRUCTURE (("TEXT" "PLAIN" NIL NIL NIL "7BIT" 10 1 NIL NIL NIL) '
            b'("APPLICATION" "PDF" ("NAME" "invoice.pdf") NIL NIL "BASE64" 100 NIL '
            b'("ATTACHMENT" ("FILENAME" "invoice.pdf")) NIL NIL) "MIXED" NIL NIL NIL)',
        ]
    )

    assert parsed[17][b"UID"] == 17
    assert parsed[17][b"RFC822.SIZE"] == 1234
    assert parsed[17][b"FLAGS"] == (b"\\Seen",)
    assert parsed[17][b"BODYSTRUCTURE"][2] == b"MIXED"
    assert parsed[17][b"BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]"] == (
        b"Date: Tue, 1 Jan 2026 00:00:00 +0000\r\n\r\n"
    )


def test_fetch_parser_does_not_attach_a_trailer_to_a_previous_uid_after_new_fetch_starts() -> None:
    parsed = _parse_fetch_rows(
        [
            (b"17 (UID 17 BODY.PEEK[HEADER.FIELDS (SUBJECT)] {1}", b"a"),
            (b"18 (UID 18 BODY.PEEK[HEADER.FIELDS (SUBJECT)] {1}", b"b"),
            b' BODYSTRUCTURE ("APPLICATION" "PDF" NIL NIL NIL "BASE64" 100 NIL '
            b'("ATTACHMENT" ("FILENAME" "invoice.pdf")) NIL NIL)',
        ]
    )

    assert b"BODYSTRUCTURE" not in parsed[17]
    assert parsed[18][b"BODYSTRUCTURE"][0:2] == (b"APPLICATION", b"PDF")


@pytest.mark.parametrize(
    ("response_selector", "literal"),
    [
        (
            b"BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]",
            b"Subject: invoice\r\n\r\n",
        ),
        (b"BODY[1]", b"plain text"),
        (b"BODY[2]", b"<b>html</b>"),
        (b"BODY[3.MIME]", b"Content-Type: application/pdf\r\n\r\n"),
        (b"BODY[3]", b"JVBERi0="),
    ],
)
def test_raw_imaplib_partial_literal_rows_use_rfc_response_keys(
    response_selector: bytes, literal: bytes
) -> None:
    parsed = _parse_fetch_rows(
        [
            (
                b"7 (UID 7 " + response_selector + b"<0> {" + str(len(literal)).encode() + b"}",
                literal,
            ),
            b")",
        ]
    )

    assert parsed[7][response_selector] == literal
    assert all(b"BODY.PEEK[" not in key for key in parsed[7])


def test_fetch_parser_accepts_greenmail_partial_literal_without_separator_space() -> None:
    literal = b'Content-Type: text/plain; charset="utf-8"\r\nContent-Transfer-Encoding: 7bit'

    parsed = _parse_fetch_rows(
        [
            (b"1 (UID 1 BODY[1.MIME]<0>{74}", literal),
            b")",
        ]
    )

    assert parsed[1][b"BODY[1.MIME]"] == literal


@pytest.mark.parametrize(
    ("response_selector", "canonical_selector", "literal"),
    [
        (
            b"bOdY.PeEk[header.fields (date from to cc subject message-id)]",
            b"BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]",
            b"Subject: invoice\r\n\r\n",
        ),
        (b"body[1]", b"BODY[1]", b"plain text"),
        (b"Body[2]", b"BODY[2]", b"<b>html</b>"),
        (
            b"bODY[3.mime]",
            b"BODY[3.MIME]",
            b"Content-Type: application/pdf\r\n\r\n",
        ),
        (b"body.peek[3]", b"BODY[3]", b"JVBERi0="),
    ],
)
def test_fetch_parser_normalizes_mixed_case_literal_selectors(
    response_selector: bytes,
    canonical_selector: bytes,
    literal: bytes,
) -> None:
    parsed = _parse_fetch_rows(
        [
            (
                b"7 (uId 7 " + response_selector + b"<0> {" + str(len(literal)).encode() + b"}",
                literal,
            ),
            b")",
        ]
    )

    assert parsed[7][canonical_selector] == literal


def test_fetch_parser_handles_mixed_case_metadata_and_bodystructure_trailer() -> None:
    parsed = _parse_fetch_rows(
        [
            (
                b"7 (uId 7 rFc822.sIzE 1234 fLaGs (\\Seen) bOdY[header.fields (subject)]<0> {20}",
                b"Subject: invoice\r\n\r\n",
            ),
            b' bOdYsTrUcTuRe ("APPLICATION" "PDF" NIL NIL NIL "BASE64" 100 NIL '
            b'("ATTACHMENT" ("FILENAME" "invoice.pdf")) NIL NIL)',
        ]
    )

    assert parsed[7][b"UID"] == 7
    assert parsed[7][b"RFC822.SIZE"] == 1234
    assert parsed[7][b"FLAGS"] == (b"\\Seen",)
    assert parsed[7][b"BODY[HEADER.FIELDS (SUBJECT)]"] == b"Subject: invoice\r\n\r\n"
    assert parsed[7][b"BODYSTRUCTURE"][:2] == (b"APPLICATION", b"PDF")


def test_bodystructure_parser_rejects_oversized_response_before_tree_construction() -> None:
    metadata = b"7 (UID 7 BODYSTRUCTURE (" + b"A" * BODYSTRUCTURE_RESPONSE_LIMIT + b"))"

    with pytest.raises(ImapExtensionError) as error:
        _parse_fetch_rows([metadata])

    assert error.value.code == "connection_failed"


def test_bodystructure_parser_rejects_excessive_nesting_depth() -> None:
    nested = (
        b"(" * (BODYSTRUCTURE_DEPTH_LIMIT + 1) + b"NIL" + b")" * (BODYSTRUCTURE_DEPTH_LIMIT + 1)
    )

    with pytest.raises(ImapExtensionError) as error:
        _parse_fetch_rows([b"7 (UID 7 BODYSTRUCTURE " + nested + b")"])

    assert error.value.code == "connection_failed"


def test_bodystructure_parser_rejects_excessive_tokens() -> None:
    tokens = b" ".join(b"NIL" for _ in range(BODYSTRUCTURE_TOKEN_LIMIT + 1))

    with pytest.raises(ImapExtensionError) as error:
        _parse_fetch_rows([b"7 (UID 7 BODYSTRUCTURE (" + tokens + b"))"])

    assert error.value.code == "connection_failed"


def test_bodystructure_parser_rejects_excessive_container_nodes() -> None:
    nodes = b" ".join(b"()" for _ in range(BODYSTRUCTURE_NODE_LIMIT + 1))

    with pytest.raises(ImapExtensionError) as error:
        _parse_fetch_rows([b"7 (UID 7 BODYSTRUCTURE (" + nodes + b"))"])

    assert error.value.code == "connection_failed"


def test_transport_rejects_oversized_server_literal_before_reading_body() -> None:
    connection = object.__new__(_PinnedIMAP4)

    with pytest.raises(imaplib.IMAP4.abort, match="literal exceeds"):
        connection.read(FETCH_LITERAL_LIMIT + 1)


def test_fetch_parser_rejects_oversized_literal_rows() -> None:
    literal = b"x" * (FETCH_LITERAL_LIMIT + 1)

    with pytest.raises(ImapExtensionError) as error:
        _parse_fetch_rows([(b"7 (UID 7 BODY[1]<0> {65537}", literal), b")"])

    assert error.value.code == "connection_failed"


def test_transport_bounds_response_line_read_before_materialization() -> None:
    requested_limits: list[int] = []

    class OversizedLine:
        def readline(self, limit: int) -> bytes:
            requested_limits.append(limit)
            return b"x" * limit

    connection = object.__new__(_PinnedIMAP4)
    connection.file = OversizedLine()
    connection._file = connection.file

    with pytest.raises(imaplib.IMAP4.error, match="response line exceeds"):
        connection.readline()

    assert requested_limits == [FETCH_RESPONSE_LINE_LIMIT + 1]


def test_starttls_finishes_verified_tls_before_login() -> None:
    server = FakeImapServer(require_tls_before_login=True)

    with _open(server, tls_mode="starttls"):
        pass

    assert server.commands.index("STARTTLS") < server.commands.index("LOGIN")
    assert server.server_names == ["imap.example.test"]


def test_default_resolver_rejects_private_target_before_dial() -> None:
    server = FakeImapServer()
    config = replace(_config(), imap_host="127.0.0.1")

    with (
        pytest.raises(ImapExtensionError) as error,
        open_mailbox_client(config, dialer=server.dial),
    ):
        pass

    assert error.value.code == "network_blocked"
    assert server.dialed_hosts == []


def test_private_override_uses_default_sdk_resolver_and_keeps_pin_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeImapServer(peer_host="127.0.0.1")
    resolutions: list[tuple[str, int]] = []

    def fake_getaddrinfo(host: str, port: int, **_kwargs: object):
        resolutions.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]

    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with open_mailbox_client(
        _config(),
        dialer=server.dial,
        tls_context_factory=server.tls_context,
    ):
        pass

    assert resolutions == [("imap.example.test", 993)]
    assert server.dialed_hosts == ["127.0.0.1"]
    assert server.server_names == ["imap.example.test"]


def test_raw_list_response_bounds_aggregate_bytes_before_parsing_all_rows() -> None:
    row = b'* LIST (\\HasNoChildren) "/" "' + (b"a" * 2_000) + b'"\r\n'
    server = FakeImapServer(list_rows=[row] * 600)

    with _open(server) as client, pytest.raises(ImapExtensionError) as error:
        client.list_folders()

    assert error.value.code == "mailbox_list_too_large"
    assert "LOGOUT" not in server.commands
    assert server.sockets[0].closed is True


def test_peer_mismatch_is_rejected_before_login() -> None:
    server = FakeImapServer(peer_host="203.0.113.11")

    with pytest.raises(ImapExtensionError) as error, _open(server):
        pass

    assert error.value.code == "network_blocked"
    assert "LOGIN" not in server.commands
    assert server.sockets[0].closed is True


def test_certificate_failure_happens_before_login() -> None:
    server = FakeImapServer(certificate_error=True)

    with pytest.raises(ImapExtensionError) as error, _open(server):
        pass

    assert error.value.code == "tls_verification_failed"
    assert "LOGIN" not in server.commands


def test_starttls_certificate_failure_closes_without_plaintext_logout() -> None:
    server = FakeImapServer(certificate_error=True)

    with pytest.raises(ImapExtensionError) as error, _open(server, tls_mode="starttls"):
        pass

    assert error.value.code == "tls_verification_failed"
    assert "LOGIN" not in server.commands
    assert "LOGOUT" not in server.commands
    assert server.sockets[0].closed is True


def test_resolution_occurs_once_and_only_returned_pins_are_dialed() -> None:
    server = FakeImapServer(dial_errors={"203.0.113.10": OSError("unreachable")})
    resolver_calls: list[tuple[str, int, str]] = []

    def resolver(host: str, *, port: int, purpose: str) -> list[str]:
        resolver_calls.append((host, port, purpose))
        return ["203.0.113.10", "203.0.113.20"]

    with _open(server, resolver=resolver):
        pass

    assert resolver_calls == [("imap.example.test", 993, "IMAP server")]
    assert server.dialed_hosts == ["203.0.113.10", "203.0.113.20"]


def test_login_abort_retries_the_next_approved_pin() -> None:
    server = FakeImapServer(
        login_errors={"203.0.113.10": imaplib.IMAP4.abort("connection dropped")}
    )

    with _open(server, resolver=_two_pins):
        pass

    assert server.dialed_hosts == ["203.0.113.10", "203.0.113.20"]
    assert server.commands.count("LOGIN") == 2


@pytest.mark.parametrize(
    ("server", "expected_code"),
    [
        (FakeImapServer(dial_errors={"203.0.113.10": TimeoutError()}), "connection_timeout"),
        (FakeImapServer(read_error=TimeoutError()), "connection_timeout"),
    ],
)
def test_connect_and_read_timeouts_are_mapped(
    server: FakeImapServer,
    expected_code: str,
) -> None:
    with pytest.raises(ImapExtensionError) as error, _open(server):
        pass

    assert error.value.code == expected_code


def test_configured_connect_and_read_timeouts_are_applied() -> None:
    server = FakeImapServer()

    with _open(server):
        pass

    assert server.dial_timeouts == [4.5]
    assert server.sockets[0].timeout_values[-1] == 8.5


def test_stdlib_fallback_round_trips_modified_utf7_mailbox_names() -> None:
    server = FakeImapServer()

    with _open(server) as client:
        folders = client.list_folders()
        client.select_folder("Projekti/ß", readonly=True)

    assert folders == [((b"\\HasNoChildren",), b"/", "Projekti/ß")]
    assert ("EXAMINE", (("Projekti/&AN8-", True),)) in server.parsed_commands


def test_mailbox_arguments_use_quoted_imap_strings() -> None:
    server = FakeImapServer()

    with _open(server) as client:
        client.list_folders('Root "Q"\\ß', '* "Q"\\ß*')
        client.select_folder('Shared "Q"\\ß Box', readonly=True)
        client.select_folder('Selected "Q"\\ß Box')
        client.move([7], 'Moved "Q"\\ß Box')
        client.copy([8], 'Copied "Q"\\ß Box')

    relevant_commands = [
        command
        for command in server.parsed_commands
        if command[0] in {"LIST", "EXAMINE", "SELECT", "UID"}
    ]
    assert relevant_commands == [
        ("LIST", (('Root "Q"\\&AN8-', True), ('* "Q"\\&AN8-*', True))),
        ("EXAMINE", (('Shared "Q"\\&AN8- Box', True),)),
        ("SELECT", (('Selected "Q"\\&AN8- Box', True),)),
        ("UID", (("MOVE", False), ("7", False), ('Moved "Q"\\&AN8- Box', True))),
        ("UID", (("COPY", False), ("8", False), ('Copied "Q"\\&AN8- Box', True))),
    ]


def test_read_only_imap_file_property_is_supported(monkeypatch) -> None:
    monkeypatch.setattr(
        imaplib.IMAP4,
        "file",
        property(lambda connection: connection._file),
        raising=False,
    )
    server = FakeImapServer()

    with _open(server) as client:
        assert b"IMAP4REV1" in client.capabilities()


@pytest.mark.parametrize("tls_mode", ["ssl", "starttls"])
def test_tls_minimum_version_is_1_2(tls_mode: str) -> None:
    server = FakeImapServer()

    with _open(server, tls_mode=tls_mode):
        pass

    assert server.contexts[0].minimum_version == ssl.TLSVersion.TLSv1_2


def test_logout_is_attempted_without_close_or_global_expunge() -> None:
    server = FakeImapServer()

    with pytest.raises(RuntimeError, match="operation failed"), _open(server):
        raise RuntimeError("operation failed")

    assert "LOGOUT" in server.commands
    assert "CLOSE" not in server.commands
    assert "EXPUNGE" not in server.commands
    assert server.sockets[0].closed is True


def test_authentication_failure_is_safe_and_closes_connection() -> None:
    server = FakeImapServer(reject_login=True)

    with pytest.raises(ImapExtensionError) as error, _open(server, resolver=_two_pins):
        pass

    assert error.value.code == "authentication_failed"
    assert "secret" not in error.value.message
    assert server.sockets[0].closed is True
    assert server.dialed_hosts == ["203.0.113.10"]
