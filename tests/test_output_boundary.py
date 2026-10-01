"""`main.py` must keep its answer inside the host's stdout cap.

A mailbox reply can be arbitrarily large — a one-megabyte body, a search result
over thousands of messages — and the host truncates anything past its cap, which
would turn a valid answer into unparseable JSON. This bundle sanitizes and, when
it cannot, replaces the payload with a structured error.

This lives here rather than in the platform's suite: it runs this bundle's own
entrypoint and asserts this bundle's behaviour.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
#: The same cap the host applies. Kept as a literal so a change on either side
#: shows up as a failing test rather than a silently truncated response.
HOST_STDOUT_CAP_BYTES = 5 * 1024 * 1024


def _run_main_with_response(response_expression: str) -> tuple[int, str, str]:
    script = "\n".join(
        [
            "import main",
            f"main.handle_payload = lambda _payload: {response_expression}",
            "raise SystemExit(main.main())",
        ]
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(BUNDLE_ROOT),
        input="{}",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_main_keeps_one_mib_control_heavy_message_inside_the_host_stdout_cap() -> None:
    returncode, stdout, stderr = _run_main_with_response(
        "{'ok': True, 'result': {'message': {"
        "'plain_text': chr(0) * (1024 * 1024), 'subject': 'Žinutės'}}}"
    )

    assert returncode == 0, stderr
    assert stderr == ""
    assert len(stdout.encode("utf-8")) <= HOST_STDOUT_CAP_BYTES
    response = json.loads(stdout)
    assert response["ok"] is True
    assert response["output_sanitized"] is True
    # Control characters are replaced, not dropped, so offsets stay meaningful.
    assert response["result"]["message"]["plain_text"] == " " * (1024 * 1024)
    # Non-ASCII text survives intact.
    assert response["result"]["message"]["subject"] == "Žinutės"


def test_main_replaces_an_oversized_search_response_with_a_structured_error() -> None:
    """Past the cap the only safe answer is an error the host can parse."""
    returncode, stdout, stderr = _run_main_with_response(
        "{'ok': True, 'result': {'messages': [{'uid': 7, 'subject': 'a' * (5 * 1024 * 1024)}]}}"
    )

    assert returncode == 2, stderr
    assert stderr == ""
    response = json.loads(stdout)
    assert response["ok"] is False
    assert response["error_code"] == "response_too_large"
    assert response["result"] == {}
