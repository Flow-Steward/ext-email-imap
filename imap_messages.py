"""Bounded, UID-only message and MIME attachment retrieval."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from typing import Any, NoReturn

from imap_errors import ImapExtensionError
from imap_transport import MAX_FETCH_LITERAL_BYTES, MailboxClient, _canonical_response_selector

MAX_HEADER_BYTES = 256 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_MIME_PARTS = 200
MAX_ATTACHMENTS = 100
_MAX_FILENAME_LENGTH = 512
_MAX_ENCODED_TEXT_BYTES = MAX_TEXT_BYTES * 4
_HEADER_SELECTOR = "BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]"
#: The MIME headers a single-part message carries in its own header, which is
#: what `BODY[<path>.MIME]` would return for a part of a multipart message.
_MESSAGE_MIME_HEADER_SELECTOR = (
    "BODY.PEEK[HEADER.FIELDS (CONTENT-TYPE CONTENT-TRANSFER-ENCODING CONTENT-DISPOSITION)]"
)


@dataclass(frozen=True)
class MimePart:
    """One bounded BODYSTRUCTURE leaf addressed by an IMAP MIME part path."""

    part_path: str
    content_type: str
    charset: str | None
    filename: str | None
    size_bytes: int | None
    transfer_encoding: str | None
    is_attachment: bool
    #: A message that is not multipart has exactly one part, addressed as `1`.
    #: RFC 3501 gives that part no separate MIME header — the message header is
    #: its MIME header — and a server may refuse `BODY[1.MIME]` outright.
    is_message_body: bool = False


@dataclass(frozen=True)
class Attachment:
    """Decoded selected attachment data plus safe response metadata."""

    attachment_id: str
    filename: str
    content_type: str
    data: bytes


def get_message(client: MailboxClient, input_payload: Mapping[str, Any]) -> dict[str, object]:
    """Read selected message fields without setting Seen or fetching attachment parts."""
    mailbox, uid = _mailbox_and_uid(input_payload)
    include_body = _boolean_option(input_payload, "include_body", default=True)
    include_headers = _boolean_option(input_payload, "include_headers", default=True)
    include_attachments = _boolean_option(
        input_payload, "include_attachment_metadata", default=True
    )
    include_raw_html = _raw_html_option(input_payload)

    client.select_folder(mailbox, readonly=True)
    values = _fetch_one(
        client,
        uid,
        ("UID", "RFC822.SIZE", "BODYSTRUCTURE"),
    )
    parts = _mime_parts(values.get(b"BODYSTRUCTURE"))
    raw_headers = _fetch_bounded_literal(
        client,
        uid,
        _HEADER_SELECTOR,
        limit=MAX_HEADER_BYTES,
        description="message headers",
        limit_error=_message_too_large,
    )
    headers = _parse_headers(raw_headers)

    message: dict[str, object] = {
        "mailbox": mailbox,
        "uid": uid,
        **headers,
    }
    if include_headers:
        message["headers"] = dict(headers)
    if include_body:
        message["plain_text"] = _read_text_parts(client, uid, parts, "text/plain")
    if include_raw_html:
        message["raw_html"] = _read_text_parts(client, uid, parts, "text/html")
    if include_attachments:
        attachments = [part for part in parts if part.is_attachment]
        message["attachments"] = [
            _attachment_metadata(part) for part in attachments[:MAX_ATTACHMENTS]
        ]
        message["attachments_truncated"] = len(attachments) > MAX_ATTACHMENTS
    return {"message": message}


#: How a workflow says which attachment it wants. A real message often carries a
#: signature logo and an inline photo beside the file the author actually means,
#: and their part paths are not knowable when the workflow is written.
_ATTACHMENT_SELECTORS = ("attachment_id", "filename", "content_type")


def _attachment_selector(input_payload: Mapping[str, Any]) -> tuple[str, str]:
    """Return the one selector this call uses, or refuse the payload."""
    given = [
        (name, input_payload[name])
        for name in _ATTACHMENT_SELECTORS
        if str(input_payload.get(name) or "").strip()
    ]
    if not given:
        _invalid_payload(
            "one of attachment_id, filename or content_type is required to choose "
            "an attachment"
        )
    if len(given) > 1:
        names = ", ".join(name for name, _ in given)
        _invalid_payload(f"choose an attachment by exactly one of {names}, not several")
    name, raw = given[0]
    value = str(raw).strip()
    if name == "attachment_id" and not _valid_part_path(value):
        _invalid_payload("attachment_id must be a MIME part identifier")
    return name, value


def _attachment_matches(part: MimePart, selector: str, value: str) -> bool:
    if selector == "attachment_id":
        return part.part_path == value
    if selector == "filename":
        return (part.filename or "").lower() == value.lower()
    wanted = value.lower()
    if wanted.endswith("/*"):
        return part.content_type.startswith(f"{wanted[:-1]}")
    return part.content_type == wanted


def _describe_attachments(parts: list[MimePart]) -> str:
    if not parts:
        return "the message has no attachments"
    described = ", ".join(
        f"{part.part_path} ({part.filename or 'unnamed'}, {part.content_type})"
        for part in parts[:MAX_ATTACHMENTS]
    )
    return f"the message has {described}"


def _select_attachment(parts: list[MimePart], selector: tuple[str, str]) -> MimePart:
    name, value = selector
    matches = [part for part in parts if _attachment_matches(part, name, value)]
    if not matches:
        raise ImapExtensionError(
            "attachment_not_found",
            f"No attachment matched {name} '{value}'; {_describe_attachments(parts)}",
        )
    if len(matches) > 1:
        raise ImapExtensionError(
            "attachment_ambiguous",
            f"{len(matches)} attachments matched {name} '{value}': "
            f"{_describe_attachments(matches)}. Narrow the selector, or choose one "
            "by attachment_id.",
        )
    return matches[0]


def load_attachment(
    client: MailboxClient,
    input_payload: Mapping[str, Any],
    *,
    max_size_bytes: int = MAX_ATTACHMENT_BYTES,
    on_download: Callable[[str], None] | None = None,
) -> tuple[str, int, Attachment]:
    """Fetch and decode one attachment, chosen by part path, filename or type.

    ``on_download`` is told the attachment's name just before its body is fetched.
    """
    mailbox, uid = _mailbox_and_uid(input_payload)
    selector = _attachment_selector(input_payload)

    client.select_folder(mailbox, readonly=True)
    values = _fetch_one(client, uid, ("UID", "RFC822.SIZE", "BODYSTRUCTURE"))
    attachments = [part for part in _mime_parts(values.get(b"BODYSTRUCTURE")) if part.is_attachment]
    selected = _select_attachment(attachments, selector)
    effective_limit = min(MAX_ATTACHMENT_BYTES, max_size_bytes)
    encoded_limit = _encoded_attachment_limit(effective_limit, selected.transfer_encoding)
    if selected.size_bytes is not None and selected.size_bytes > encoded_limit:
        _attachment_too_large()

    mime_headers = _fetch_bounded_literal(
        client,
        uid,
        _mime_header_selector(selected),
        limit=MAX_HEADER_BYTES,
        description="attachment metadata",
        limit_error=_message_too_large,
    )
    if on_download is not None:
        on_download(_safe_filename(selected.filename) or "the attachment")
    encoded_body = _fetch_bounded_literal(
        client,
        uid,
        f"BODY.PEEK[{selected.part_path}]",
        limit=encoded_limit,
        description="attachment body",
        limit_error=_attachment_too_large,
    )
    parsed = _parse_mime_section(mime_headers, encoded_body)
    data = _decoded_payload(parsed)
    if len(data) > effective_limit:
        _attachment_too_large()
    filename = _safe_filename(parsed.get_filename() or selected.filename)
    if filename is None:
        raise ImapExtensionError(
            "attachment_type_disallowed",
            "The selected attachment does not have an allowed filename and type",
        )
    content_type = parsed.get_content_type().lower()
    return (
        mailbox,
        uid,
        Attachment(
            attachment_id=selected.part_path,
            filename=filename,
            content_type=content_type,
            data=data,
        ),
    )


def _mailbox_and_uid(input_payload: Mapping[str, Any]) -> tuple[str, int]:
    if not isinstance(input_payload, Mapping):
        _invalid_payload("message input must be an object")
    mailbox = input_payload.get("mailbox")
    uid = input_payload.get("uid")
    if not isinstance(mailbox, str) or not mailbox.strip() or "\r" in mailbox or "\n" in mailbox:
        _invalid_payload("mailbox must be a non-empty name")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        _invalid_payload("uid must be a positive integer")
    return mailbox, uid


def _boolean_option(input_payload: Mapping[str, Any], name: str, *, default: bool) -> bool:
    value = input_payload.get(name, default)
    if not isinstance(value, bool):
        _invalid_payload(f"{name} must be a boolean")
    return value


def _raw_html_option(input_payload: Mapping[str, Any]) -> bool:
    raw_value = input_payload.get("include_raw_html")
    compatibility_value = input_payload.get("include_html")
    if (
        raw_value is not None
        and compatibility_value is not None
        and raw_value != compatibility_value
    ):
        _invalid_payload("HTML inclusion options must agree")
    value = raw_value if raw_value is not None else compatibility_value
    if value is None:
        return False
    if not isinstance(value, bool):
        _invalid_payload("include_raw_html must be a boolean")
    return value


def _fetch_one(
    client: MailboxClient,
    uid: int,
    fields: tuple[str, ...],
) -> Mapping[bytes, object]:
    fetched = client.fetch(uid, fields)
    values = fetched.get(uid)
    if not isinstance(values, Mapping) or values.get(b"UID") != uid:
        raise ImapExtensionError("message_not_found", "The selected message was not found")
    return values


def _required_literal(values: Mapping[bytes, object], selector: str, description: str) -> bytes:
    response_selector = _canonical_response_selector(selector.encode("ascii"))
    value = values.get(response_selector)
    if not isinstance(value, bytes):
        value = next(
            (
                candidate
                for key, candidate in values.items()
                if isinstance(key, bytes) and _canonical_response_selector(key) == response_selector
            ),
            None,
        )
    if not isinstance(value, bytes):
        raise ImapExtensionError(
            "connection_failed",
            f"The IMAP server did not return valid {description}",
        )
    return value


def _fetch_bounded_literal(
    client: MailboxClient,
    uid: int,
    selector: str,
    *,
    limit: int,
    description: str,
    limit_error: Callable[[], NoReturn],
) -> bytes:
    output = bytearray()
    offset = 0
    while True:
        request_count = min(MAX_FETCH_LITERAL_BYTES, limit + 1 - len(output))
        request_selector = f"{selector}<{offset}.{request_count}>"
        values = _fetch_one(client, uid, ("UID", request_selector))
        chunk = _required_literal(values, selector, description)
        if len(chunk) > request_count:
            limit_error()
        output.extend(chunk)
        if len(output) > limit:
            limit_error()
        if len(chunk) < request_count:
            return bytes(output)
        offset += len(chunk)


def _parse_headers(raw_headers: bytes) -> dict[str, str]:
    try:
        parsed = BytesParser(policy=policy.default).parsebytes(raw_headers, headersonly=True)
    except Exception as exc:
        raise ImapExtensionError(
            "connection_failed", "The IMAP server returned invalid message headers"
        ) from exc
    return {
        "date": _header_value(parsed, "Date"),
        "from": _header_value(parsed, "From"),
        "to": _header_value(parsed, "To"),
        "cc": _header_value(parsed, "Cc"),
        "subject": _header_value(parsed, "Subject"),
        "message_id": _header_value(parsed, "Message-ID"),
    }


def _header_value(message: Message, name: str) -> str:
    value = message.get(name)
    return str(value) if value is not None else ""


def _mime_parts(bodystructure: object) -> list[MimePart]:
    if not isinstance(bodystructure, tuple):
        raise ImapExtensionError(
            "connection_failed", "The IMAP server returned invalid MIME metadata"
        )
    parts: list[MimePart] = []
    count = 0

    def visit(value: tuple[object, ...], path: str) -> None:
        nonlocal count
        count += 1
        if count > MAX_MIME_PARTS:
            raise ImapExtensionError(
                "message_too_complex", "The message contains too many MIME parts"
            )
        children = _multipart_children(value)
        if children:
            for index, child in enumerate(children, start=1):
                child_path = f"{path}.{index}" if path else str(index)
                visit(child, child_path)
            return
        if len(value) < 7 or not isinstance(value[0], bytes) or not isinstance(value[1], bytes):
            raise ImapExtensionError(
                "connection_failed", "The IMAP server returned invalid MIME metadata"
            )
        parameters = _parameter_map(value[2])
        disposition, disposition_parameters = _disposition(value)
        filename = _safe_filename(disposition_parameters.get("filename") or parameters.get("name"))
        major = value[0].decode("ascii", errors="replace").lower()
        minor = value[1].decode("ascii", errors="replace").lower()
        parts.append(
            MimePart(
                part_path=path or "1",
                is_message_body=not path,
                content_type=f"{major}/{minor}",
                charset=parameters.get("charset"),
                filename=filename,
                size_bytes=_bodystructure_size(value[6]),
                transfer_encoding=_bodystructure_transfer_encoding(value[5]),
                is_attachment=disposition == "attachment" or filename is not None,
            )
        )

    visit(bodystructure, "")
    return parts


def _multipart_children(value: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
    children: list[tuple[object, ...]] = []
    for item in value:
        if not isinstance(item, tuple):
            break
        children.append(item)
    return tuple(children)


def _parameter_map(value: object) -> dict[str, str]:
    if not isinstance(value, tuple):
        return {}
    result: dict[str, str] = {}
    for index in range(0, len(value) - 1, 2):
        key = value[index]
        item = value[index + 1]
        if isinstance(key, bytes) and isinstance(item, bytes):
            result[key.decode("ascii", errors="ignore").lower()] = item.decode(
                "utf-8", errors="replace"
            )
    return result


def _disposition(value: tuple[object, ...]) -> tuple[str, dict[str, str]]:
    for item in value[7:]:
        if (
            isinstance(item, tuple)
            and item
            and isinstance(item[0], bytes)
            and item[0].upper() in {b"ATTACHMENT", b"INLINE"}
        ):
            parameters = _parameter_map(item[1]) if len(item) > 1 else {}
            return item[0].decode("ascii").lower(), parameters
    return "", {}


def _bodystructure_size(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, bytes) and value.isdigit():
        return int(value)
    return None


def _bodystructure_transfer_encoding(value: object) -> str | None:
    if not isinstance(value, bytes):
        return None
    transfer_encoding = value.decode("ascii", errors="ignore").lower()
    return transfer_encoding or None


def _encoded_attachment_limit(decoded_limit: int, transfer_encoding: str | None) -> int:
    if transfer_encoding == "base64":
        encoded_characters = 4 * ((decoded_limit + 2) // 3)
        folded_lines = (encoded_characters + 75) // 76
        return encoded_characters + 2 * folded_lines
    if transfer_encoding == "quoted-printable":
        return decoded_limit * 4
    return decoded_limit


def _mime_header_selector(part: MimePart) -> str:
    """Return the selector carrying ``part``'s MIME headers.

    For a part of a multipart message that is `BODY[<path>.MIME]`. A message that
    is not multipart has no such section: its own header is the part header, and
    asking for `BODY[1.MIME]` is an error the server is entitled to refuse.
    """
    if part.is_message_body:
        return _MESSAGE_MIME_HEADER_SELECTOR
    return f"BODY.PEEK[{part.part_path}.MIME]"


def _read_text_parts(
    client: MailboxClient,
    uid: int,
    parts: list[MimePart],
    content_type: str,
) -> str:
    chunks: list[str] = []
    total_size = 0
    encoded_size = 0
    mime_header_size = 0
    for part in parts:
        if part.is_attachment or part.content_type != content_type:
            continue
        remaining_encoded = _MAX_ENCODED_TEXT_BYTES - encoded_size
        if part.size_bytes is not None and part.size_bytes > remaining_encoded:
            _message_too_large()
        remaining_mime_headers = MAX_HEADER_BYTES - mime_header_size
        mime_selector = _mime_header_selector(part)
        body_selector = f"BODY.PEEK[{part.part_path}]"
        if remaining_mime_headers <= 0:
            _message_too_large()
        mime_headers = _fetch_bounded_literal(
            client,
            uid,
            mime_selector,
            limit=remaining_mime_headers,
            description="MIME part metadata",
            limit_error=_message_too_large,
        )
        encoded_body = _fetch_bounded_literal(
            client,
            uid,
            body_selector,
            limit=remaining_encoded,
            description="MIME text body",
            limit_error=_message_too_large,
        )
        mime_header_size += len(mime_headers)
        encoded_size += len(encoded_body)
        parsed = _parse_mime_section(mime_headers, encoded_body)
        decoded = _decoded_payload(parsed)
        total_size += len(decoded)
        if total_size > MAX_TEXT_BYTES:
            _message_too_large()
        charset = parsed.get_content_charset() or part.charset or "utf-8"
        try:
            chunks.append(decoded.decode(charset, errors="replace"))
        except LookupError:
            chunks.append(decoded.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _parse_mime_section(mime_headers: bytes, encoded_body: bytes) -> Message:
    raw = mime_headers.rstrip(b"\r\n") + b"\r\n\r\n" + encoded_body
    try:
        return BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise ImapExtensionError(
            "connection_failed", "The IMAP server returned invalid MIME content"
        ) from exc


def _decoded_payload(message: Message) -> bytes:
    try:
        decoded = message.get_payload(decode=True)
    except Exception as exc:
        raise ImapExtensionError(
            "connection_failed", "The IMAP server returned invalid MIME encoding"
        ) from exc
    if isinstance(decoded, bytes):
        return decoded
    payload = message.get_payload()
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    raise ImapExtensionError("connection_failed", "The IMAP server returned invalid MIME content")


def _attachment_metadata(part: MimePart) -> dict[str, object]:
    return {
        "attachment_id": part.part_path,
        "original_filename": part.filename,
        "content_type": part.content_type,
        "size_bytes": part.size_bytes,
    }


def _safe_filename(value: object) -> str | None:
    if isinstance(value, bytes):
        decoded = value.decode("utf-8", errors="replace")
    elif isinstance(value, str):
        decoded = value
    else:
        return None
    normalized = decoded.replace("\x00", "").replace("\r", "").replace("\n", "").strip()
    if not normalized:
        return None
    return normalized[:_MAX_FILENAME_LENGTH]


def _valid_part_path(value: str) -> bool:
    if not value or len(value) > 1024:
        return False
    return all(
        item.isascii() and item.isdigit() and item[0] != "0" and len(item) <= 3
        for item in value.split(".")
    )


def _invalid_payload(message: str) -> None:
    raise ImapExtensionError("invalid_payload", message)


def _message_too_large() -> NoReturn:
    raise ImapExtensionError("message_too_large", "The selected message content exceeds its limit")


def _attachment_too_large() -> NoReturn:
    raise ImapExtensionError("attachment_too_large", "The selected attachment exceeds 20 MiB")
