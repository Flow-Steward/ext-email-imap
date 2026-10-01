from __future__ import annotations

import imaplib
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from imap_config import ImapConnectionConfig, connection_config_from_payload
from imap_errors import ImapExtensionError
from imap_transport import MailboxClient, open_mailbox_client

MutationStep = Callable[[], object]
ClientFactory = Callable[[ImapConnectionConfig], AbstractContextManager[MailboxClient]]

_MUTATION_HANDLERS = {"set_message_flags", "move_messages", "delete_messages"}


def set_message_flags(
    client: MailboxClient,
    config: ImapConnectionConfig,
    input_payload: Mapping[str, Any],
) -> dict[str, object]:
    """Set Seen/Flagged state through one UID STORE state machine per message."""
    del config
    payload = _input_mapping(input_payload)
    mailbox = _mailbox(payload.get("mailbox"), field="mailbox")
    uids = _validated_uids(payload.get("uids"))
    seen = _optional_boolean(payload, "seen")
    flagged = _optional_boolean(payload, "flagged")
    if seen is None and flagged is None:
        _invalid("seen or flagged must be provided as a boolean")

    add = tuple(flag for value, flag in ((seen, "\\Seen"), (flagged, "\\Flagged")) if value is True)
    remove = tuple(
        flag for value, flag in ((seen, "\\Seen"), (flagged, "\\Flagged")) if value is False
    )
    _select_writable(client, mailbox)

    def steps(uid: int) -> Sequence[MutationStep]:
        commands: list[MutationStep] = []
        if add:
            commands.append(lambda: client.add_flags(uid, add, silent=True))
        if remove:
            commands.append(lambda: client.remove_flags(uid, remove, silent=True))
        return commands

    return _run_uid_batch(uids, steps)


def move_messages(
    client: MailboxClient,
    config: ImapConnectionConfig,
    input_payload: Mapping[str, Any],
) -> dict[str, object]:
    """Move messages with UID MOVE or a UIDPLUS-scoped copy/delete fallback."""
    del config
    payload = _input_mapping(input_payload)
    source = _mailbox(payload.get("source_mailbox"), field="source_mailbox")
    target = _mailbox(payload.get("target_mailbox"), field="target_mailbox")
    if _same_mailbox(source, target):
        _invalid("source_mailbox and target_mailbox must be different")
    uids = _validated_uids(payload.get("uids"))
    return _move_validated(client, source, target, uids)


def delete_messages(
    client: MailboxClient,
    config: ImapConnectionConfig,
    input_payload: Mapping[str, Any],
) -> dict[str, object]:
    """Move messages to a trusted trash mailbox or UID-expunge them explicitly."""
    payload = _input_mapping(input_payload)
    mailbox = _mailbox(payload.get("mailbox"), field="mailbox")
    uids = _validated_uids(payload.get("uids"))
    mode = payload.get("mode", "move_to_trash")
    if mode not in {"move_to_trash", "permanent"}:
        _invalid("mode must be move_to_trash or permanent")

    confirmation = payload.get("confirm_permanent_delete", False)
    if not isinstance(confirmation, bool):
        _invalid("confirm_permanent_delete must be a boolean")
    if mode == "permanent":
        if confirmation is not True:
            _invalid("permanent deletion requires explicit confirmation")
        _require_uid_expunge(client)
        _select_writable(client, mailbox)
        return _run_uid_batch(
            uids,
            lambda uid: (
                lambda: client.add_flags(uid, ("\\Deleted",), silent=True),
                lambda: client.uid_expunge(uid),
            ),
        )

    trash = config.trash_mailbox or _special_use_trash(client)
    if _same_mailbox(mailbox, trash):
        _invalid("mailbox and trash mailbox must be different")
    return _move_validated(client, mailbox, trash, uids)


def dispatch_mutation(
    payload: Mapping[str, Any],
    *,
    client_factory: ClientFactory = open_mailbox_client,
) -> dict[str, object]:
    """Dispatch one mutation, suppressing test mode before connection resolution."""
    root = _input_mapping(payload)
    operation = _operation_id(root)
    if operation not in _MUTATION_HANDLERS:
        _invalid("unsupported mutation operation")
    if _test_mode(root):
        return {
            "results": [],
            "external_effect_status": "suppressed",
            "definitely_no_external_effect": True,
        }

    config = connection_config_from_payload(root)
    operation_input = _operation_input(root)
    with client_factory(config) as client:
        if operation == "set_message_flags":
            return set_message_flags(client, config, operation_input)
        if operation == "move_messages":
            return move_messages(client, config, operation_input)
        return delete_messages(client, config, operation_input)


def _move_validated(
    client: MailboxClient,
    source: str,
    target: str,
    uids: list[int],
) -> dict[str, object]:
    capabilities = _capabilities(client)
    use_move = b"MOVE" in capabilities
    if not use_move:
        _require_uid_expunge(client, capabilities=capabilities)
    _select_writable(client, source)

    if use_move:
        return _run_uid_batch(
            uids,
            lambda uid: (lambda: client.move(uid, target),),
            command_errors_are_ambiguous=True,
        )
    return _run_uid_batch(
        uids,
        lambda uid: (
            lambda: client.copy(uid, target),
            lambda: client.add_flags(uid, ("\\Deleted",), silent=True),
            lambda: client.uid_expunge(uid),
        ),
    )


def _run_uid_batch(
    uids: Sequence[int],
    steps_for_uid: Callable[[int], Sequence[MutationStep]],
    *,
    command_errors_are_ambiguous: bool = False,
) -> dict[str, object]:
    # Build every command only after public validation and before any command can
    # reach the server. Exceptions below therefore occur at the mutation boundary.
    commands_by_uid = [(uid, tuple(steps_for_uid(uid))) for uid in uids]
    results: list[dict[str, object]] = []
    stop_index: int | None = None
    for index, (uid, commands) in enumerate(commands_by_uid):
        completed_steps = 0
        try:
            for step in commands:
                step()
                completed_steps += 1
        except Exception as exc:
            if command_errors_are_ambiguous or _ambiguous_mutation_failure(exc):
                results.append(
                    {"uid": uid, "status": "timeout_unknown", "error_code": "timeout_unknown"}
                )
                stop_index = index + 1
                break
            if _known_mutation_failure(exc):
                if completed_steps:
                    results.append(
                        {"uid": uid, "status": "timeout_unknown", "error_code": "partial_mutation"}
                    )
                    stop_index = index + 1
                    break
                results.append({"uid": uid, "status": "failed", "error_code": "mutation_failed"})
                continue
            raise
        results.append({"uid": uid, "status": "succeeded"})

    if stop_index is not None:
        results.extend(
            {
                "uid": uid,
                "status": "failed",
                "error_code": "not_attempted_after_timeout_unknown",
            }
            for uid in uids[stop_index:]
        )
    return _aggregate_results(results)


def _aggregate_results(results: list[dict[str, object]]) -> dict[str, object]:
    statuses = {row["status"] for row in results}
    if "timeout_unknown" in statuses or statuses == {"succeeded", "failed"}:
        status = "timeout_unknown"
    elif statuses == {"succeeded"}:
        status = "succeeded"
    else:
        status = "failed"
    response: dict[str, object] = {
        "results": results,
        "external_effect_status": status,
    }
    if status == "failed":
        response["definitely_no_external_effect"] = True
    elif status == "timeout_unknown":
        response["definitely_no_external_effect"] = False
    return response


def _validated_uids(value: object) -> list[int]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise ImapExtensionError("invalid_payload", "uids must contain 1 to 100 items")
    if any(isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0 for uid in value):
        raise ImapExtensionError("invalid_payload", "uids must contain positive integers")
    return sorted(set(value))


def _require_uid_expunge(
    client: MailboxClient,
    *,
    capabilities: frozenset[bytes] | None = None,
) -> None:
    if b"UIDPLUS" not in (capabilities or _capabilities(client)):
        raise ImapExtensionError(
            "unsafe_expunge_unsupported",
            "The server does not support safe UID-scoped expunge",
        )


def _capabilities(client: MailboxClient) -> frozenset[bytes]:
    return frozenset(value.upper() for value in client.capabilities())


def _select_writable(client: MailboxClient, mailbox: str) -> None:
    try:
        client.select_folder(mailbox, readonly=False)
    except imaplib.IMAP4.abort as exc:
        raise ImapExtensionError("connection_failed", "The IMAP server connection failed") from exc
    except imaplib.IMAP4.error as exc:
        raise ImapExtensionError("mailbox_not_found", "The mailbox could not be selected") from exc


def _special_use_trash(client: MailboxClient) -> str:
    candidates: list[str] = []
    try:
        folders = client.list_folders()
    except imaplib.IMAP4.error as exc:
        raise ImapExtensionError(
            "connection_failed", "The IMAP server could not list mailboxes"
        ) from exc
    for flags, _delimiter, name in folders:
        normalized = {flag.upper() for flag in flags}
        if b"\\TRASH" in normalized and b"\\NOSELECT" not in normalized:
            candidates.append(name)
    if len(candidates) != 1:
        raise ImapExtensionError(
            "trash_mailbox_unavailable",
            "A reliable trash mailbox is not configured or advertised",
        )
    return candidates[0]


def _optional_boolean(payload: Mapping[str, Any], field: str) -> bool | None:
    if field not in payload:
        return None
    value = payload[field]
    if not isinstance(value, bool):
        _invalid(f"{field} must be a boolean")
    return value


def _mailbox(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _invalid(f"{field} must be a non-empty mailbox name")
    return value


def _same_mailbox(left: str, right: str) -> bool:
    return left == right or (left.casefold() == "inbox" and right.casefold() == "inbox")


def _ambiguous_mutation_failure(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, OSError, EOFError, imaplib.IMAP4.abort)):
        return True
    return isinstance(error, ImapExtensionError) and (
        error.code in {"connection_failed", "connection_timeout"}
        or error.external_effect_status == "timeout_unknown"
        or not error.definitely_no_external_effect
    )


def _known_mutation_failure(error: Exception) -> bool:
    return isinstance(error, (ImapExtensionError, imaplib.IMAP4.error))


def _test_mode(payload: Mapping[str, Any]) -> bool:
    runtime = payload.get("runtime_context")
    if runtime is None:
        return False
    if not isinstance(runtime, Mapping):
        _invalid("runtime_context must be an object")
    value = runtime.get("test_mode", False)
    if not isinstance(value, bool):
        _invalid("test_mode must be a boolean")
    return value


def _operation_id(payload: Mapping[str, Any]) -> str:
    action = payload.get("action")
    if isinstance(action, Mapping):
        value = action.get("action_id") or action.get("operation_id")
    else:
        value = action
    if not isinstance(value, str) or not value:
        _invalid("mutation operation is required")
    return value


def _operation_input(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    action = payload.get("action")
    candidates = [action.get("input") if isinstance(action, Mapping) else None]
    candidates.extend((payload.get("input"), payload.get("request")))
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _input_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _invalid("request payload must be an object")
    return value


def _invalid(message: str) -> None:
    raise ImapExtensionError("invalid_payload", message)
