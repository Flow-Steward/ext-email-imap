from __future__ import annotations

import base64
import json
import sys
from datetime import date
from pathlib import Path

import pytest

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SDK_PARENT = Path(__file__).resolve().parents[3] / "core/infrastructure/extension_sdk/public"
for import_root in (BUNDLE_ROOT, SDK_PARENT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from fake_mailbox_client import FakeMailboxClient  # noqa: E402
from imap_errors import ImapExtensionError  # noqa: E402
from imap_search import (  # noqa: E402
    CANDIDATE_WINDOW,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SearchCursor,
    build_search_plan,
    list_mailboxes,
    search_messages,
)
from imap_transport import _parse_fetch_rows  # noqa: E402

_EXPECTED_MAX_SUMMARY_HEADER_BYTES = 64 * 1024
_EXPECTED_MAX_SEARCH_HEADER_BYTES = 1024 * 1024


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"mailbox": "INBOX", "filters": {}}
    payload.update(overrides)
    return payload


def _cursor_payload(cursor: str, **overrides: object) -> dict[str, object]:
    return _payload(cursor=cursor, **overrides)


def _raw_summary_headers(subject: bytes = b"Invoice") -> bytes:
    return (
        b"Date: Fri, 21 Aug 2026 09:30:00 +0000\r\n"
        b"From: Billing <billing@example.test>\r\n"
        b"To: AP <ap@example.test>\r\n"
        b"Cc: Audit <audit@example.test>\r\n"
        b"Subject: " + subject + b"\r\nMessage-ID: <invoice@example.test>\r\n\r\n"
    )


def test_lists_mailboxes_with_selectability_delimiter_and_special_use_hints() -> None:
    client = FakeMailboxClient(
        folders=[
            ((b"\\HasNoChildren", b"\\Sent"), b"/", "Sent"),
            ((b"\\Noselect",), None, "Projects"),
            ((b"\\HasChildren", b"\\Trash"), b"/", "Trash"),
        ]
    )

    result = list_mailboxes(client)

    assert result == {
        "mailboxes": [
            {
                "name": "Projects",
                "delimiter": None,
                "flags": ["\\Noselect"],
                "selectable": False,
                "special_use": [],
            },
            {
                "name": "Sent",
                "delimiter": "/",
                "flags": ["\\HasNoChildren", "\\Sent"],
                "selectable": True,
                "special_use": ["sent"],
            },
            {
                "name": "Trash",
                "delimiter": "/",
                "flags": ["\\HasChildren", "\\Trash"],
                "selectable": True,
                "special_use": ["trash"],
            },
        ]
    }


def test_list_mailboxes_stops_at_the_row_budget_before_sorting_all_rows() -> None:
    class TooManyFolders:
        yielded = 0

        def list_folders(self):
            for index in range(1_010):
                self.yielded += 1
                yield ((b"\\HasNoChildren",), b"/", f"Folder-{index:04d}")

    client = TooManyFolders()

    with pytest.raises(ImapExtensionError) as error:
        list_mailboxes(client)  # type: ignore[arg-type]

    assert error.value.code == "mailbox_list_too_large"
    assert client.yielded < 1_010


def test_list_mailboxes_bounds_aggregate_input_before_materializing_all_rows() -> None:
    class OversizedFolders:
        yielded = 0

        def list_folders(self):
            name = "é" * 6_000
            for _index in range(100):
                self.yielded += 1
                yield ((b"\\HasNoChildren",), b"/", name)

    client = OversizedFolders()

    with pytest.raises(ImapExtensionError) as error:
        list_mailboxes(client)  # type: ignore[arg-type]

    assert error.value.code == "mailbox_list_too_large"
    assert client.yielded < 100


def test_list_mailboxes_bounds_aggregate_json_output_bytes() -> None:
    client = FakeMailboxClient(folders=[((b"\\HasNoChildren",), b"/", '"' * (600 * 1024))])

    with pytest.raises(ImapExtensionError) as error:
        list_mailboxes(client)

    assert error.value.code == "mailbox_list_too_large"


@pytest.mark.parametrize(
    ("filters", "criteria"),
    [
        ({"from": "a@example.test"}, ("FROM", "a@example.test")),
        ({"to": "a@example.test"}, ("TO", "a@example.test")),
        ({"cc": "a@example.test"}, ("CC", "a@example.test")),
        ({"subject": 'invoice "august"'}, ("SUBJECT", 'invoice "august"')),
        ({"text": "approved"}, ("TEXT", "approved")),
        ({"message_id": "<a@example.test>"}, ("HEADER", "MESSAGE-ID", "<a@example.test>")),
        ({"since": "2026-08-01"}, ("SINCE", date(2026, 8, 1))),
        ({"before": "2026-08-31"}, ("BEFORE", date(2026, 8, 31))),
        ({"unseen": True}, ("UNSEEN",)),
        ({"seen": True}, ("SEEN",)),
        ({"flagged": True}, ("FLAGGED",)),
        ({"unflagged": True}, ("UNFLAGGED",)),
        ({"uid_gte": 7}, ("UID", "7:*")),
        ({"uid_lte": 7}, ("UID", "1:7")),
        ({"uid_gte": 7, "uid_lte": 9}, ("UID", "7:9")),
    ],
)
def test_builds_structured_criteria_for_each_supported_server_filter(
    filters: dict[str, object], criteria: tuple[object, ...]
) -> None:
    plan = build_search_plan(filters, None, "INBOX", DEFAULT_LIMIT)

    assert plan.criteria == ("ALL", *criteria)
    assert all(not (isinstance(item, str) and "SEARCH" in item) for item in plan.criteria)


@pytest.mark.parametrize("value", ["2026-2-01", "2026-02-30", "not-a-date", 4])
def test_rejects_non_iso_or_impossible_date_filters(value: object) -> None:
    with pytest.raises(ImapExtensionError) as error:
        build_search_plan({"since": value}, None, "INBOX", DEFAULT_LIMIT)

    assert error.value.code == "invalid_payload"


@pytest.mark.parametrize(
    "filters",
    [
        {"nope": True},
        {"seen": True, "unseen": True},
        {"flagged": True, "unflagged": True},
        {"uid_gte": 9, "uid_lte": 7},
    ],
)
def test_rejects_unknown_or_contradictory_filters(filters: dict[str, object]) -> None:
    with pytest.raises(ImapExtensionError) as error:
        build_search_plan(filters, None, "INBOX", DEFAULT_LIMIT)

    assert error.value.code == "invalid_payload"


@pytest.mark.parametrize("limit", [0, -1, True, 201, "50"])
def test_rejects_limits_outside_one_through_two_hundred(limit: object) -> None:
    with pytest.raises(ImapExtensionError) as error:
        build_search_plan({}, None, "INBOX", limit)

    assert error.value.code == "invalid_payload"


def test_search_is_read_only_uid_ordered_and_has_a_query_bound_opaque_cursor() -> None:
    client = FakeMailboxClient(uidvalidity=42, uids=[8, 2, 5, 1])

    result = search_messages(client, _payload(limit=2, filters={"subject": "invoice"}))

    assert [row["uid"] for row in result["messages"]] == [1, 2]
    assert client.selected_folders == [("INBOX", True)]
    assert client.fetches == [
        (
            [1, 2],
            (
                "UID",
                "RFC822.SIZE",
                "FLAGS",
                "BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]",
            ),
        )
    ]
    assert client.mutation_calls == []
    assert result["truncated"] is True
    assert isinstance(result["next_cursor"], str)
    assert "cursor" not in result

    next_result = search_messages(
        client, _cursor_payload(result["next_cursor"], limit=2, filters={"subject": "invoice"})
    )

    assert [row["uid"] for row in next_result["messages"]] == [5, 8]
    assert next_result["truncated"] is False


def test_search_returns_decoded_summary_headers_without_marking_messages_seen() -> None:
    client = FakeMailboxClient(
        uids=[7],
        message_sections={
            7: {
                "BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]": (
                    b"Date: Fri, 21 Aug 2026 09:30:00 +0000\r\n"
                    b"From: =?utf-8?q?S=C4=85skaitos?= <billing@example.test>\r\n"
                    b"To: AP <ap@example.test>\r\n"
                    b"Cc: Audit <audit@example.test>\r\n"
                    b"Subject: August =?utf-8?b?4oKs?= invoice\r\n"
                    b"Message-ID: <invoice-7@example.test>\r\n\r\n"
                )
            }
        },
    )

    result = search_messages(client, _payload())

    assert result["messages"] == [
        {
            "uid": 7,
            "size_bytes": 70,
            "flags": [],
            "date": "Fri, 21 Aug 2026 09:30:00 +0000",
            "from": "Sąskaitos <billing@example.test>",
            "to": "AP <ap@example.test>",
            "cc": "Audit <audit@example.test>",
            "subject": "August € invoice",
            "message_id": "<invoice-7@example.test>",
        }
    ]
    assert client.fetches == [
        (
            [7],
            (
                "UID",
                "RFC822.SIZE",
                "FLAGS",
                "BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]",
            ),
        )
    ]
    assert client.flags_by_uid[7] == set()
    assert client.mutation_calls == []


def test_search_uses_uids_and_advances_over_attachment_candidates() -> None:
    client = FakeMailboxClient(
        uidvalidity=42,
        uids=range(1, 701),
        attachments={500},
        message_sections={
            500: {
                "BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]": (_raw_summary_headers())
            }
        },
    )
    client.flags_by_uid[500] = set()

    result = search_messages(client, _payload(filters={"has_attachments": True}))

    assert result["messages"] == [
        {
            "uid": 500,
            "size_bytes": 5000,
            "flags": [],
            "date": "Fri, 21 Aug 2026 09:30:00 +0000",
            "from": "Billing <billing@example.test>",
            "to": "AP <ap@example.test>",
            "cc": "Audit <audit@example.test>",
            "subject": "Invoice",
            "message_id": "<invoice@example.test>",
        }
    ]
    assert result["truncated"] is True
    assert client.fetches == [
        (
            list(range(1, CANDIDATE_WINDOW + 1)),
            ("UID", "BODYSTRUCTURE"),
        ),
        (
            [500],
            (
                "UID",
                "RFC822.SIZE",
                "FLAGS",
                "BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]",
            ),
        ),
    ]
    assert client.flags_by_uid[500] == set()
    assert client.mutation_calls == []
    assert SearchCursor.from_opaque(result["next_cursor"]).last_seen_uid == CANDIDATE_WINDOW


def test_search_rejects_one_oversized_summary_header_literal() -> None:
    raw_headers = b"Subject: " + b"x" * _EXPECTED_MAX_SUMMARY_HEADER_BYTES + b"\r\n\r\n"
    client = FakeMailboxClient(
        uids=[7],
        message_sections={
            7: {"BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]": raw_headers}
        },
    )

    _raises_input_335_1 = _payload()
    with pytest.raises(ImapExtensionError) as error:
        search_messages(client, _raises_input_335_1)

    assert error.value.code == "response_too_large"


def test_search_rejects_aggregate_summary_headers_over_the_budget() -> None:
    raw_headers = b"Subject: " + b"x" * (_EXPECTED_MAX_SUMMARY_HEADER_BYTES - 128) + b"\r\n\r\n"
    row_count = _EXPECTED_MAX_SEARCH_HEADER_BYTES // len(raw_headers) + 1
    client = FakeMailboxClient(
        uids=range(1, row_count + 1),
        message_sections={
            uid: {"BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]": raw_headers}
            for uid in range(1, row_count + 1)
        },
    )

    _raises_input_352_1 = _payload(limit=row_count)
    with pytest.raises(ImapExtensionError) as error:
        search_messages(client, _raises_input_352_1)

    assert error.value.code == "response_too_large"


def test_search_bounds_the_serialized_operation_result_below_the_process_cap() -> None:
    raw_headers = b"Subject: " + b"x" * (_EXPECTED_MAX_SUMMARY_HEADER_BYTES - 128) + b"\r\n\r\n"
    row_count = _EXPECTED_MAX_SEARCH_HEADER_BYTES // len(raw_headers)
    assert row_count * len(raw_headers) <= _EXPECTED_MAX_SEARCH_HEADER_BYTES
    client = FakeMailboxClient(
        uids=range(1, row_count + 1),
        message_sections={
            uid: {"BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]": raw_headers}
            for uid in range(1, row_count + 1)
        },
    )

    _raises_input_370_1 = _payload(limit=row_count)
    with pytest.raises(ImapExtensionError) as error:
        search_messages(client, _raises_input_370_1)

    assert error.value.code == "response_too_large"


def test_search_supports_non_ascii_mailbox_names_without_mutating_messages() -> None:
    client = FakeMailboxClient(folders=[((b"\\HasNoChildren",), b"/", "Projekti/Žinutės")])

    result = search_messages(client, _payload(mailbox="Projekti/Žinutės"))

    assert client.selected_folders == [("Projekti/Žinutės", True)]
    assert [row["uid"] for row in result["messages"]] == [1, 2, 3]
    assert client.mutation_calls == []


def test_cursor_uidvalidity_change_fails_closed() -> None:
    prior_cursor = search_messages(FakeMailboxClient(uidvalidity=7), _payload())["next_cursor"]

    _raises_input_389_1 = FakeMailboxClient(uidvalidity=8)
    _raises_input_389_2 = _cursor_payload(prior_cursor)
    with pytest.raises(ImapExtensionError) as error:
        search_messages(_raises_input_389_1, _raises_input_389_2)

    assert error.value.code == "uidvalidity_changed"


def test_empty_follow_up_page_keeps_the_prior_cursor_high_water_mark() -> None:
    first_page = search_messages(FakeMailboxClient(uidvalidity=42, uids=[4]), _payload())

    follow_up = search_messages(
        FakeMailboxClient(uidvalidity=42, uids=[]), _cursor_payload(first_page["next_cursor"])
    )

    assert follow_up["messages"] == []
    assert SearchCursor.from_opaque(follow_up["next_cursor"]).last_seen_uid == 4


def test_attachment_filter_uses_real_bodystructure_markers_not_any_multipart() -> None:
    parsed = _parse_fetch_rows(
        [
            b"* 1 FETCH (UID 1 RFC822.SIZE 90 FLAGS () BODYSTRUCTURE "
            b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 10 1 NIL NIL NIL) '
            b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "QUOTED-PRINTABLE" 20 1 NIL NIL NIL) '
            b'"ALTERNATIVE" ("BOUNDARY" "part") NIL NIL))',
            b"* 2 FETCH (UID 2 RFC822.SIZE 200 FLAGS (\\Seen) BODYSTRUCTURE "
            b'("APPLICATION" "PDF" ("NAME" "invoice.pdf") NIL NIL "BASE64" 100 NIL '
            b'("ATTACHMENT" ("FILENAME" "invoice.pdf")) NIL NIL))',
        ]
    )
    client = FakeMailboxClient(
        uids=[1, 2],
        bodystructures={
            1: parsed[1][b"BODYSTRUCTURE"],
            2: parsed[2][b"BODYSTRUCTURE"],
        },
    )

    result = search_messages(client, _payload(filters={"has_attachments": True}))

    assert [row["uid"] for row in result["messages"]] == [2]


@pytest.mark.parametrize("cursor", ["not-a-cursor", "", "eyJub3BlIjp0cnVlfQ"])
def test_rejects_malformed_cursor(cursor: str) -> None:
    _raises_input_433_1 = FakeMailboxClient()
    _raises_input_433_2 = _cursor_payload(cursor)
    with pytest.raises(ImapExtensionError) as error:
        search_messages(_raises_input_433_1, _raises_input_433_2)

    assert error.value.code == "invalid_payload"


def test_rejects_tampered_or_query_mismatched_cursor() -> None:
    cursor = search_messages(FakeMailboxClient(), _payload(filters={"subject": "invoice"}))[
        "next_cursor"
    ]
    encoded = base64.urlsafe_b64decode(cursor.encode("ascii") + b"=")
    payload = json.loads(encoded)
    payload["payload"]["last_seen_uid"] = 1
    tampered = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )

    _raises_input_454_1 = FakeMailboxClient()
    _raises_input_454_2 = _cursor_payload(tampered, filters={"subject": "invoice"})
    with pytest.raises(ImapExtensionError) as tampered_error:
        search_messages(_raises_input_454_1, _raises_input_454_2)
    _raises_input_458_1 = FakeMailboxClient()
    _raises_input_458_2 = _cursor_payload(cursor, filters={"subject": "receipt"})
    with pytest.raises(ImapExtensionError) as query_error:
        search_messages(_raises_input_458_1, _raises_input_458_2)

    assert tampered_error.value.code == "invalid_payload"
    assert query_error.value.code == "invalid_payload"


def test_cursor_mailbox_mismatch_fails_closed() -> None:
    cursor = search_messages(FakeMailboxClient(), _payload())["next_cursor"]

    _raises_input_470_1 = FakeMailboxClient(folders=[((b"\\HasNoChildren",), b"/", "Archive")])
    _raises_input_470_2 = _cursor_payload(cursor, mailbox="Archive")
    with pytest.raises(ImapExtensionError) as error:
        search_messages(_raises_input_470_1, _raises_input_470_2)

    assert error.value.code == "invalid_payload"


def test_highest_modseq_is_included_only_when_advertised_by_mailbox_selection() -> None:
    cursor = search_messages(
        FakeMailboxClient(highest_modseq=71, capability_values=frozenset({b"CONDSTORE"})),
        _payload(),
    )["next_cursor"]

    decoded = SearchCursor.from_opaque(cursor)

    assert decoded.highest_modseq == 71
    assert decoded.uidvalidity == 42
    assert decoded.last_seen_uid == 3


def test_highest_modseq_is_omitted_without_condstore_or_qresync() -> None:
    cursor = search_messages(FakeMailboxClient(highest_modseq=71), _payload())["next_cursor"]

    assert SearchCursor.from_opaque(cursor).highest_modseq is None


def test_default_and_maximum_limits_are_explicit() -> None:
    assert build_search_plan({}, None, "INBOX", None).limit == DEFAULT_LIMIT
    assert build_search_plan({}, None, "INBOX", MAX_LIMIT).limit == MAX_LIMIT
