"""Strict public-envelope dispatcher for Generic IMAP mailbox operations."""

from __future__ import annotations

import imaplib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any

from flowsteward_extension_sdk import runtime_context_from_payload
from imap_artifacts import get_attachment
from imap_config import (
    ImapConnectionConfig,
    connection_config_from_payload,
    discover_settings,
    discovery_config_from_payload,
)
from imap_errors import ImapExtensionError
from imap_messages import get_message
from imap_mutations import dispatch_mutation
from imap_search import list_mailboxes, search_messages
from imap_transport import MailboxClient, open_mailbox_client

ClientFactory = Callable[[ImapConnectionConfig], AbstractContextManager[MailboxClient]]

#: A refused IMAP command is quoted back to the operator, bounded so a
#: talkative server cannot fill the job record.
_MAX_SERVER_MESSAGE_CHARS = 200

MUTATION_OPERATIONS = frozenset({"set_message_flags", "move_messages", "delete_messages"})
_OPERATION_FIELDS: dict[str, frozenset[str]] = {
    "discover_settings": frozenset({"connection_ref"}),
    "test_connection": frozenset({"connection_ref"}),
    "list_mailboxes": frozenset({"connection_ref"}),
    "search_messages": frozenset({"connection_ref", "mailbox", "filters", "limit", "cursor"}),
    "get_message": frozenset(
        {
            "connection_ref",
            "mailbox",
            "uid",
            "include_body",
            "include_headers",
            "include_attachment_metadata",
            "include_raw_html",
        }
    ),
    "get_attachment": frozenset(
        {"connection_ref", "mailbox", "uid", "attachment_id", "filename", "content_type"}
    ),
    "set_message_flags": frozenset({"connection_ref", "mailbox", "uids", "seen", "flagged"}),
    "move_messages": frozenset({"connection_ref", "source_mailbox", "target_mailbox", "uids"}),
    "delete_messages": frozenset(
        {
            "connection_ref",
            "mailbox",
            "uids",
            "mode",
            "confirm_permanent_delete",
        }
    ),
}
_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "discover_settings": frozenset({"connection_ref"}),
    "test_connection": frozenset({"connection_ref"}),
    "list_mailboxes": frozenset({"connection_ref"}),
    "search_messages": frozenset({"connection_ref", "mailbox"}),
    "get_message": frozenset({"connection_ref", "mailbox", "uid"}),
    # The selector itself is required, but which one is a choice; load_attachment
    # refuses a call that names none or several, and says what the message holds.
    "get_attachment": frozenset({"connection_ref", "mailbox", "uid"}),
    "set_message_flags": frozenset({"connection_ref", "mailbox", "uids"}),
    "move_messages": frozenset({"connection_ref", "source_mailbox", "target_mailbox", "uids"}),
    "delete_messages": frozenset({"connection_ref", "mailbox", "uids"}),
}
_SAFE_ERROR_CODES = frozenset(
    {
        "artifact_output_unavailable",
        "attachment_not_found",
        "attachment_too_large",
        "attachment_type_disallowed",
        "authentication_failed",
        "connection_failed",
        "connection_timeout",
        "invalid_connection",
        "invalid_payload",
        "mailbox_list_too_large",
        "mailbox_not_found",
        "message_not_found",
        "message_too_complex",
        "message_too_large",
        "network_blocked",
        "response_too_large",
        "timeout_unknown",
        "tls_verification_failed",
        "trash_mailbox_unavailable",
        "uidvalidity_changed",
        "unsafe_expunge_unsupported",
        "unsupported_provider",
    }
)


def handle_payload(
    payload: dict[str, Any],
    *,
    client_factory: ClientFactory = open_mailbox_client,
) -> dict[str, Any]:
    """Validate and execute one host action envelope with stable safe responses."""
    operation_id = _untrusted_operation_id(payload)
    try:
        root, operation_id, input_payload = _validated_action_envelope(payload)
        result = _dispatch_operation(
            root,
            operation_id=operation_id,
            input_payload=input_payload,
            client_factory=client_factory,
        )
        return _success_response(operation_id, result)
    except ImapExtensionError as exc:
        return _imap_error_response(operation_id, exc)
    except imaplib.IMAP4.error as exc:
        # The connection is usually fine here: this is the server refusing one
        # command. Say which one it was and what the server answered, so the
        # operator is not sent to check a connection that works.
        return _imap_error_response(
            operation_id,
            ImapExtensionError("imap_command_failed", _server_refusal_message(exc)),
        )
    except Exception:
        return _internal_error_response(operation_id)


def _server_refusal_message(exc: BaseException) -> str:
    """Render a server refusal without letting an unbounded reply through."""
    text = " ".join(str(exc or "").split())
    if not text:
        return "The IMAP server refused the command"
    if len(text) > _MAX_SERVER_MESSAGE_CHARS:
        text = f"{text[:_MAX_SERVER_MESSAGE_CHARS].rstrip()}…"
    return f"The IMAP server refused the command: {text}"


def invalid_payload_response() -> dict[str, Any]:
    """Return the stable subprocess response for malformed JSON input."""
    return _imap_error_response(
        "",
        ImapExtensionError("invalid_payload", "Request payload must be a JSON object"),
    )


def _validated_action_envelope(
    payload: object,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if not isinstance(payload, dict):
        _invalid_payload("Request payload must be a JSON object")
    root = payload
    if root.get("mode") != "action":
        _invalid_payload("Request mode must be action")
    action = root.get("action")
    if not isinstance(action, Mapping):
        _invalid_payload("action must be an object")
    operation_id = action.get("action_id") or action.get("operation_id")
    if not isinstance(operation_id, str) or operation_id not in _OPERATION_FIELDS:
        _invalid_payload("Unknown IMAP operation")
    operation_input = action.get("input")
    if not isinstance(operation_input, Mapping):
        _invalid_payload("action.input must be an object")
    input_payload = dict(operation_input)
    if set(input_payload) - _OPERATION_FIELDS[operation_id]:
        _invalid_payload("Operation input contains an unsupported field")
    if not _REQUIRED_FIELDS[operation_id].issubset(input_payload):
        _invalid_payload("Operation input is missing a required field")
    connection_ref = input_payload.get("connection_ref")
    if not isinstance(connection_ref, str) or not connection_ref.strip():
        _invalid_payload("connection_ref must be a non-empty string")
    runtime = root.get("runtime_context")
    if runtime is not None and not isinstance(runtime, Mapping):
        _invalid_payload("runtime_context must be an object")
    if (
        isinstance(runtime, Mapping)
        and "test_mode" in runtime
        and not isinstance(runtime.get("test_mode"), bool)
    ):
        _invalid_payload("runtime_context.test_mode must be a boolean")
    runtime_context_from_payload(root)
    return root, operation_id, input_payload


def _dispatch_operation(
    payload: dict[str, Any],
    *,
    operation_id: str,
    input_payload: dict[str, Any],
    client_factory: ClientFactory,
) -> dict[str, object]:
    if operation_id in MUTATION_OPERATIONS:
        return dispatch_mutation(payload, client_factory=client_factory)

    if operation_id == "discover_settings":
        return {"settings": discover_settings(discovery_config_from_payload(payload))}
    config = connection_config_from_payload(payload)
    with client_factory(config) as client:
        if operation_id == "test_connection":
            return _connection_capabilities(client)
        if operation_id == "list_mailboxes":
            return list_mailboxes(client)
        if operation_id == "search_messages":
            return search_messages(client, input_payload)
        if operation_id == "get_message":
            return get_message(client, input_payload)
        if operation_id == "get_attachment":
            return get_attachment(client, payload, input_payload)
    raise AssertionError("validated operation was not dispatched")


def _connection_capabilities(client: MailboxClient) -> dict[str, object]:
    capabilities = sorted(
        {value.decode("ascii", errors="replace").upper() for value in client.capabilities()}
    )
    advertised = set(capabilities)
    return {
        "capabilities": capabilities,
        "supports_idle": "IDLE" in advertised,
        "supports_move": "MOVE" in advertised,
        "supports_uidplus": "UIDPLUS" in advertised,
        "supports_condstore": "CONDSTORE" in advertised,
        "supports_qresync": "QRESYNC" in advertised,
    }


def _success_response(operation_id: str, result: Mapping[str, object]) -> dict[str, Any]:
    response: dict[str, Any] = {"ok": True, "result": dict(result)}
    if operation_id in MUTATION_OPERATIONS:
        status = result.get("external_effect_status")
        if isinstance(status, str) and status:
            response["external_effect_status"] = status
        if "definitely_no_external_effect" in result:
            response["definitely_no_external_effect"] = bool(
                result["definitely_no_external_effect"]
            )
    return response


def _imap_error_response(
    operation_id: str,
    error: ImapExtensionError,
) -> dict[str, Any]:
    if error.code not in _SAFE_ERROR_CODES:
        return _internal_error_response(operation_id)
    response: dict[str, Any] = {
        "ok": False,
        "result": {},
        "error_code": error.code,
        "error": error.message,
        "errors": [{"code": error.code, "message": error.message}],
    }
    if operation_id in MUTATION_OPERATIONS:
        status = error.external_effect_status or (
            "failed" if error.definitely_no_external_effect else "timeout_unknown"
        )
        response["external_effect_status"] = status
        response["definitely_no_external_effect"] = bool(error.definitely_no_external_effect)
    return response


def _internal_error_response(operation_id: str) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": False,
        "result": {},
        "error_code": "internal_error",
        "error": "The IMAP operation could not be completed",
        "errors": [
            {
                "code": "internal_error",
                "message": "The IMAP operation could not be completed",
            }
        ],
    }
    if operation_id in MUTATION_OPERATIONS:
        response["external_effect_status"] = "timeout_unknown"
        response["definitely_no_external_effect"] = False
    return response


def _untrusted_operation_id(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return ""
    action = payload.get("action")
    if not isinstance(action, Mapping):
        return ""
    value = action.get("action_id") or action.get("operation_id")
    return value if isinstance(value, str) and value in MUTATION_OPERATIONS else ""


def _invalid_payload(message: str) -> None:
    raise ImapExtensionError("invalid_payload", message)


__all__ = ["handle_payload", "invalid_payload_response"]
