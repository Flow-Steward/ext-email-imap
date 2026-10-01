#!/usr/bin/env python3
"""Generic IMAP mailbox subprocess entrypoint."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from imap_operations import handle_payload, invalid_payload_response

# The host's default stdout cap is 5 MiB. Reserve 512 KiB for the JSON envelope,
# framing, and future fixed metadata while keeping the two 1 MiB text fields useful.
MAX_SERIALIZED_RESPONSE_BYTES = 9 * 512 * 1024
_CONTROL_TRANSLATION = {
    codepoint: " " for codepoint in (*range(32), *range(127, 160)) if codepoint not in {9, 10, 13}
}


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        response = invalid_payload_response()
    else:
        response = handle_payload(payload)
    final_response, serialized = _bounded_serialized_response(response)
    sys.stdout.buffer.write(serialized)
    sys.stdout.buffer.write(b"\n")
    return 0 if final_response.get("ok") is True else 2


def _bounded_serialized_response(response: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    sanitized, changed = _sanitize_json_value(dict(response))
    if not isinstance(sanitized, dict):
        raise TypeError("extension response must be an object")
    if changed:
        sanitized["output_sanitized"] = True
    serialized = _json_bytes(sanitized)
    if len(serialized) + 1 <= MAX_SERIALIZED_RESPONSE_BYTES:
        return sanitized, serialized
    error_response = _response_too_large()
    return error_response, _json_bytes(error_response)


def _sanitize_json_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        sanitized = value.translate(_CONTROL_TRANSLATION)
        return sanitized, sanitized != value
    if isinstance(value, Mapping):
        output: dict[Any, Any] = {}
        changed = False
        for key, item in value.items():
            sanitized, item_changed = _sanitize_json_value(item)
            output[key] = sanitized
            changed = changed or item_changed
        return output, changed
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        output = []
        changed = False
        for item in value:
            sanitized, item_changed = _sanitize_json_value(item)
            output.append(sanitized)
            changed = changed or item_changed
        return output, changed
    return value, False


def _json_bytes(response: Mapping[str, Any]) -> bytes:
    return json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _response_too_large() -> dict[str, Any]:
    message = "The extension response exceeds its safe output limit"
    return {
        "ok": False,
        "result": {},
        "error_code": "response_too_large",
        "error": message,
        "errors": [{"code": "response_too_large", "message": message}],
    }


if __name__ == "__main__":
    raise SystemExit(main())
