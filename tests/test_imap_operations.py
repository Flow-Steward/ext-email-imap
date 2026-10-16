from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SDK_PARENT = Path(__file__).resolve().parents[3] / "core/infrastructure/extension_sdk/public"
for import_root in (BUNDLE_ROOT, SDK_PARENT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from fake_mailbox_client import FakeMailboxClient  # noqa: E402
from imap_operations import handle_payload  # noqa: E402

#: The host contract every subprocess payload declares.
HOST_CONTRACT_VERSION = "extension_host_v2"
#: The final extension_host_v2 execution scope: all four keys, at most one owner.
ROOT_STEP_SCOPE: dict[str, Any] = {
    "group_id": None,
    "for_each_id": None,
    "step_id": "read_mailbox",
    "step_execution_id": "9a8b7c6d-5e4f-4a3b-8c2d-000000000001",
}
GROUP_STEP_SCOPE: dict[str, Any] = {**ROOT_STEP_SCOPE, "group_id": "collect_invoices"}
FOR_EACH_STEP_SCOPE: dict[str, Any] = {
    **ROOT_STEP_SCOPE,
    "for_each_id": "each_message",
    "step_execution_id": "9a8b7c6d-5e4f-4a3b-8c2d-000000000002",
}


def _connection() -> dict[str, Any]:
    return {
        "connection_id": "conn-imap-primary",
        "connection_type": "imap_mailbox",
        "config": {
            "provider_preset": "manual",
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "tls_mode": "ssl",
            "username": "mailbox@example.test",
        },
        "secrets": {"password": "not-a-real-password"},
    }


def _payload(
    operation_id: str,
    input_payload: dict[str, Any] | None = None,
    *,
    include_target: bool = True,
    test_mode: bool = False,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action_id": operation_id,
        "input": {
            "connection_ref": "conn-imap-primary",
            **dict(input_payload or {}),
        },
    }
    if include_target:
        action["target"] = {"connection": _connection()}
    return {
        "contract_version": HOST_CONTRACT_VERSION,
        "mode": "action",
        "action": action,
        "runtime_context": {
            **(ROOT_STEP_SCOPE if scope is None else scope),
            "test_mode": test_mode,
        },
    }


def _factory_for(
    client: FakeMailboxClient,
) -> tuple[list[object], Any]:
    opened: list[object] = []

    @contextmanager
    def factory(config: object) -> Iterator[FakeMailboxClient]:
        opened.append(config)
        yield client

    return opened, factory


@pytest.mark.parametrize(
    ("operation_id", "input_payload", "expected_key"),
    [
        ("list_mailboxes", {}, "mailboxes"),
        ("search_messages", {"mailbox": "INBOX"}, "messages"),
    ],
)
def test_dispatches_read_operations_through_one_authenticated_client(
    operation_id: str,
    input_payload: dict[str, Any],
    expected_key: str,
) -> None:
    client = FakeMailboxClient()
    opened, factory = _factory_for(client)

    response = handle_payload(_payload(operation_id, input_payload), client_factory=factory)

    assert response["ok"] is True
    assert expected_key in response["result"]
    assert len(opened) == 1


def test_test_connection_returns_stable_capability_metadata() -> None:
    client = FakeMailboxClient(
        capability_values=frozenset({b"IMAP4REV1", b"IDLE", b"MOVE", b"UIDPLUS"})
    )
    opened, factory = _factory_for(client)

    response = handle_payload(_payload("test_connection"), client_factory=factory)

    assert response == {
        "ok": True,
        "result": {
            "capabilities": ["IDLE", "IMAP4REV1", "MOVE", "UIDPLUS"],
            "supports_idle": True,
            "supports_move": True,
            "supports_uidplus": True,
            "supports_condstore": False,
            "supports_qresync": False,
        },
    }
    assert len(opened) == 1


def test_discovery_uses_saved_connection_without_opening_transport() -> None:
    def forbidden_factory(_config: object) -> object:
        raise AssertionError("discovery must not open an IMAP transport")

    response = handle_payload(
        _payload("discover_settings"),
        client_factory=forbidden_factory,  # type: ignore[arg-type]
    )

    assert response["ok"] is True
    assert response["result"]["settings"]["requires_user_confirmation"] is True
    assert response["result"]["settings"]["candidates"] == [
        {"imap_host": "imap.example.test", "requires_user_confirmation": True},
        {"imap_host": "mail.example.test", "requires_user_confirmation": True},
    ]


def test_discovery_succeeds_without_secret_grant_dns_or_transport(monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("dns"))

    def forbidden_factory(_config: object) -> object:
        raise AssertionError("discovery must not open an IMAP transport")

    payload = _payload("discover_settings")
    del payload["action"]["target"]["connection"]["secrets"]

    response = handle_payload(payload, client_factory=forbidden_factory)  # type: ignore[arg-type]

    assert response["ok"] is True
    assert response["result"]["settings"]["provider_preset"] == "manual"


@pytest.mark.parametrize(
    "bad_input",
    [
        {},
        {"connection_ref": "conn-imap-primary", "password": "forbidden"},
        {"connection_ref": "conn-imap-primary", "connection_id": "forbidden"},
        {"connection_ref": "conn-imap-primary", "raw_search": "ALL"},
    ],
)
def test_public_input_requires_connection_ref_and_rejects_non_contract_fields(
    bad_input: dict[str, Any],
) -> None:
    client = FakeMailboxClient()
    opened, factory = _factory_for(client)
    payload = _payload("search_messages", {"mailbox": "INBOX"})
    payload["action"]["input"] = bad_input

    response = handle_payload(payload, client_factory=factory)

    assert response["ok"] is False
    assert response["error_code"] == "invalid_payload"
    assert response["errors"] == [{"code": "invalid_payload", "message": response["error"]}]
    assert opened == []
    assert "forbidden" not in json.dumps(response)


def test_public_envelope_requires_action_mode_before_client_io() -> None:
    client = FakeMailboxClient()
    opened, factory = _factory_for(client)
    payload = _payload("list_mailboxes")
    del payload["mode"]

    response = handle_payload(payload, client_factory=factory)

    assert response["ok"] is False
    assert response["error_code"] == "invalid_payload"
    assert opened == []


def test_mutation_test_mode_suppresses_before_connection_or_client_io() -> None:
    def forbidden_factory(_config: object) -> object:
        raise AssertionError("test mode must not open an IMAP transport")

    response = handle_payload(
        _payload(
            "set_message_flags",
            {"mailbox": "INBOX", "uids": [7], "seen": True},
            include_target=False,
            test_mode=True,
        ),
        client_factory=forbidden_factory,  # type: ignore[arg-type]
    )

    assert response == {
        "ok": True,
        "result": {
            "results": [],
            "external_effect_status": "suppressed",
            "definitely_no_external_effect": True,
        },
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }


def test_mutation_failure_has_definitive_outer_effect_metadata() -> None:
    payload = _payload(
        "set_message_flags",
        {"mailbox": "INBOX", "uids": [7], "seen": True},
    )
    payload["action"]["target"]["connection"]["connection_id"] = "conn-other"

    response = handle_payload(payload)

    assert response["ok"] is False
    assert response["error_code"] == "invalid_connection"
    assert response["external_effect_status"] == "failed"
    assert response["definitely_no_external_effect"] is True


def test_ambiguous_mutation_result_keeps_unknown_outer_effect_metadata() -> None:
    client = FakeMailboxClient(
        uids=[7, 8],
        capability_values=frozenset({b"MOVE"}),
        folders=[
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", "Processed"),
        ],
        mailbox_uids={"INBOX": {7, 8}, "Processed": set()},
        mutation_failures={("MOVE", 7): RuntimeError("wire outcome unavailable")},
    )
    _opened, factory = _factory_for(client)

    response = handle_payload(
        _payload(
            "move_messages",
            {
                "source_mailbox": "INBOX",
                "target_mailbox": "Processed",
                "uids": [7, 8],
            },
        ),
        client_factory=factory,
    )

    assert response["ok"] is True
    assert response["external_effect_status"] == "timeout_unknown"
    assert response["definitely_no_external_effect"] is False
    assert response["result"]["results"] == [
        {"uid": 7, "status": "timeout_unknown", "error_code": "timeout_unknown"},
        {
            "uid": 8,
            "status": "failed",
            "error_code": "not_attempted_after_timeout_unknown",
        },
    ]


def test_attachment_dispatch_receives_complete_signed_grant_payload(monkeypatch) -> None:
    client = FakeMailboxClient()
    _opened, factory = _factory_for(client)
    payload = _payload(
        "get_attachment",
        {"mailbox": "INBOX", "uid": 7, "attachment_id": "2"},
    )
    payload["artifacts"] = {
        "outputs": [
            {
                "binding_key": "attachment_artifact_handle",
                "role": "output",
                "access": {"platform_grant": {"version": 1, "signature": "signed"}},
            }
        ]
    }

    def fake_get_attachment(
        observed_client: object,
        observed_payload: dict[str, Any],
        observed_input: dict[str, Any],
    ) -> dict[str, object]:
        assert observed_client is client
        assert observed_payload is payload
        assert observed_input["connection_ref"] == "conn-imap-primary"
        assert observed_payload["artifacts"] == payload["artifacts"]
        return {
            "attachment_artifact_handle": "artifact:invoice-7",
            "attachment_metadata": {"original_filename": "invoice.pdf"},
        }

    monkeypatch.setattr("imap_operations.get_attachment", fake_get_attachment)

    response = handle_payload(payload, client_factory=factory)

    assert response["ok"] is True
    assert response["result"]["attachment_artifact_handle"] == "artifact:invoice-7"
    assert "artifacts" not in response


def test_unknown_runtime_exception_uses_stable_non_sensitive_error(monkeypatch) -> None:
    marker = "PRIVATE_SUBJECT_AND_PASSWORD"
    monkeypatch.setattr(
        "imap_operations.list_mailboxes",
        lambda _client: (_ for _ in ()).throw(RuntimeError(marker)),
    )
    _opened, factory = _factory_for(FakeMailboxClient())

    response = handle_payload(_payload("list_mailboxes"), client_factory=factory)

    assert response["ok"] is False
    assert response["error_code"] == "internal_error"
    assert marker not in json.dumps(response)


def test_main_emits_one_safe_json_line_for_invalid_json() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(BUNDLE_ROOT), str(SDK_PARENT), env.get("PYTHONPATH", "")]
    )

    completed = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "main.py")],
        input="{not-json",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert completed.stdout.count("\n") == 1
    response = json.loads(completed.stdout)
    assert response["ok"] is False
    assert response["error_code"] == "invalid_payload"
    assert "not-json" not in completed.stdout


# -- The final execution scope ---------------------------------------------


def _attachment_request(scope: dict[str, Any], monkeypatch) -> dict[str, Any]:
    from test_imap_artifacts import _attachment_input, _payload_with_output_grant

    payload = _payload("get_attachment", _attachment_input(), scope=scope)
    payload.update(_payload_with_output_grant(monkeypatch))
    return payload


@pytest.mark.parametrize(
    "scope",
    [ROOT_STEP_SCOPE, GROUP_STEP_SCOPE, FOR_EACH_STEP_SCOPE],
    ids=["root", "group", "for_each"],
)
def test_an_attachment_reaches_its_artifact_for_every_owner(monkeypatch, scope) -> None:
    import functools
    import hashlib

    import imap_artifacts
    import imap_operations
    from test_imap_artifacts import _attachment_client

    body = b"%PDF-1.7\ninvoice"
    writes: list[bytes] = []

    def artifact_writer(_payload: Any, data: bytes, **_kwargs: Any) -> dict[str, Any]:
        writes.append(data)
        return {
            "artifact_handle": "artifact:artifact-7",
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    monkeypatch.setattr(
        imap_operations,
        "get_attachment",
        functools.partial(imap_artifacts.get_attachment, artifact_writer=artifact_writer),
    )
    opened, factory = _factory_for(_attachment_client(body))

    response = handle_payload(_attachment_request(scope, monkeypatch), client_factory=factory)

    assert response["ok"] is True, response
    assert response["result"]["attachment_artifact_handle"] == "artifact:artifact-7"
    assert writes == [body]
    assert len(opened) == 1


@pytest.mark.parametrize(
    ("contract_version", "scope", "reason"),
    [
        ("extension_host_v1", FOR_EACH_STEP_SCOPE, "Unsupported host contract"),
        ("", FOR_EACH_STEP_SCOPE, "Unsupported host contract"),
        (
            HOST_CONTRACT_VERSION,
            {k: v for k, v in FOR_EACH_STEP_SCOPE.items() if k != "for_each_id"},
            "misses execution scope",
        ),
        (
            HOST_CONTRACT_VERSION,
            {**FOR_EACH_STEP_SCOPE, "group_id": "collect_invoices"},
            "both a Group and a For each",
        ),
        (
            HOST_CONTRACT_VERSION,
            {**ROOT_STEP_SCOPE, "step_execution_id": None},
            "without its logical execution",
        ),
    ],
    ids=["v1", "no_contract", "missing_key", "two_owners", "step_without_execution"],
)
@pytest.mark.parametrize("operation_id", ["get_attachment", "move_messages"])
def test_a_foreign_or_incomplete_envelope_is_refused_before_any_effect(
    monkeypatch, operation_id: str, contract_version: str, scope: dict[str, Any], reason: str
) -> None:
    import imap_operations

    touched: list[str] = []

    def forbidden(name: str) -> Any:
        def call(*_args: Any, **_kwargs: Any) -> Any:
            touched.append(name)
            raise AssertionError(f"{name} ran for a refused envelope")

        return call

    monkeypatch.setattr(imap_operations, "get_attachment", forbidden("artifact_write"))
    monkeypatch.setattr(imap_operations, "dispatch_mutation", forbidden("mutation"))
    if operation_id == "get_attachment":
        payload = _attachment_request(ROOT_STEP_SCOPE, monkeypatch)
    else:
        payload = _payload(
            "move_messages",
            {"source_mailbox": "INBOX", "target_mailbox": "Processed", "uids": [7]},
        )
    payload["contract_version"] = contract_version
    payload["runtime_context"] = {**scope, "test_mode": False}

    response = handle_payload(payload, client_factory=forbidden("imap_connection"))

    assert response["ok"] is False
    assert response["error_code"] == "invalid_host_envelope"
    assert reason in response["error"]
    assert response["definitely_no_external_effect"] is True
    if operation_id == "move_messages":
        assert response["external_effect_status"] == "failed"
    assert touched == []


def test_main_refuses_an_envelope_of_another_contract() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(BUNDLE_ROOT), str(SDK_PARENT), env.get("PYTHONPATH", "")]
    )
    payload = _payload("list_mailboxes")
    payload["contract_version"] = "extension_host_v1"

    completed = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "main.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert completed.returncode == 2
    response = json.loads(completed.stdout)
    assert response["error_code"] == "invalid_host_envelope"
    assert response["definitely_no_external_effect"] is True
