from __future__ import annotations

from pathlib import Path

import pytest
import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_OPERATIONS = {
    "discover_settings",
    "test_connection",
    "list_mailboxes",
    "search_messages",
    "get_message",
    "get_attachment",
    "set_message_flags",
    "move_messages",
    "delete_messages",
}
MUTATIONS = {"set_message_flags", "move_messages", "delete_messages"}


@pytest.fixture
def bundle_payloads() -> tuple[dict, dict, dict, dict]:
    manifest = yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text())
    contract_refs = manifest["runtime"]["extension_contract_v2"]
    operations = yaml.safe_load((BUNDLE_ROOT / contract_refs["operation_manifest"]).read_text())
    actions = yaml.safe_load((BUNDLE_ROOT / manifest["action_manifest"]).read_text())
    policies = yaml.safe_load((BUNDLE_ROOT / contract_refs["artifact_policies"]).read_text())
    return manifest, operations, actions, policies


def test_operation_contract_ui_and_effect_surfaces_do_not_drift(bundle_payloads) -> None:
    manifest, operations, actions, policies = bundle_payloads
    operation_rows = {row["operation_id"]: row for row in operations["operations"]}
    action_rows = {row["action_id"]: row for row in actions["actions"]}
    effect_rows = {row["operation_id"]: row for row in manifest["external_effects"]}
    assert set(operation_rows) == EXPECTED_OPERATIONS
    assert set(action_rows) == EXPECTED_OPERATIONS
    assert set(effect_rows) == MUTATIONS
    assert all(row["mutates_platform"] is False for row in action_rows.values())
    assert all(
        any(item["name"] == "connection_ref" for item in row["inputs"])
        for row in operation_rows.values()
    )
    assert policies["policies"] == manifest["artifact_policies"]


def test_search_result_fields_expose_pagination_and_snapshot_state(bundle_payloads) -> None:
    _manifest, operations, actions, _policies = bundle_payloads
    operation = next(
        row for row in operations["operations"] if row["operation_id"] == "search_messages"
    )
    action = next(row for row in actions["actions"] if row["action_id"] == "search_messages")

    assert operation["outputs"] == [
        {"name": "messages", "value_type": "array"},
        {"name": "next_cursor", "value_type": "text"},
        {"name": "truncated", "value_type": "boolean"},
        {"name": "uidvalidity", "value_type": "integer"},
    ]
    assert operation["result_fields"] == [
        "result.messages",
        "result.next_cursor",
        "result.truncated",
        "result.uidvalidity",
    ]
    assert action["result_fields"] == ["messages", "next_cursor", "truncated", "uidvalidity"]


def test_connection_result_fields_expose_all_capability_metadata(bundle_payloads) -> None:
    _manifest, operations, actions, _policies = bundle_payloads
    operation = next(
        row for row in operations["operations"] if row["operation_id"] == "test_connection"
    )
    action = next(row for row in actions["actions"] if row["action_id"] == "test_connection")

    assert operation["outputs"] == [
        {"name": "capabilities", "value_type": "array"},
        {"name": "supports_idle", "value_type": "boolean"},
        {"name": "supports_move", "value_type": "boolean"},
        {"name": "supports_uidplus", "value_type": "boolean"},
        {"name": "supports_condstore", "value_type": "boolean"},
        {"name": "supports_qresync", "value_type": "boolean"},
    ]
    assert operation["result_fields"] == [
        "result.capabilities",
        "result.supports_idle",
        "result.supports_move",
        "result.supports_uidplus",
        "result.supports_condstore",
        "result.supports_qresync",
    ]
    assert action["result_fields"] == [
        "capabilities",
        "supports_idle",
        "supports_move",
        "supports_uidplus",
        "supports_condstore",
        "supports_qresync",
    ]


def test_runtime_entrypoint_and_get_message_public_inputs_match_runtime(bundle_payloads) -> None:
    manifest, operations, actions, _policies = bundle_payloads
    operation = next(
        row for row in operations["operations"] if row["operation_id"] == "get_message"
    )
    action = next(row for row in actions["actions"] if row["action_id"] == "get_message")

    assert manifest["entrypoint"]["command"] == ["python3", "main.py"]
    assert [row["name"] for row in operation["inputs"]] == [
        "connection_ref",
        "mailbox",
        "uid",
        "include_body",
        "include_headers",
        "include_attachment_metadata",
        "include_raw_html",
    ]
    assert [row["name"] for row in action["parameters"]] == [
        "connection_ref",
        "mailbox",
        "uid",
        "include_body",
        "include_headers",
        "include_attachment_metadata",
        "include_raw_html",
    ]


def test_get_message_step_ui_fields_match_public_runtime_inputs() -> None:
    manifest = yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text())
    step_ui_path = manifest["runtime"]["extension_contract_v2"]["step_ui_manifest"]
    step_ui = yaml.safe_load((BUNDLE_ROOT / step_ui_path).read_text())
    operation = next(row for row in step_ui["forms"] if row["operation_id"] == "get_message")

    assert [row["name"] for row in operation["fields"]] == [
        "mailbox",
        "uid",
        "include_body",
        "include_headers",
        "include_attachment_metadata",
        "include_raw_html",
    ]


def test_public_error_codes_cover_runtime_errors_and_match_ui(bundle_payloads) -> None:
    _manifest, operations, actions, _policies = bundle_payloads
    operation_rows = {row["operation_id"]: row for row in operations["operations"]}
    action_rows = {row["action_id"]: row for row in actions["actions"]}
    expected_errors = {
        "discover_settings": {"invalid_payload", "invalid_connection", "unsupported_provider"},
        "test_connection": {
            "invalid_payload",
            "invalid_connection",
            "unsupported_provider",
            "authentication_failed",
            "network_blocked",
            "connection_timeout",
            "tls_verification_failed",
            "connection_failed",
        },
        "list_mailboxes": {
            "invalid_payload",
            "invalid_connection",
            "unsupported_provider",
            "authentication_failed",
            "network_blocked",
            "connection_timeout",
            "tls_verification_failed",
            "connection_failed",
            "mailbox_list_too_large",
            "response_too_large",
        },
        "search_messages": {
            "invalid_payload",
            "invalid_connection",
            "unsupported_provider",
            "authentication_failed",
            "network_blocked",
            "connection_timeout",
            "tls_verification_failed",
            "connection_failed",
            "mailbox_not_found",
            "uidvalidity_changed",
            "response_too_large",
        },
        "get_message": {
            "invalid_payload",
            "invalid_connection",
            "unsupported_provider",
            "authentication_failed",
            "network_blocked",
            "connection_timeout",
            "tls_verification_failed",
            "connection_failed",
            "mailbox_not_found",
            "message_not_found",
            "message_too_large",
            "message_too_complex",
            "response_too_large",
        },
        "get_attachment": {
            "invalid_payload",
            "invalid_connection",
            "unsupported_provider",
            "authentication_failed",
            "network_blocked",
            "connection_timeout",
            "tls_verification_failed",
            "connection_failed",
            "mailbox_not_found",
            "message_not_found",
            "message_too_large",
            "message_too_complex",
            "attachment_not_found",
            "attachment_ambiguous",
            "attachment_too_large",
            "attachment_type_disallowed",
            "artifact_output_unavailable",
        },
        "set_message_flags": {
            "invalid_payload",
            "invalid_connection",
            "unsupported_provider",
            "authentication_failed",
            "network_blocked",
            "connection_timeout",
            "tls_verification_failed",
            "connection_failed",
            "mailbox_not_found",
            "timeout_unknown",
        },
        "move_messages": {
            "invalid_payload",
            "invalid_connection",
            "unsupported_provider",
            "authentication_failed",
            "network_blocked",
            "connection_timeout",
            "tls_verification_failed",
            "connection_failed",
            "mailbox_not_found",
            "unsafe_expunge_unsupported",
            "timeout_unknown",
        },
        "delete_messages": {
            "invalid_payload",
            "invalid_connection",
            "unsupported_provider",
            "authentication_failed",
            "network_blocked",
            "connection_timeout",
            "tls_verification_failed",
            "connection_failed",
            "mailbox_not_found",
            "trash_mailbox_unavailable",
            "unsafe_expunge_unsupported",
            "timeout_unknown",
        },
    }

    assert set(operation_rows) == set(expected_errors)
    for operation_id, errors in expected_errors.items():
        # Raised by the shared dispatch wrapper, so reachable from every one.
        errors.add("internal_error")
        errors.add("imap_command_failed")
        assert set(operation_rows[operation_id]["error_codes"]) == errors
        assert set(action_rows[operation_id]["error_codes"]) == errors


def test_mutation_result_contract_exposes_effect_certainty_at_result_level(
    bundle_payloads,
) -> None:
    _manifest, operations, actions, _policies = bundle_payloads
    operation_rows = {row["operation_id"]: row for row in operations["operations"]}
    action_rows = {row["action_id"]: row for row in actions["actions"]}

    for operation_id in MUTATIONS:
        assert operation_rows[operation_id]["outputs"] == [
            {"name": "results", "value_type": "array"},
            {"name": "external_effect_status", "value_type": "text"},
            {"name": "definitely_no_external_effect", "value_type": "boolean"},
        ]
        assert operation_rows[operation_id]["result_fields"] == [
            "result.results",
            "result.external_effect_status",
            "result.definitely_no_external_effect",
        ]
        assert action_rows[operation_id]["result_fields"] == [
            "results",
            "external_effect_status",
            "definitely_no_external_effect",
        ]


# ---------------------------------------------------------------------------
# Audit policy
#
# Flow Steward applies the policy an action declares; it does not know IMAP, so
# it cannot know that a message body must never reach an audit record. That
# knowledge is this bundle's, and these tests are what hold it in place. They
# used to live in the platform's own suite, which is what let the platform grow
# concrete knowledge of this extension in the first place.
# ---------------------------------------------------------------------------

#: Every field through which message content can leave an IMAP operation. A new
#: content field added to a result must be added here in the same change.
MESSAGE_CONTENT_FIELDS = {
    "body",
    "bodyHtml",
    "bodyText",
    "cc",
    "from",
    "headers",
    "plainText",
    "rawHeaders",
    "rawHtml",
    "subject",
    "text",
    "to",
}
CONTENT_OPERATIONS = {"search_messages", "get_message"}


def test_content_operations_declare_every_message_field_as_audit_redacted(
    bundle_payloads,
) -> None:
    """A body, a subject or a header must never be written to an audit record."""
    _manifest, _operations, actions, _policies = bundle_payloads
    for row in actions["actions"]:
        if row["action_id"] not in CONTENT_OPERATIONS:
            continue
        declared = set(row["audit"]["redact_result_fields"])
        assert declared == MESSAGE_CONTENT_FIELDS, row["action_id"]


def test_every_operation_declares_a_failure_projection_keyed_on_uid(bundle_payloads) -> None:
    """A failure must carry codes and the UID that failed — never mailbox content.

    Every operation, not only the content ones: a failure payload from any of
    them can echo the settings or the message it was working on.
    """
    _manifest, _operations, actions, _policies = bundle_payloads
    for row in actions["actions"]:
        projection = row["audit"]["failure_projection"]
        assert projection["keep_row_fields"] == ["uid"], row["action_id"]


def test_no_operation_is_left_without_an_audit_policy(bundle_payloads) -> None:
    """An operation added without a policy would silently audit its own payload."""
    _manifest, _operations, actions, _policies = bundle_payloads
    assert {row["action_id"] for row in actions["actions"] if row.get("audit")} == (
        EXPECTED_OPERATIONS
    )


def test_the_declared_policy_is_shaped_the_way_the_platform_accepts_it(bundle_payloads) -> None:
    """A policy the platform rejects fails `extensions validate`, so pin the shape here.

    This bundle cannot compile itself — that needs private platform code its
    dependency boundary forbids importing — so what it owns is the declaration.
    Compilation preserving it is proven on the platform side, generically.
    """
    _manifest, _operations, actions, _policies = bundle_payloads
    allowed_keys = {"redact_result_fields", "failure_projection"}
    for row in actions["actions"]:
        policy = row["audit"]
        assert isinstance(policy, dict), row["action_id"]
        assert policy, row["action_id"]
        assert set(policy) <= allowed_keys, row["action_id"]

        projection = policy["failure_projection"]
        assert set(projection) == {"keep_row_fields"}, row["action_id"]
        keep_rows = projection["keep_row_fields"]
        assert isinstance(keep_rows, list)
        assert all(isinstance(item, str) for item in keep_rows)
        assert keep_rows == sorted(set(keep_rows)), f"{row['action_id']} repeats a row field"

        redacted = policy.get("redact_result_fields")
        if redacted is None:
            continue
        assert isinstance(redacted, list), row["action_id"]
        assert redacted, row["action_id"]
        assert all(isinstance(item, str) and item.strip() for item in redacted), row["action_id"]
        assert len(set(redacted)) == len(redacted), f"{row['action_id']} repeats a field"
        assert len(redacted) <= 64, row["action_id"]


# ---------------------------------------------------------------------------
# The contract this bundle declares
#
# The platform reads whatever a bundle declares; what *this* bundle declares is
# its own claim, so it is pinned here. These assertions used to live in the
# platform's suite, which is what made that suite depend on this extension.
# ---------------------------------------------------------------------------

OPERATION_KINDS = {
    "discover_settings": "source",
    "test_connection": "source",
    "list_mailboxes": "source",
    "search_messages": "source",
    "get_message": "source",
    "get_attachment": "source",
    "set_message_flags": "action",
    "move_messages": "action",
    "delete_messages": "action",
}
MUTATION_EFFECT_KINDS = {
    "set_message_flags": "mailbox_message_update",
    "move_messages": "mailbox_message_move",
    "delete_messages": "mailbox_message_delete",
}
SEARCH_FILTER_PROPERTIES = {
    "from": {"type": "string"},
    "to": {"type": "string"},
    "cc": {"type": "string"},
    "subject": {"type": "string"},
    "text": {"type": "string"},
    "message_id": {"type": "string"},
    "since": {"type": "string", "format": "date"},
    "before": {"type": "string", "format": "date"},
    "unseen": {"type": "boolean"},
    "seen": {"type": "boolean"},
    "flagged": {"type": "boolean"},
    "unflagged": {"type": "boolean"},
    "has_attachments": {"type": "boolean"},
    "uid_gte": {"type": "integer", "minimum": 1},
    "uid_lte": {"type": "integer", "minimum": 1},
}


def test_every_operation_declares_its_kind_and_connection_type(bundle_payloads) -> None:
    _manifest, operations, _actions, _policies = bundle_payloads
    rows = {row["operation_id"]: row for row in operations["operations"]}
    assert {op_id: row["operation_kind"] for op_id, row in rows.items()} == OPERATION_KINDS
    assert {tuple(row["connection_type_ids"]) for row in rows.values()} == {("imap_mailbox",)}


def test_every_mutation_declares_a_suppressible_idempotent_effect(bundle_payloads) -> None:
    """A mailbox mutation must be replayable and must not fire in test mode."""
    manifest, _operations, _actions, _policies = bundle_payloads
    effects = {row["operation_id"]: row for row in manifest["external_effects"]}
    assert {op_id: row["effect_kind"] for op_id, row in effects.items()} == MUTATION_EFFECT_KINDS
    assert {
        op_id: (row["idempotency"], row["test_mode_behavior"]) for op_id, row in effects.items()
    } == dict.fromkeys(MUTATION_EFFECT_KINDS, ("required", "suppress"))


def test_the_connection_type_declares_its_settings_and_its_one_secret(bundle_payloads) -> None:
    _manifest, _operations, _actions, _policies = bundle_payloads
    connection_types = yaml.safe_load(
        (
            BUNDLE_ROOT
            / yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text())["runtime"][
                "extension_contract_v2"
            ]["connection_types"]
        ).read_text()
    )["connection_types"]
    connection_type = connection_types[0]

    assert connection_type["connection_type_id"] == "imap_mailbox"
    assert connection_type["config_schema"]["required"] == [
        "provider_preset",
        "imap_host",
        "imap_port",
        "tls_mode",
        "username",
    ]
    assert connection_type["secret_schema"]["required"] == ["password"]
    assert connection_type["capabilities"] == dict.fromkeys(OPERATION_KINDS, True)


def test_search_accepts_only_typed_filters_and_no_raw_query(bundle_payloads) -> None:
    """A raw IMAP search string would let a caller reach past the typed filters."""
    _manifest, operations, _actions, _policies = bundle_payloads
    search = next(
        row for row in operations["operations"] if row["operation_id"] == "search_messages"
    )
    filters = next(row for row in search["inputs"] if row["name"] == "filters")

    assert filters["value_type"] == "object"
    assert filters["schema"]["additionalProperties"] is False
    assert filters["schema"]["properties"] == SEARCH_FILTER_PROPERTIES
    assert "raw_search" not in filters["schema"]["properties"]
    assert "search_criteria" not in filters["schema"]["properties"]


# ---------------------------------------------------------------------------
# Raised-versus-declared
#
# The tables above are hand-written, so they pin what someone believed the
# contract to be. They cannot notice a code the runtime learned to raise and
# nobody added: `attachment_ambiguous` and `imap_command_failed` were both
# returned to callers for a while as codes no manifest declared, which makes
# them undocumented strings a workflow cannot branch on. This test reads the
# codes out of the runtime instead of restating them.
# ---------------------------------------------------------------------------


def _raised_error_codes() -> set[str]:
    """Every literal code the bundle's runtime constructs an error with."""
    import ast

    codes: set[str] = set()
    for path in sorted(BUNDLE_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "ImapExtensionError" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                codes.add(first.value)
    return codes


def test_every_code_the_runtime_raises_is_declared_somewhere(bundle_payloads) -> None:
    _manifest, operations, _actions, _policies = bundle_payloads
    declared: set[str] = set()
    for row in operations["operations"]:
        declared.update(row["error_codes"])

    raised = _raised_error_codes()
    assert raised, "no raised codes found — the AST walk stopped matching"
    assert raised <= declared, f"undeclared codes reach callers: {sorted(raised - declared)}"


def test_dispatch_level_codes_are_declared_by_every_operation(bundle_payloads) -> None:
    """`internal_error` and `imap_command_failed` are raised by the shared
    dispatch wrapper, so they are reachable from any operation and every
    operation must declare them."""
    _manifest, operations, actions, _policies = bundle_payloads
    for row in operations["operations"]:
        assert {"internal_error", "imap_command_failed"} <= set(row["error_codes"]), row[
            "operation_id"
        ]
    for row in actions["actions"]:
        assert {"internal_error", "imap_command_failed"} <= set(row["error_codes"]), row[
            "action_id"
        ]
