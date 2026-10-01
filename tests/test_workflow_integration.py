"""Portable workflow integration coverage for IMAP attachment outputs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SDK_PARENT = Path(__file__).resolve().parents[3] / "core/infrastructure/extension_sdk/public"
if str(SDK_PARENT) not in sys.path:
    sys.path.insert(0, str(SDK_PARENT))

from flowsteward_extension_sdk import write_artifact_bytes  # noqa: E402
from flowsteward_extension_sdk.testing import ExtensionWorkflowHarness  # noqa: E402

EXTENSION_ID = "flowsteward.imap-mailbox"


def _extension_record() -> dict[str, Any]:
    manifest = yaml.safe_load((BUNDLE_ROOT / "extension.yaml").read_text())
    actions = yaml.safe_load((BUNDLE_ROOT / manifest["action_manifest"]).read_text())
    contract_path = manifest["runtime"]["extension_contract_v2"]["artifact_policies"]
    policies = yaml.safe_load((BUNDLE_ROOT / contract_path).read_text())
    return {
        "extension_key": EXTENSION_ID,
        "action_definitions": actions["actions"],
        "compiled_manifest": {"artifact_policies": policies["policies"]},
    }


def test_attachment_handle_flows_unchanged_into_tabular_parsing(tmp_path) -> None:
    harness = ExtensionWorkflowHarness(artifact_root=tmp_path / "artifact_store")
    state = harness.initial_state(
        job_id="job-imap-inventory",
        workflow_id="wf.imap.inventory",
        account_id="acc-alpha",
        project_id="project-alpha",
    )

    def run_action(
        extension: dict[str, Any],
        action_id: str,
        input_payload: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert extension["extension_key"] == EXTENSION_ID
        assert action_id == "get_attachment"
        assert input_payload == {
            "connection_ref": "conn-imap-primary",
            "mailbox": "INBOX",
            "uid": 17,
            "attachment_id": "2",
        }
        outputs = list(kwargs.get("artifact_outputs") or [])
        assert outputs[0]["binding_key"] == "attachment_artifact_handle"
        write_result = write_artifact_bytes(
            {"artifacts": {"outputs": outputs}},
            b"sku,qty\nA1,2\n",
            binding_key="attachment_artifact_handle",
        )
        return {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {
                    "attachment_artifact_handle": write_result["artifact_handle"],
                    "attachment_metadata": {
                        "mailbox": "INBOX",
                        "uid": 17,
                        "attachment_id": "2",
                        "original_filename": "inventory.csv",
                        "content_type": "text/csv",
                        "size_bytes": 15,
                        "sha256": "a" * 64,
                    },
                },
            },
        }

    fetch_result = harness.execute_connector_step(
        step={
            "step_kind": "connector",
            "connector_id": f"{EXTENSION_ID}.get_attachment",
            "input_mapping_jsonb": {
                "connection_ref": "literal:conn-imap-primary",
                "mailbox": "literal:INBOX",
                "uid": 17,
                "attachment_id": "literal:2",
            },
            "output_mapping_jsonb": {"artifact_handle": "state.inventory_artifact"},
        },
        state=state,
        extensions={EXTENSION_ID: _extension_record()},
        run_action=run_action,
    )
    state.update(fetch_result["state_patch"])
    tabular_result = harness.execute_tabular_feed_step(
        step={
            "step_kind": "tabular_feed",
            "override": {
                "tabular_feed": {
                    "operation": "read",
                    "input": {"artifact_handle": "{{state.inventory_artifact}}"},
                    "key_column": "sku",
                    "result_target": "state.inventory_rows",
                }
            },
        },
        state=state,
    )

    assert fetch_result["ok"] is True
    assert (
        fetch_result["grant_outputs"]["attachment_artifact_handle"] == state["inventory_artifact"]
    )
    assert state["inventory_artifact"].startswith("artifact:")
    assert "attachment_context" not in state
    assert tabular_result["ok"] is True
    assert tabular_result["result"]["dataset_handle"].startswith("dst_")
    visible_state = {key: value for key, value in state.items() if key != "_artifact_plane"}
    assert "sku,qty" not in json.dumps(visible_state)
