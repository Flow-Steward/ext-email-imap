from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SDK_PARENT = Path(__file__).resolve().parents[3] / "core/infrastructure/extension_sdk/public"
for import_root in (BUNDLE_ROOT, SDK_PARENT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from fake_mailbox_client import FakeMailboxClient  # noqa: E402
from imap_errors import ImapExtensionError  # noqa: E402
from imap_messages import (  # noqa: E402
    MAX_ATTACHMENTS,
    MAX_HEADER_BYTES,
    MAX_MIME_PARTS,
    MAX_TEXT_BYTES,
    get_message,
)


def _leaf(
    major: bytes,
    minor: bytes,
    *,
    size: int,
    charset: bytes | None = None,
    filename: bytes | None = None,
) -> tuple[object, ...]:
    parameters: tuple[bytes, ...] | None = None
    if charset is not None:
        parameters = (b"CHARSET", charset)
    if filename is not None:
        parameters = (*parameters, b"NAME", filename) if parameters else (b"NAME", filename)
    disposition = (b"ATTACHMENT", (b"FILENAME", filename)) if filename else None
    return (
        major,
        minor,
        parameters,
        None,
        None,
        b"BASE64",
        size,
        None,
        disposition,
        None,
        None,
    )


def _multipart(*parts: tuple[object, ...]) -> tuple[object, ...]:
    return (*parts, b"MIXED", (b"BOUNDARY", b"mail-boundary"), None, None)


def _message_client(
    *,
    bodystructure: object | None = None,
    plain: bytes = b"Invoice total: =E2=82=AC42",
    html: bytes = b"PGI+SW52b2ljZTwvYj4=",
    headers: bytes | None = None,
) -> FakeMailboxClient:
    structure = bodystructure or _multipart(
        _leaf(b"TEXT", b"PLAIN", size=len(plain), charset=b"UTF-8"),
        _leaf(b"TEXT", b"HTML", size=len(html), charset=b"UTF-8"),
        _leaf(b"APPLICATION", b"PDF", size=12, filename=b"invoice.pdf"),
    )
    return FakeMailboxClient(
        uids=[7],
        bodystructures={7: structure},
        message_sections={
            7: {
                "BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]": headers
                or (
                    b"Date: Fri, 21 Aug 2026 09:30:00 +0000\r\n"
                    b"From: Billing <billing@example.test>\r\n"
                    b"To: AP <ap@example.test>\r\n"
                    b"Cc: Audit <audit@example.test>\r\n"
                    b"Subject: August =?utf-8?b?4oKs?= invoice\r\n"
                    b"Message-ID: <invoice-7@example.test>\r\n\r\n"
                ),
                "BODY[1.MIME]": (
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
                ),
                "BODY[1]": plain,
                "BODY[2.MIME]": (
                    b"Content-Type: text/html; charset=utf-8\r\n"
                    b"Content-Transfer-Encoding: base64\r\n\r\n"
                ),
                "BODY[2]": html,
            }
        },
    )


def test_get_message_returns_bounded_fields_and_part_path_attachment_ids() -> None:
    client = _message_client()

    result = get_message(
        client,
        {
            "mailbox": "INBOX",
            "uid": 7,
            "include_body": True,
            "include_headers": True,
            "include_attachment_metadata": True,
            "include_raw_html": True,
        },
    )

    assert result == {
        "message": {
            "mailbox": "INBOX",
            "uid": 7,
            "date": "Fri, 21 Aug 2026 09:30:00 +0000",
            "from": "Billing <billing@example.test>",
            "to": "AP <ap@example.test>",
            "cc": "Audit <audit@example.test>",
            "subject": "August € invoice",
            "message_id": "<invoice-7@example.test>",
            "headers": {
                "date": "Fri, 21 Aug 2026 09:30:00 +0000",
                "from": "Billing <billing@example.test>",
                "to": "AP <ap@example.test>",
                "cc": "Audit <audit@example.test>",
                "subject": "August € invoice",
                "message_id": "<invoice-7@example.test>",
            },
            "plain_text": "Invoice total: €42",
            "raw_html": "<b>Invoice</b>",
            "attachments": [
                {
                    "attachment_id": "3",
                    "original_filename": "invoice.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 12,
                }
            ],
            "attachments_truncated": False,
        }
    }
    assert client.selected_folders == [("INBOX", True)]
    assert all(
        "BODY.PEEK[" in field or field in {"UID", "RFC822.SIZE", "BODYSTRUCTURE"}
        for _uids, fields in client.fetches
        for field in fields
    )
    assert client.mutation_calls == []
    assert "sanitized_html" not in result["message"]


def test_raw_html_requires_explicit_request_and_headers_can_be_omitted() -> None:
    result = get_message(
        _message_client(),
        {
            "mailbox": "INBOX",
            "uid": 7,
            "include_body": True,
            "include_headers": False,
            "include_attachment_metadata": False,
        },
    )["message"]

    assert result["plain_text"] == "Invoice total: €42"
    assert "raw_html" not in result
    assert "headers" not in result
    assert "attachments" not in result
    assert "sanitized_html" not in result


def test_rejects_headers_larger_than_256_kib_with_safe_error() -> None:
    client = _message_client(headers=b"Subject: " + b"x" * MAX_HEADER_BYTES)

    with pytest.raises(ImapExtensionError) as error:
        get_message(client, {"mailbox": "INBOX", "uid": 7})

    assert error.value.code == "message_too_large"
    assert "x" * 32 not in error.value.message


def test_rejects_decoded_text_larger_than_one_mib() -> None:
    body = base64.b64encode(b"x" * (MAX_TEXT_BYTES + 1))
    client = _message_client(plain=body)
    client.message_sections[7]["BODY[1.MIME]"] = (
        b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
    )

    with pytest.raises(ImapExtensionError) as error:
        get_message(client, {"mailbox": "INBOX", "uid": 7})

    assert error.value.code == "message_too_large"


def test_rejects_more_than_two_hundred_mime_parts_before_fetching_part_bodies() -> None:
    parts = tuple(
        _leaf(b"TEXT", b"PLAIN", size=1, charset=b"UTF-8") for _ in range(MAX_MIME_PARTS + 1)
    )
    client = _message_client(bodystructure=_multipart(*parts))

    with pytest.raises(ImapExtensionError) as error:
        get_message(client, {"mailbox": "INBOX", "uid": 7})

    assert error.value.code == "message_too_complex"
    assert len(client.fetches) == 1


def test_attachment_metadata_is_capped_at_one_hundred_rows() -> None:
    parts = tuple(
        _leaf(b"TEXT", b"PLAIN", size=1, filename=f"row-{index}.txt".encode())
        for index in range(MAX_ATTACHMENTS + 1)
    )
    result = get_message(
        _message_client(bodystructure=_multipart(*parts)),
        {
            "mailbox": "INBOX",
            "uid": 7,
            "include_body": False,
            "include_attachment_metadata": True,
        },
    )["message"]

    assert len(result["attachments"]) == MAX_ATTACHMENTS
    assert result["attachments"][0]["attachment_id"] == "1"
    assert result["attachments"][-1]["attachment_id"] == "100"
    assert result["attachments_truncated"] is True


def test_html_remote_resources_are_returned_as_text_without_network_access(monkeypatch) -> None:
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("network access"))
    html = base64.b64encode(b'<img src="https://tracker.example.test/pixel">')

    result = get_message(
        _message_client(html=html),
        {"mailbox": "INBOX", "uid": 7, "include_raw_html": True},
    )["message"]

    assert result["raw_html"] == '<img src="https://tracker.example.test/pixel">'


def test_message_literals_are_requested_with_bounded_peek_partials() -> None:
    client = _message_client()

    get_message(client, {"mailbox": "INBOX", "uid": 7, "include_raw_html": True})

    literal_fields = [
        field
        for _uids, fields in client.fetches
        for field in fields
        if field.startswith("BODY.PEEK[")
    ]
    assert literal_fields
    assert all("<" in field and field.endswith(">") for field in literal_fields)
    assert not any(field.endswith("]") for field in literal_fields)


def test_declared_oversized_text_part_is_rejected_before_body_fetch() -> None:
    structure = _multipart(_leaf(b"TEXT", b"PLAIN", size=MAX_TEXT_BYTES * 4 + 1, charset=b"UTF-8"))
    client = _message_client(bodystructure=structure)

    with pytest.raises(ImapExtensionError) as error:
        get_message(client, {"mailbox": "INBOX", "uid": 7})

    assert error.value.code == "message_too_large"
    assert not any("BODY.PEEK[1]" in field for _uids, fields in client.fetches for field in fields)


def test_bounded_partial_fetches_reassemble_text_without_full_literal_request() -> None:
    plain = b"a" * 70_000
    client = _message_client(plain=plain)
    client.message_sections[7]["BODY[1.MIME]"] = (
        b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 7bit\r\n\r\n"
    )

    result = get_message(client, {"mailbox": "INBOX", "uid": 7})

    assert result["message"]["plain_text"] == plain.decode()
    body_requests = [
        field
        for _uids, fields in client.fetches
        for field in fields
        if field.startswith("BODY.PEEK[1]<")
    ]
    assert len(body_requests) == 2
    assert body_requests[0].startswith("BODY.PEEK[1]<0.")


def _single_part_client(*, include_part_mime_section: bool) -> FakeMailboxClient:
    """A message that is not multipart, as most customer mail is.

    RFC 3501 gives such a message's single part no separate MIME section — the
    message header is its part header — so a server may refuse `BODY[1.MIME]`.
    GreenMail does exactly that. `include_part_mime_section` lets a test offer
    the section anyway, to show the reader does not depend on it either way.
    """
    sections = {
        "BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]": (
            b"Date: Wed, 03 Sep 2026 08:00:00 +0000\r\n"
            b"From: Customer <customer@example.test>\r\n"
            b"To: Support <support@example.test>\r\n"
            b"Subject: Where is my order?\r\n"
            b"Message-ID: <plain-11@example.test>\r\n\r\n"
        ),
        "BODY[HEADER.FIELDS (CONTENT-TYPE CONTENT-TRANSFER-ENCODING CONTENT-DISPOSITION)]": (
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        ),
        "BODY[1]": b"I placed order 1192 and heard nothing =E2=80=94 what is happening?",
    }
    if include_part_mime_section:
        sections["BODY[1.MIME]"] = (
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        )
    body = _leaf(b"TEXT", b"PLAIN", size=64, charset=b"UTF-8")
    return FakeMailboxClient(
        uids=[11], bodystructures={11: body}, message_sections={11: sections}
    )


def test_plain_text_message_is_read_without_a_part_mime_section() -> None:
    """IMAP-001: the commonest customer email could not be read at all."""
    client = _single_part_client(include_part_mime_section=False)

    result = get_message(client, {"mailbox": "INBOX", "uid": 11})

    message = result["message"]
    assert "order 1192" in message["plain_text"]
    assert message["plain_text"].endswith("what is happening?")


def test_plain_text_message_reads_the_same_when_the_server_offers_a_part_section() -> None:
    lenient = get_message(
        _single_part_client(include_part_mime_section=True), {"mailbox": "INBOX", "uid": 11}
    )
    strict = get_message(
        _single_part_client(include_part_mime_section=False), {"mailbox": "INBOX", "uid": 11}
    )

    assert lenient["message"]["plain_text"] == strict["message"]["plain_text"]


def test_multipart_parts_still_use_their_own_mime_section() -> None:
    """The fix must not change how a part of a multipart message is addressed."""
    from imap_messages import MimePart, _mime_header_selector

    part_of_multipart = MimePart(
        part_path="2",
        content_type="text/html",
        charset="utf-8",
        filename=None,
        size_bytes=10,
        transfer_encoding="base64",
        is_attachment=False,
    )
    message_body = MimePart(
        part_path="1",
        content_type="text/plain",
        charset="utf-8",
        filename=None,
        size_bytes=10,
        transfer_encoding="7bit",
        is_attachment=False,
        is_message_body=True,
    )

    assert _mime_header_selector(part_of_multipart) == "BODY.PEEK[2.MIME]"
    assert ".MIME]" not in _mime_header_selector(message_body)


def _many_attachments_client() -> FakeMailboxClient:
    """A message shaped like real mail: a signature logo, a photo, and the file.

    Their part paths are not knowable when the workflow is written, so an author
    who can only say "part 2" is guessing.
    """
    structure = _multipart(
        _leaf(b"TEXT", b"PLAIN", size=20, charset=b"UTF-8"),
        _leaf(b"IMAGE", b"PNG", size=900, filename=b"signature-logo.png"),
        _leaf(b"IMAGE", b"JPEG", size=4096, filename=b"team-photo.jpg"),
        _leaf(b"APPLICATION", b"PDF", size=2048, filename=b"invoice-2026-09.pdf"),
    )
    return FakeMailboxClient(uids=[21], bodystructures={21: structure}, message_sections={21: {}})


def _select(**selector):
    from imap_messages import _mime_parts, _select_attachment, _attachment_selector

    client = _many_attachments_client()
    parts = [
        part
        for part in _mime_parts(client.fetch([21], ["BODYSTRUCTURE"])[21][b"BODYSTRUCTURE"])
        if part.is_attachment
    ]
    return _select_attachment(parts, _attachment_selector(selector))


def test_an_attachment_can_be_chosen_by_content_type() -> None:
    assert _select(content_type="application/pdf").filename == "invoice-2026-09.pdf"


def test_an_attachment_can_be_chosen_by_filename() -> None:
    assert _select(filename="team-photo.jpg").content_type == "image/jpeg"


def test_a_wildcard_content_type_that_matches_two_files_refuses_to_guess() -> None:
    """Two images: picking the first silently would be the wrong kind of helpful."""
    with pytest.raises(ImapExtensionError) as excinfo:
        _select(content_type="image/*")

    assert excinfo.value.code == "attachment_ambiguous"
    assert "signature-logo.png" in str(excinfo.value)
    assert "team-photo.jpg" in str(excinfo.value)


def test_a_selector_that_matches_nothing_names_what_the_message_holds() -> None:
    with pytest.raises(ImapExtensionError) as excinfo:
        _select(content_type="application/zip")

    message = str(excinfo.value)
    assert excinfo.value.code == "attachment_not_found"
    assert "invoice-2026-09.pdf" in message
    assert "application/pdf" in message


def test_choosing_by_part_path_still_works() -> None:
    assert _select(attachment_id="4").content_type == "application/pdf"


def test_a_call_that_names_no_selector_is_refused() -> None:
    with pytest.raises(ImapExtensionError) as excinfo:
        _select()

    assert "attachment_id, filename or content_type" in str(excinfo.value)


def test_a_call_that_names_two_selectors_is_refused() -> None:
    with pytest.raises(ImapExtensionError) as excinfo:
        _select(filename="invoice-2026-09.pdf", content_type="application/pdf")

    assert "not several" in str(excinfo.value)
