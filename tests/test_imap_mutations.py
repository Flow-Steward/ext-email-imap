from __future__ import annotations

import imaplib
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SDK_PARENT = Path(__file__).resolve().parents[3] / "core/infrastructure/extension_sdk/public"
for import_root in (BUNDLE_ROOT, SDK_PARENT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from fake_mailbox_client import FakeMailboxClient  # noqa: E402
from imap_config import ImapConnectionConfig  # noqa: E402
from imap_errors import ImapExtensionError  # noqa: E402
from imap_mutations import (  # noqa: E402
    delete_messages,
    dispatch_mutation,
    move_messages,
    set_message_flags,
)
from imap_transport import _StdlibMailboxClient  # noqa: E402


def _config(*, trash_mailbox: str = "") -> ImapConnectionConfig:
    return ImapConnectionConfig(
        provider_preset="manual",
        imap_host="imap.example.test",
        imap_port=993,
        tls_mode="ssl",
        username="mailbox@example.test",
        password="secret-password",
        trash_mailbox=trash_mailbox,
    )


def _folders() -> list[tuple[tuple[bytes, ...], bytes | None, str]]:
    return [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren",), b"/", "Processed"),
        ((b"\\HasNoChildren", b"\\Trash"), b"/", "Trash"),
    ]


def _client(
    *,
    capabilities: set[bytes] | None = None,
    uids: list[int] | None = None,
    failures: dict[tuple[str, int], Exception] | None = None,
) -> FakeMailboxClient:
    return FakeMailboxClient(
        uids=uids or [3, 7, 9],
        capability_values=frozenset(capabilities or {b"IMAP4REV1", b"MOVE", b"UIDPLUS"}),
        folders=_folders(),
        mailbox_uids={"INBOX": set(uids or [3, 7, 9]), "Processed": set(), "Trash": set()},
        mutation_failures=failures or {},
    )


class _TaggedNoMoveConnection:
    capabilities = ("IMAP4REV1", "MOVE")

    def __init__(self) -> None:
        self.untagged_responses = {"UIDVALIDITY": [b"42"]}
        self.uid_calls: list[tuple[str, tuple[object, ...]]] = []

    def select(self, _mailbox: bytes, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly is False
        return "OK", [b"2"]

    def uid(self, command: str, *arguments: object) -> tuple[str, list[bytes]]:
        self.uid_calls.append((command, arguments))
        return "NO", [b"MOVE failed after a possible partial move"]


class _CancellationSignal(BaseException):
    pass


def test_set_flags_deduplicates_sorts_and_sets_seen_and_flagged_per_uid() -> None:
    client = _client(uids=[9, 3, 7])

    result = set_message_flags(
        client,
        _config(),
        {"mailbox": "INBOX", "uids": [9, 3, 9, 7], "seen": True, "flagged": False},
    )

    assert result == {
        "results": [
            {"uid": 3, "status": "succeeded"},
            {"uid": 7, "status": "succeeded"},
            {"uid": 9, "status": "succeeded"},
        ],
        "external_effect_status": "succeeded",
    }
    assert client.flags_by_uid == {
        3: {"\\Seen"},
        7: {"\\Seen"},
        9: {"\\Seen"},
    }
    assert client.mutation_details == [
        ("STORE_ADD", 3, ("\\Seen",)),
        ("STORE_REMOVE", 3, ("\\Flagged",)),
        ("STORE_ADD", 7, ("\\Seen",)),
        ("STORE_REMOVE", 7, ("\\Flagged",)),
        ("STORE_ADD", 9, ("\\Seen",)),
        ("STORE_REMOVE", 9, ("\\Flagged",)),
    ]


def test_set_flags_can_mark_unread_and_flagged() -> None:
    client = _client(uids=[7])
    client.flags_by_uid[7] = {"\\Seen"}

    result = set_message_flags(
        client,
        _config(),
        {"mailbox": "INBOX", "uids": [7], "seen": False, "flagged": True},
    )

    assert result["external_effect_status"] == "succeeded"
    assert client.flags_by_uid[7] == {"\\Flagged"}


@pytest.mark.parametrize(
    "uids",
    [None, [], [True], [0], [-1], ["7"], list(range(1, 102))],
)
def test_mutations_reject_invalid_uid_batches_before_select_or_store(uids: object) -> None:
    client = _client()

    _raises_input_139_1 = _config()
    with pytest.raises(ImapExtensionError) as error:
        set_message_flags(
            client, _raises_input_139_1, {"mailbox": "INBOX", "uids": uids, "seen": True}
        )

    assert error.value.code == "invalid_payload"
    assert client.selected_folders == []
    assert client.mutation_calls == []


@pytest.mark.parametrize(
    "updates",
    [{}, {"seen": 1}, {"flagged": "yes"}, {"seen": None, "flagged": None}],
)
def test_set_flags_requires_at_least_one_strict_boolean(updates: dict[str, object]) -> None:
    client = _client()

    _raises_input_158_1 = _config()
    with pytest.raises(ImapExtensionError) as error:
        set_message_flags(client, _raises_input_158_1, {"mailbox": "INBOX", "uids": [7], **updates})

    assert error.value.code == "invalid_payload"
    assert client.mutation_calls == []


def test_move_prefers_uid_move_and_changes_fake_mailbox_state() -> None:
    client = _client(uids=[3, 7])

    result = move_messages(
        client,
        _config(),
        {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [7, 3]},
    )

    assert result["external_effect_status"] == "succeeded"
    assert client.mailbox_uids["INBOX"] == set()
    assert client.mailbox_uids["Processed"] == {3, 7}
    assert client.mutation_details == [("MOVE", 3, "Processed"), ("MOVE", 7, "Processed")]
    assert "EXPUNGE" not in client.mutation_calls
    assert "CLOSE" not in client.mutation_calls


def test_move_copy_delete_fallback_uses_only_uid_scoped_expunge() -> None:
    client = _client(capabilities={b"IMAP4REV1", b"UIDPLUS"}, uids=[7, 99])
    client.flags_by_uid[99] = {"\\Deleted"}

    result = move_messages(
        client,
        _config(),
        {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [7]},
    )

    assert result["external_effect_status"] == "succeeded"
    assert client.mailbox_uids["INBOX"] == {99}
    assert client.mailbox_uids["Processed"] == {7}
    assert "\\Deleted" not in client.flags_by_uid[7]
    assert client.flags_by_uid[99] == {"\\Deleted"}
    assert client.mutation_details == [
        ("COPY", 7, "Processed"),
        ("STORE_ADD", 7, ("\\Deleted",)),
        ("UID_EXPUNGE", 7),
    ]
    assert "CLOSE" not in client.mutation_calls


def test_move_fallback_without_uidplus_fails_before_copy_or_store() -> None:
    client = _client(capabilities={b"IMAP4REV1"}, uids=[7])

    _raises_input_208_1 = _config()
    with pytest.raises(ImapExtensionError) as error:
        move_messages(
            client,
            _raises_input_208_1,
            {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [7]},
        )

    assert error.value.code == "unsafe_expunge_unsupported"
    assert error.value.definitely_no_external_effect is True
    assert client.mutation_calls == []


def test_move_rejects_same_folder_before_select() -> None:
    client = _client(uids=[7])

    _raises_input_223_1 = _config()
    with pytest.raises(ImapExtensionError) as error:
        move_messages(
            client,
            _raises_input_223_1,
            {"source_mailbox": "INBOX", "target_mailbox": "INBOX", "uids": [7]},
        )

    assert error.value.code == "invalid_payload"
    assert client.selected_folders == []


def test_delete_defaults_to_configured_trash_mailbox() -> None:
    client = _client(uids=[7])

    result = delete_messages(
        client,
        _config(trash_mailbox="Trash"),
        {"mailbox": "INBOX", "uids": [7]},
    )

    assert result["external_effect_status"] == "succeeded"
    assert client.mailbox_uids["INBOX"] == set()
    assert client.mailbox_uids["Trash"] == {7}
    assert client.mutation_details == [("MOVE", 7, "Trash")]


def test_delete_resolves_one_selectable_special_use_trash_mailbox() -> None:
    client = _client(uids=[7])

    result = delete_messages(client, _config(), {"mailbox": "INBOX", "uids": [7]})

    assert result["external_effect_status"] == "succeeded"
    assert client.mailbox_uids["Trash"] == {7}


@pytest.mark.parametrize(
    "folders",
    [
        [((b"\\HasNoChildren",), b"/", "INBOX")],
        [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\Trash",), b"/", "Trash A"),
            ((b"\\Trash",), b"/", "Trash B"),
        ],
        [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\Trash", b"\\Noselect"), b"/", "Trash"),
        ],
    ],
)
def test_delete_fails_closed_without_one_reliable_trash_mailbox(
    folders: list[tuple[tuple[bytes, ...], bytes | None, str]],
) -> None:
    client = _client(uids=[7])
    client.folders = folders

    _raises_input_279_1 = _config()
    with pytest.raises(ImapExtensionError) as error:
        delete_messages(client, _raises_input_279_1, {"mailbox": "INBOX", "uids": [7]})

    assert error.value.code == "trash_mailbox_unavailable"
    assert client.mutation_calls == []


def test_permanent_delete_requires_explicit_confirmation_before_store() -> None:
    client = _client(uids=[7])

    _raises_input_289_1 = _config()
    with pytest.raises(ImapExtensionError) as error:
        delete_messages(
            client, _raises_input_289_1, {"mailbox": "INBOX", "uids": [7], "mode": "permanent"}
        )

    assert error.value.code == "invalid_payload"
    assert client.mutation_calls == []


def test_permanent_delete_without_uid_expunge_fails_without_global_expunge() -> None:
    client = _client(capabilities={b"IMAP4REV1"}, uids=[7])

    _raises_input_303_1 = _config()
    with pytest.raises(ImapExtensionError) as error:
        delete_messages(
            client,
            _raises_input_303_1,
            {
                "mailbox": "INBOX",
                "uids": [7],
                "mode": "permanent",
                "confirm_permanent_delete": True,
            },
        )

    assert error.value.code == "unsafe_expunge_unsupported"
    assert client.mutation_calls == []


def test_permanent_delete_marks_and_uid_expunges_only_selected_uid() -> None:
    client = _client(capabilities={b"IMAP4REV1", b"UIDPLUS"}, uids=[7, 99])
    client.flags_by_uid[99] = {"\\Deleted"}

    result = delete_messages(
        client,
        _config(),
        {
            "mailbox": "INBOX",
            "uids": [7],
            "mode": "permanent",
            "confirm_permanent_delete": True,
        },
    )

    assert result["external_effect_status"] == "succeeded"
    assert client.mailbox_uids["INBOX"] == {99}
    assert client.flags_by_uid[99] == {"\\Deleted"}
    assert client.mutation_details == [
        ("STORE_ADD", 7, ("\\Deleted",)),
        ("UID_EXPUNGE", 7),
    ]


def test_real_transport_uid_move_tagged_no_is_ambiguous_and_stops_batch() -> None:
    connection = _TaggedNoMoveConnection()
    client = _StdlibMailboxClient(cast(Any, connection))

    result = move_messages(
        client,
        _config(),
        {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [3, 7]},
    )

    assert result == {
        "results": [
            {"uid": 3, "status": "timeout_unknown", "error_code": "timeout_unknown"},
            {
                "uid": 7,
                "status": "failed",
                "error_code": "not_attempted_after_timeout_unknown",
            },
        ],
        "external_effect_status": "timeout_unknown",
        "definitely_no_external_effect": False,
    }
    assert connection.uid_calls == [("MOVE", ("3", b'"Processed"'))]


@pytest.mark.parametrize("move_error", [RuntimeError("rejected"), ValueError("bad response")])
def test_alternate_client_move_exception_is_ambiguous_and_stops_batch(
    move_error: Exception,
) -> None:
    client = _client(uids=[3, 7], failures={("MOVE", 3): move_error})

    result = move_messages(
        client,
        _config(),
        {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [3, 7]},
    )

    assert result == {
        "results": [
            {"uid": 3, "status": "timeout_unknown", "error_code": "timeout_unknown"},
            {
                "uid": 7,
                "status": "failed",
                "error_code": "not_attempted_after_timeout_unknown",
            },
        ],
        "external_effect_status": "timeout_unknown",
        "definitely_no_external_effect": False,
    }
    assert client.mutation_details == [("MOVE", 3, "Processed")]


def test_move_does_not_swallow_cancellation_signal() -> None:
    client = _client(uids=[3, 7])
    client.mutation_failures[("MOVE", 3)] = cast(Any, _CancellationSignal())

    _raises_input_399_1 = _config()
    with pytest.raises(_CancellationSignal):
        move_messages(
            client,
            _raises_input_399_1,
            {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [3, 7]},
        )

    assert client.mutation_details == [("MOVE", 3, "Processed")]


def test_tagged_flag_failures_continue_per_uid_and_all_failed_is_definitive() -> None:
    client = _client(
        uids=[3, 7],
        failures={
            ("STORE_ADD", 3): imaplib.IMAP4.error("rejected"),
            ("STORE_ADD", 7): imaplib.IMAP4.error("rejected"),
        },
    )

    result = set_message_flags(
        client,
        _config(),
        {"mailbox": "INBOX", "uids": [3, 7], "seen": True},
    )

    assert result == {
        "results": [
            {"uid": 3, "status": "failed", "error_code": "mutation_failed"},
            {"uid": 7, "status": "failed", "error_code": "mutation_failed"},
        ],
        "external_effect_status": "failed",
        "definitely_no_external_effect": True,
    }
    assert client.mailbox_uids["INBOX"] == {3, 7}


def test_prior_success_then_move_tagged_no_remains_ambiguous() -> None:
    client = _client(
        uids=[3, 7],
        failures={("MOVE", 7): imaplib.IMAP4.error("rejected")},
    )

    result = move_messages(
        client,
        _config(),
        {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [3, 7]},
    )

    assert result["results"] == [
        {"uid": 3, "status": "succeeded"},
        {"uid": 7, "status": "timeout_unknown", "error_code": "timeout_unknown"},
    ]
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False


def test_timeout_stops_batch_without_retry_and_marks_remaining_not_attempted() -> None:
    timeout = TimeoutError("disconnect after command write")
    client = _client(uids=[3, 7, 9], failures={("MOVE", 7): timeout})

    result = move_messages(
        client,
        _config(),
        {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [3, 7, 9]},
    )

    assert result == {
        "results": [
            {"uid": 3, "status": "succeeded"},
            {"uid": 7, "status": "timeout_unknown", "error_code": "timeout_unknown"},
            {
                "uid": 9,
                "status": "failed",
                "error_code": "not_attempted_after_timeout_unknown",
            },
        ],
        "external_effect_status": "timeout_unknown",
        "definitely_no_external_effect": False,
    }
    assert client.mutation_details == [("MOVE", 3, "Processed"), ("MOVE", 7, "Processed")]


def test_client_error_with_ambiguous_effect_metadata_stops_without_retry() -> None:
    ambiguous = ImapExtensionError(
        "provider_throttled",
        "The provider disconnected",
        external_effect_status="timeout_unknown",
        definitely_no_external_effect=False,
    )
    client = _client(uids=[7, 9], failures={("MOVE", 7): ambiguous})

    result = move_messages(
        client,
        _config(),
        {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [7, 9]},
    )

    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert result["results"][0] == {
        "uid": 7,
        "status": "timeout_unknown",
        "error_code": "timeout_unknown",
    }
    assert client.mutation_details == [("MOVE", 7, "Processed")]


def test_copy_delete_partial_failure_is_timeout_unknown_and_not_retried() -> None:
    client = _client(
        capabilities={b"IMAP4REV1", b"UIDPLUS"},
        uids=[7],
        failures={("STORE_ADD", 7): imaplib.IMAP4.error("rejected")},
    )

    result = move_messages(
        client,
        _config(),
        {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [7]},
    )

    assert result == {
        "results": [{"uid": 7, "status": "timeout_unknown", "error_code": "partial_mutation"}],
        "external_effect_status": "timeout_unknown",
        "definitely_no_external_effect": False,
    }
    assert client.mailbox_uids["INBOX"] == {7}
    assert client.mailbox_uids["Processed"] == {7}
    assert client.mutation_details == [
        ("COPY", 7, "Processed"),
        ("STORE_ADD", 7, ("\\Deleted",)),
    ]


def test_test_mode_suppresses_before_client_factory() -> None:
    factory = Mock(side_effect=AssertionError("no connection"))
    payload = {
        "runtime_context": {"test_mode": True},
        "action": {"action_id": "move_messages", "input": {"uids": [7]}},
    }

    result = dispatch_mutation(payload, client_factory=factory)

    assert result == {
        "results": [],
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }
    factory.assert_not_called()


def test_dispatch_opens_one_client_and_routes_validated_input() -> None:
    client = _client(uids=[7])
    payload = {
        "runtime_context": {"test_mode": False},
        "action": {
            "action_id": "set_message_flags",
            "input": {
                "connection_ref": "conn_imap",
                "mailbox": "INBOX",
                "uids": [7],
                "seen": True,
            },
            "target": {
                "connection": {
                    "connection_type_id": "imap_mailbox",
                    "connection_id": "conn_imap",
                    "config": {
                        "provider_preset": "manual",
                        "imap_host": "imap.example.test",
                        "imap_port": 993,
                        "tls_mode": "ssl",
                        "username": "mailbox@example.test",
                    },
                    "secrets": {"password": "secret-password"},
                }
            },
        },
    }
    factory = Mock(return_value=nullcontext(client))

    result = dispatch_mutation(payload, client_factory=factory)

    assert result["external_effect_status"] == "succeeded"
    assert client.flags_by_uid[7] == {"\\Seen"}
    factory.assert_called_once()


def test_mutation_manifest_effects_and_ui_mirror_are_exact() -> None:
    manifest = yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text())
    operations = yaml.safe_load((BUNDLE_ROOT / "contracts/operation_manifest.yaml").read_text())
    actions = yaml.safe_load((BUNDLE_ROOT / "ui/actions/actions.yaml").read_text())
    operation_rows = {
        row["operation_id"]: row
        for row in operations["operations"]
        if row["operation_id"] in {"set_message_flags", "move_messages", "delete_messages"}
    }
    action_rows = {
        row["action_id"]: row for row in actions["actions"] if row["action_id"] in operation_rows
    }

    assert manifest["external_effects"] == [
        {
            "operation_id": "set_message_flags",
            "effect_kind": "mailbox_message_update",
            "channel": "imap",
            "idempotency": "required",
            "test_mode_behavior": "suppress",
            "observability": {
                "target_fields": ["mailbox", "uids"],
                "target_kind": "mailbox_message",
                "target_label": "mailbox UID",
            },
            "rate_limit": {
                "window_seconds": 60,
                "account_max": 600,
                "project_max": 300,
                "connection_max": 120,
            },
        },
        {
            "operation_id": "move_messages",
            "effect_kind": "mailbox_message_move",
            "channel": "imap",
            "idempotency": "required",
            "test_mode_behavior": "suppress",
            "observability": {
                "target_fields": ["source_mailbox", "target_mailbox", "uids"],
                "target_kind": "mailbox_message",
                "target_label": "mailbox UID",
            },
            "rate_limit": {
                "window_seconds": 60,
                "account_max": 600,
                "project_max": 300,
                "connection_max": 120,
            },
        },
        {
            "operation_id": "delete_messages",
            "effect_kind": "mailbox_message_delete",
            "channel": "imap",
            "idempotency": "required",
            "test_mode_behavior": "suppress",
            "observability": {
                "target_fields": ["mailbox", "uids"],
                "target_kind": "mailbox_message",
                "target_label": "mailbox UID",
            },
            "rate_limit": {
                "window_seconds": 60,
                "account_max": 600,
                "project_max": 300,
                "connection_max": 120,
            },
        },
    ]
    assert set(action_rows) == set(operation_rows)
    connection_errors = [
        "invalid_payload",
        "invalid_connection",
        "unsupported_provider",
        "authentication_failed",
        "network_blocked",
        "connection_timeout",
        "tls_verification_failed",
        "connection_failed",
        "mailbox_not_found",
    ]
    # The shared dispatch wrapper can raise either of these from any operation,
    # so both are declared last by every one of them.
    dispatch_errors = ["imap_command_failed", "internal_error"]
    expected_errors = {
        "set_message_flags": [*connection_errors, "timeout_unknown", *dispatch_errors],
        "move_messages": [
            *connection_errors,
            "unsafe_expunge_unsupported",
            "timeout_unknown",
            *dispatch_errors,
        ],
        "delete_messages": [
            *connection_errors,
            "trash_mailbox_unavailable",
            "unsafe_expunge_unsupported",
            "timeout_unknown",
            *dispatch_errors,
        ],
    }
    for operation_id, operation in operation_rows.items():
        action = action_rows[operation_id]
        assert action["mutates_platform"] is False
        assert action["description"] == operation["description"]
        assert [row["name"] for row in action["parameters"]] == [
            row["name"] for row in operation["inputs"]
        ]
        assert action["result_fields"] == [
            field.removeprefix("result.") for field in operation["result_fields"]
        ]
        assert action["error_codes"] == operation["error_codes"] == expected_errors[operation_id]
