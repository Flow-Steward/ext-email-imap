from __future__ import annotations

import json


def check_health() -> dict[str, object]:
    return {
        "extension_id": "flowsteward.imap-mailbox",
        "ok": True,
        "status": "healthy",
    }


if __name__ == "__main__":
    print(json.dumps(check_health(), separators=(",", ":")))
