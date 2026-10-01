"""UID-based, read-only mailbox listing and bounded message search operations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from imap_errors import ImapExtensionError
from imap_messages import _parse_headers
from imap_transport import MailboxClient

_MESSAGE_CURSOR_IS_MALFORMED = "cursor is malformed"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
CANDIDATE_WINDOW = 500
MAX_MAILBOX_ROWS = 1_000
MAX_MAILBOX_INPUT_BYTES = 1024 * 1024
MAX_MAILBOX_OUTPUT_BYTES = 1024 * 1024
# The transport rejects any individual FETCH literal above 64 KiB. Keep the
# operation-layer bound explicit so fake/custom clients receive the same error.
MAX_SUMMARY_HEADER_BYTES = 64 * 1024
# A page may contain up to 200 summaries; cap their aggregate header material
# before parsing so bounded literals cannot accumulate into a large response.
MAX_SEARCH_HEADER_BYTES = 1024 * 1024
# Keep search output well below the subprocess-wide response cap in main.py.
MAX_SEARCH_OUTPUT_BYTES = 1024 * 1024
_CURSOR_HMAC_KEY = b"flowsteward.imap-mailbox.cursor.v1"
_STRING_FILTERS = {
    "from": "FROM",
    "to": "TO",
    "cc": "CC",
    "subject": "SUBJECT",
    "text": "TEXT",
}
_BOOLEAN_FILTERS = frozenset({"unseen", "seen", "flagged", "unflagged", "has_attachments"})
_ALLOWED_FILTERS = frozenset(
    {*_STRING_FILTERS, "message_id", "since", "before", *_BOOLEAN_FILTERS, "uid_gte", "uid_lte"}
)
_SPECIAL_USE_FLAGS = {
    b"\\All": "all",
    b"\\Archive": "archive",
    b"\\Drafts": "drafts",
    b"\\Flagged": "flagged",
    b"\\Important": "important",
    b"\\Junk": "junk",
    b"\\Sent": "sent",
    b"\\Trash": "trash",
}
_SUMMARY_FIELDS = (
    "UID",
    "RFC822.SIZE",
    "FLAGS",
    "BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]",
)
_SUMMARY_HEADER_RESPONSE = b"BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]"


@dataclass(frozen=True)
class SearchCursor:
    """Validated high-water mark for one immutable mailbox/query view."""

    mailbox: str
    uidvalidity: int
    last_seen_uid: int
    highest_modseq: int | None = None
    query_fingerprint: str = ""

    def to_opaque(self) -> str:
        payload = {
            "highest_modseq": self.highest_modseq,
            "last_seen_uid": self.last_seen_uid,
            "mailbox": self.mailbox,
            "query_fingerprint": self.query_fingerprint,
            "uidvalidity": self.uidvalidity,
        }
        encoded = _canonical_json(payload)
        signature = hmac.new(_CURSOR_HMAC_KEY, encoded, hashlib.sha256).hexdigest()
        return _encode_cursor({"payload": payload, "signature": signature})

    @classmethod
    def from_opaque(cls, value: object) -> SearchCursor:
        if not isinstance(value, str) or not value:
            _invalid_payload("cursor must be a non-empty opaque value")
        try:
            document = json.loads(_decode_cursor(value))
            payload = document["payload"]
            signature = document["signature"]
        except (KeyError, TypeError, ValueError):
            _invalid_payload(_MESSAGE_CURSOR_IS_MALFORMED)
        if not isinstance(payload, dict) or not isinstance(signature, str):
            _invalid_payload(_MESSAGE_CURSOR_IS_MALFORMED)
        encoded = _canonical_json(payload)
        expected_signature = hmac.new(_CURSOR_HMAC_KEY, encoded, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            _invalid_payload("cursor is invalid")
        mailbox = payload.get("mailbox")
        query_fingerprint = payload.get("query_fingerprint")
        uidvalidity = payload.get("uidvalidity")
        last_seen_uid = payload.get("last_seen_uid")
        highest_modseq = payload.get("highest_modseq")
        if (
            not isinstance(mailbox, str)
            or not mailbox
            or not isinstance(query_fingerprint, str)
            or not query_fingerprint
            or not _positive_int(uidvalidity)
            or not _non_negative_int(last_seen_uid)
            or (highest_modseq is not None and not _positive_int(highest_modseq))
        ):
            _invalid_payload(_MESSAGE_CURSOR_IS_MALFORMED)
        return cls(mailbox, uidvalidity, last_seen_uid, highest_modseq, query_fingerprint)


@dataclass(frozen=True)
class SearchPlan:
    """Safe IMAP SEARCH arguments and bounded local filtering parameters."""

    criteria: Sequence[object]
    limit: int
    candidate_window: int = CANDIDATE_WINDOW
    has_attachments: bool | None = None


def list_mailboxes(client: MailboxClient) -> dict[str, list[dict[str, object]]]:
    """List account folders without selecting or reading any message."""
    mailboxes: list[dict[str, object]] = []
    input_bytes = 0
    output_bytes = 0
    for flags, delimiter, name in client.list_folders():
        if len(mailboxes) >= MAX_MAILBOX_ROWS:
            _mailbox_list_too_large()
        input_bytes += _mailbox_input_bytes(flags, delimiter, name)
        if input_bytes > MAX_MAILBOX_INPUT_BYTES:
            _mailbox_list_too_large()
        decoded_flags = [flag.decode("ascii", errors="replace") for flag in flags]
        special_use = [hint for flag, hint in _SPECIAL_USE_FLAGS.items() if flag in flags]
        mailbox = {
            "name": name,
            "delimiter": delimiter.decode("ascii", errors="replace")
            if delimiter is not None
            else None,
            "flags": decoded_flags,
            "selectable": b"\\Noselect" not in flags,
            "special_use": special_use,
        }
        output_bytes += (
            len(json.dumps(mailbox, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        )
        if output_bytes > MAX_MAILBOX_OUTPUT_BYTES:
            _mailbox_list_too_large()
        mailboxes.append(mailbox)
    return {"mailboxes": sorted(mailboxes, key=lambda mailbox: str(mailbox["name"]))}


def _mailbox_input_bytes(flags: Sequence[bytes], delimiter: bytes | None, name: str) -> int:
    return (
        sum(len(flag) for flag in flags)
        + (len(delimiter) if delimiter is not None else 0)
        + len(name.encode("utf-8", errors="replace"))
    )


def _mailbox_list_too_large() -> None:
    raise ImapExtensionError(
        "mailbox_list_too_large",
        "The IMAP server mailbox list exceeds its safe limit",
    )


def build_search_plan(
    filters: Mapping[str, object] | None,
    cursor: SearchCursor | None,
    mailbox: str,
    limit: object,
) -> SearchPlan:
    """Translate the typed filter object into non-raw IMAP SEARCH criteria."""
    _valid_mailbox(mailbox)
    normalized_filters = _normalized_filters(filters)
    criteria: list[object] = ["ALL"]
    for field, command in _STRING_FILTERS.items():
        if field in normalized_filters:
            criteria.extend((command, _filter_text(normalized_filters[field], field)))
    if "message_id" in normalized_filters:
        criteria.extend(
            ("HEADER", "MESSAGE-ID", _filter_text(normalized_filters["message_id"], "message_id"))
        )
    for field, command in (("since", "SINCE"), ("before", "BEFORE")):
        if field in normalized_filters:
            criteria.extend((command, _iso_date(normalized_filters[field], field)))
    for field, command in (
        ("unseen", "UNSEEN"),
        ("seen", "SEEN"),
        ("flagged", "FLAGGED"),
        ("unflagged", "UNFLAGGED"),
    ):
        if normalized_filters.get(field) is True:
            criteria.append(command)
    uid_range = _uid_range(normalized_filters)
    if uid_range is not None:
        criteria.extend(("UID", uid_range))
    if cursor is not None:
        criteria.extend(("UID", f"{cursor.last_seen_uid + 1}:*"))
    return SearchPlan(
        criteria=tuple(criteria),
        limit=_limit(limit),
        has_attachments=normalized_filters.get("has_attachments"),
    )


def search_messages(
    client: MailboxClient, input_payload: Mapping[str, object]
) -> dict[str, object]:
    """Return UID-ascending summaries with a query-bound opaque high-water cursor."""
    if not isinstance(input_payload, Mapping):
        _invalid_payload("search input must be an object")
    mailbox = input_payload.get("mailbox")
    _valid_mailbox(mailbox)
    filters = input_payload.get("filters", {})
    normalized_filters = _normalized_filters(filters)
    query_fingerprint = _query_fingerprint(mailbox, normalized_filters)
    cursor = _cursor_for_payload(input_payload.get("cursor"), mailbox, query_fingerprint)
    plan = build_search_plan(normalized_filters, cursor, mailbox, input_payload.get("limit"))
    selected = client.select_folder(mailbox, readonly=True)
    uidvalidity = _selected_uidvalidity(selected)
    if cursor is not None and cursor.uidvalidity != uidvalidity:
        raise ImapExtensionError("uidvalidity_changed", "The mailbox UID validity changed")
    highest_modseq = (
        _selected_highest_modseq(selected)
        if {b"CONDSTORE", b"QRESYNC"}.intersection(client.capabilities())
        else None
    )
    candidate_uids = sorted({uid for uid in client.search(plan.criteria) if _positive_int(uid)})
    if cursor is not None:
        candidate_uids = [uid for uid in candidate_uids if uid > cursor.last_seen_uid]
    if plan.has_attachments is None:
        inspected_uids = candidate_uids[: plan.limit]
        truncated = len(candidate_uids) > len(inspected_uids)
    else:
        inspected_uids = candidate_uids[: plan.candidate_window]
        truncated = len(candidate_uids) > len(inspected_uids)
    attachment_metadata = (
        client.fetch(inspected_uids, ("UID", "BODYSTRUCTURE"))
        if inspected_uids and plan.has_attachments is not None
        else {}
    )
    matching_uids = [
        uid
        for uid in inspected_uids
        if plan.has_attachments is None
        or _has_attachments(attachment_metadata.get(uid, {})) == plan.has_attachments
    ]
    result_uids = matching_uids[: plan.limit]
    if len(matching_uids) > len(result_uids):
        truncated = True
    summaries = client.fetch(result_uids, _SUMMARY_FIELDS) if result_uids else {}
    last_seen_uid = _last_seen_uid(result_uids, inspected_uids, cursor)
    next_cursor = SearchCursor(
        mailbox=mailbox,
        uidvalidity=uidvalidity,
        last_seen_uid=last_seen_uid,
        highest_modseq=highest_modseq,
        query_fingerprint=query_fingerprint,
    ).to_opaque()
    result = {
        "messages": _summaries(result_uids, summaries),
        "next_cursor": next_cursor,
        "truncated": truncated,
        "uidvalidity": uidvalidity,
    }
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > (
        MAX_SEARCH_OUTPUT_BYTES
    ):
        _response_too_large()
    return result


def _normalized_filters(filters: Mapping[str, object] | None) -> dict[str, object]:
    if filters is None:
        return {}
    if not isinstance(filters, Mapping) or any(not isinstance(key, str) for key in filters):
        _invalid_payload("filters must be an object")
    unknown = set(filters) - _ALLOWED_FILTERS
    if unknown:
        _invalid_payload("filters contain an unsupported field")
    normalized = dict(filters)
    for field in _BOOLEAN_FILTERS:
        if field in normalized and not isinstance(normalized[field], bool):
            _invalid_payload(f"{field} must be a boolean")
    if normalized.get("seen") is True and normalized.get("unseen") is True:
        _invalid_payload("seen and unseen cannot both be true")
    if normalized.get("flagged") is True and normalized.get("unflagged") is True:
        _invalid_payload("flagged and unflagged cannot both be true")
    return normalized


def _cursor_for_payload(value: object, mailbox: str, query_fingerprint: str) -> SearchCursor | None:
    if value is None:
        return None
    cursor = SearchCursor.from_opaque(value)
    if cursor.mailbox != mailbox or cursor.query_fingerprint != query_fingerprint:
        _invalid_payload("cursor does not match this mailbox search")
    return cursor


def _query_fingerprint(mailbox: str, filters: Mapping[str, object]) -> str:
    return hashlib.sha256(
        _canonical_json({"mailbox": mailbox, "filters": dict(filters)})
    ).hexdigest()


def _summary(uid: int, values: Mapping[bytes, object]) -> dict[str, object]:
    size = values.get(b"RFC822.SIZE")
    flags = values.get(b"FLAGS")
    return {
        "uid": uid,
        "size_bytes": size if _non_negative_int(size) else None,
        "flags": [
            flag.decode("ascii", errors="replace") for flag in flags if isinstance(flag, bytes)
        ]
        if isinstance(flags, tuple)
        else [],
        **_summary_headers(values),
    }


def _summaries(
    result_uids: Sequence[int], fetched: Mapping[int, Mapping[bytes, object]]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    header_bytes = 0
    for uid in result_uids:
        values = fetched.get(uid, {})
        raw_headers = values.get(_SUMMARY_HEADER_RESPONSE)
        if isinstance(raw_headers, bytes):
            if len(raw_headers) > MAX_SUMMARY_HEADER_BYTES:
                _response_too_large()
            header_bytes += len(raw_headers)
            if header_bytes > MAX_SEARCH_HEADER_BYTES:
                _response_too_large()
        rows.append(_summary(uid, values))
    return rows


def _summary_headers(values: Mapping[bytes, object]) -> dict[str, str]:
    raw_headers = values.get(_SUMMARY_HEADER_RESPONSE)
    return _parse_headers(raw_headers if isinstance(raw_headers, bytes) else b"")


def _response_too_large() -> None:
    raise ImapExtensionError(
        "response_too_large",
        "The message search response exceeds its safe output limit",
    )


def _has_attachments(values: Mapping[bytes, object]) -> bool:
    bodystructure = values.get(b"BODYSTRUCTURE")
    return _bodystructure_has_attachment(bodystructure)


def _bodystructure_has_attachment(value: object) -> bool:
    if not isinstance(value, tuple):
        return False
    if _is_attachment_disposition(value) or _has_attachment_filename(value):
        return True
    return any(_bodystructure_has_attachment(item) for item in value)


def _is_attachment_disposition(value: tuple[object, ...]) -> bool:
    return bool(value) and isinstance(value[0], bytes) and value[0].upper() == b"ATTACHMENT"


def _has_attachment_filename(value: tuple[object, ...]) -> bool:
    for index, item in enumerate(value[:-1]):
        if isinstance(item, bytes) and item.upper() in {b"FILENAME", b"NAME"}:
            return isinstance(value[index + 1], bytes) and bool(value[index + 1])
    return False


def _last_seen_uid(
    result_uids: Sequence[int], inspected_uids: Sequence[int], cursor: SearchCursor | None
) -> int:
    if result_uids:
        return result_uids[-1]
    if inspected_uids:
        return inspected_uids[-1]
    return cursor.last_seen_uid if cursor is not None else 0


def _selected_uidvalidity(selected: Mapping[bytes, object]) -> int:
    uidvalidity = selected.get(b"UIDVALIDITY")
    if not _positive_int(uidvalidity):
        raise ImapExtensionError("mailbox_not_found", "The mailbox did not provide UID validity")
    return uidvalidity


def _selected_highest_modseq(selected: Mapping[bytes, object]) -> int | None:
    highest_modseq = selected.get(b"HIGHESTMODSEQ")
    return highest_modseq if _positive_int(highest_modseq) else None


def _uid_range(filters: Mapping[str, object]) -> str | None:
    lower = filters.get("uid_gte")
    upper = filters.get("uid_lte")
    if lower is not None and not _positive_int(lower):
        _invalid_payload("uid_gte must be a positive integer")
    if upper is not None and not _positive_int(upper):
        _invalid_payload("uid_lte must be a positive integer")
    if lower is not None and upper is not None and lower > upper:
        _invalid_payload("uid_gte cannot exceed uid_lte")
    if lower is not None and upper is not None:
        return f"{lower}:{upper}"
    if lower is not None:
        return f"{lower}:*"
    if upper is not None:
        return f"1:{upper}"
    return None


def _iso_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        _invalid_payload(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _invalid_payload(f"{field} must be an ISO date")
    if parsed.isoformat() != value:
        _invalid_payload(f"{field} must be an ISO date")
    return parsed


def _filter_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        _invalid_payload(f"{field} must be a non-empty text value")
    return value


def _limit(value: object) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if not _positive_int(value) or value > MAX_LIMIT:
        _invalid_payload("limit must be an integer from 1 to 200")
    return value


def _valid_mailbox(value: object) -> None:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        _invalid_payload("mailbox must be a non-empty name")


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _encode_cursor(value: Mapping[str, object]) -> str:
    return base64.urlsafe_b64encode(_canonical_json(value)).rstrip(b"=").decode("ascii")


def _decode_cursor(value: str) -> bytes:
    encoded = value.encode("ascii")
    return base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))


def _invalid_payload(message: str) -> None:
    raise ImapExtensionError("invalid_payload", message)
