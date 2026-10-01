from __future__ import annotations

import base64
import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
SDK_PARENT = Path(__file__).resolve().parents[3] / "core/infrastructure/extension_sdk/public"
for import_root in (BUNDLE_ROOT, SDK_PARENT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fake_mailbox_client import FakeMailboxClient  # noqa: E402
from flowsteward_extension_sdk.http import platform_grant_signing_payload  # noqa: E402
from imap_artifacts import MAX_ATTACHMENT_BYTES, get_attachment  # noqa: E402
from imap_errors import ImapExtensionError  # noqa: E402


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _payload_with_output_grant(
    monkeypatch, *, valid: bool = True, max_size_bytes: int = MAX_ATTACHMENT_BYTES
) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "artifact_id": "artifact-7",
        "artifact_handle": "artifact:artifact-7",
        "artifact_kind": "artifact",
        "binding_key": "attachment_artifact_handle",
        "role": "output",
        "content_type": "application/octet-stream",
        "scope": {"account_id": "account-1", "project_id": "project-1", "job_id": "job-1"},
        "access": {
            "transport": "presigned_url",
            "mode": "write",
            "upload_url": "https://artifacts.example.test/upload/artifact-7",
            "max_size_bytes": max_size_bytes,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "single_use": True,
            "required_headers": {},
        },
    }
    access = descriptor["access"]
    assert isinstance(access, dict)
    key = Ed25519PrivateKey.generate()
    signature = key.sign(platform_grant_signing_payload(descriptor, access))
    access["platform_grant"] = {"version": 1, "signature": _b64url(signature)}
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv("FS_ARTIFACT_GRANT_VERIFY_KEY", _b64url(public_key))
    if not valid:
        access["upload_url"] = "https://forged.example.test/upload"
    return {"artifacts": {"outputs": [descriptor]}}


def _attachment_leaf(
    minor: bytes,
    *,
    filename: str,
    size: int,
) -> tuple[object, ...]:
    return (
        b"APPLICATION" if minor not in {b"CSV", b"PLAIN"} else b"TEXT",
        minor,
        (b"NAME", filename.encode()),
        None,
        None,
        b"BASE64",
        size,
        None,
        (b"ATTACHMENT", (b"FILENAME", filename.encode())),
        None,
        None,
    )


def _attachment_client(
    body: bytes,
    *,
    filename: str = "invoice.pdf",
    content_type: str = "application/pdf",
    declared_size: int | None = None,
) -> FakeMailboxClient:
    major, minor = content_type.split("/", 1)
    encoded = base64.b64encode(body)
    leaf = (
        major.upper().encode(),
        minor.upper().encode(),
        (b"NAME", filename.encode()),
        None,
        None,
        b"BASE64",
        len(encoded) if declared_size is None else declared_size,
        None,
        (b"ATTACHMENT", (b"FILENAME", filename.encode())),
        None,
        None,
    )
    return FakeMailboxClient(
        uids=[7],
        bodystructures={7: (leaf, b"MIXED", (b"BOUNDARY", b"x"), None, None)},
        message_sections={
            7: {
                "BODY[1.MIME]": (
                    f'Content-Type: {content_type}; name="{filename}"\r\n'
                    f'Content-Disposition: attachment; filename="{filename}"\r\n'
                    "Content-Transfer-Encoding: base64\r\n\r\n"
                ).encode(),
                "BODY[1]": encoded,
            }
        },
    )


def _attachment_input() -> dict[str, object]:
    return {"mailbox": "INBOX", "uid": 7, "attachment_id": "1"}


def _base64_fetch_limit(decoded_limit: int) -> int:
    encoded_characters = 4 * ((decoded_limit + 2) // 3)
    return encoded_characters + 2 * ((encoded_characters + 75) // 76)


def test_get_attachment_writes_only_selected_bytes_to_static_grant(monkeypatch) -> None:
    body = b"%PDF-1.7\ninvoice"
    client = _attachment_client(body)
    writes: list[tuple[bytes, dict[str, object]]] = []

    result = get_attachment(
        client,
        _payload_with_output_grant(monkeypatch),
        _attachment_input(),
        artifact_writer=lambda _payload, data, **kwargs: (
            writes.append((data, kwargs))
            or {
                "artifact_handle": "artifact:artifact-7",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ),
    )

    assert result == {
        "attachment_artifact_handle": "artifact:artifact-7",
        "attachment_metadata": {
            "mailbox": "INBOX",
            "uid": 7,
            "attachment_id": "1",
            "original_filename": "invoice.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        },
    }
    assert writes == [
        (
            body,
            {
                "binding_key": "attachment_artifact_handle",
                "content_type": "application/octet-stream",
            },
        )
    ]
    assert client.fetches[0] == ([7], ("UID", "RFC822.SIZE", "BODYSTRUCTURE"))
    literal_fields = [
        field for _uids, fields in client.fetches[1:] for field in fields if field != "UID"
    ]
    assert len(literal_fields) == 2
    assert all(field.startswith("BODY.PEEK[1") and "<" in field for field in literal_fields)
    assert "data" not in result
    assert "bytes" not in result
    assert "path" not in result


def test_missing_or_forged_artifact_output_fails_before_message_fetch(monkeypatch) -> None:
    for payload in ({}, _payload_with_output_grant(monkeypatch, valid=False)):
        client = _attachment_client(b"%PDF-1.7\ninvoice")

        _raises_input_188_1 = _attachment_input()
        with pytest.raises(ImapExtensionError) as error:
            get_attachment(client, payload, _raises_input_188_1)

        assert error.value.code == "artifact_output_unavailable"
        assert client.fetches == []


@pytest.mark.parametrize(
    ("filename", "content_type", "body"),
    [
        ("invoice.pdf", "application/pdf", b"%PDF-1.7\nrow"),
        ("stock.csv", "text/csv", b"sku,quantity\nA-1,4\n"),
        ("notes.txt", "text/plain", b"approved\n"),
        ("legacy.xls", "application/vnd.ms-excel", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1payload"),
        (
            "stock.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            b"PK\x03\x04workbook",
        ),
    ],
)
def test_allows_supported_attachment_mime_extension_and_magic(
    monkeypatch, filename: str, content_type: str, body: bytes
) -> None:
    result = get_attachment(
        _attachment_client(body, filename=filename, content_type=content_type),
        _payload_with_output_grant(monkeypatch),
        _attachment_input(),
        artifact_writer=lambda _payload, data, **_kwargs: {
            "artifact_handle": "artifact:allowed",
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )

    assert result["attachment_metadata"]["original_filename"] == filename
    assert result["attachment_metadata"]["content_type"] == content_type


@pytest.mark.parametrize(
    ("filename", "content_type", "body"),
    [
        ("script.exe", "application/octet-stream", b"MZpayload"),
        ("invoice.pdf", "application/pdf", b"not a pdf"),
        ("stock.xlsx", "application/zip", b"PK\x03\x04workbook"),
        ("stock.csv", "text/csv", b"sku\x00quantity"),
    ],
)
def test_rejects_disallowed_or_mismatched_attachment_types_without_writing(
    monkeypatch, filename: str, content_type: str, body: bytes
) -> None:
    writes: list[bytes] = []

    _raises_input_241_1 = _attachment_client(body, filename=filename, content_type=content_type)
    _raises_input_241_2 = _payload_with_output_grant(monkeypatch)
    _raises_input_241_3 = _attachment_input()

    def _raises_input_241_4(_payload, data, **_kwargs):
        return writes.append(data)

    with pytest.raises(ImapExtensionError) as error:
        get_attachment(
            _raises_input_241_1,
            _raises_input_241_2,
            _raises_input_241_3,
            artifact_writer=_raises_input_241_4,
        )

    assert error.value.code == "attachment_type_disallowed"
    assert writes == []
    assert filename not in error.value.message


def test_attachment_size_is_enforced_before_body_fetch_and_after_decode(monkeypatch) -> None:
    payload = _payload_with_output_grant(monkeypatch)
    client = _attachment_client(
        b"%PDF-1.7\nsmall",
        declared_size=_base64_fetch_limit(MAX_ATTACHMENT_BYTES) + 1,
    )

    _raises_input_261_1 = _attachment_input()
    with pytest.raises(ImapExtensionError) as metadata_error:
        get_attachment(client, payload, _raises_input_261_1)

    assert metadata_error.value.code == "attachment_too_large"
    assert client.fetches == [([7], ("UID", "RFC822.SIZE", "BODYSTRUCTURE"))]

    oversized = _attachment_client(
        b"%PDF-1.7\n" + b"x" * MAX_ATTACHMENT_BYTES,
        declared_size=1,
    )
    _raises_input_271_1 = _attachment_input()
    with pytest.raises(ImapExtensionError) as decoded_error:
        get_attachment(oversized, payload, _raises_input_271_1)

    assert decoded_error.value.code == "attachment_too_large"
    body_requests = [
        field
        for _uids, fields in oversized.fetches
        for field in fields
        if field.startswith("BODY.PEEK[1]<")
    ]
    requested_bytes = sum(int(field.rsplit(".", 1)[1][:-1]) for field in body_requests)
    assert MAX_ATTACHMENT_BYTES < requested_bytes <= (_base64_fetch_limit(MAX_ATTACHMENT_BYTES) + 1)


def test_base64_attachment_at_decoded_limit_uses_separate_encoded_fetch_budget(
    monkeypatch,
) -> None:
    body = b"%PDF-" + b"x" * (MAX_ATTACHMENT_BYTES - len(b"%PDF-"))
    client = _attachment_client(
        body,
        declared_size=len(base64.b64encode(body)),
    )
    writes: list[int] = []

    result = get_attachment(
        client,
        _payload_with_output_grant(monkeypatch),
        _attachment_input(),
        artifact_writer=lambda _payload, data, **_kwargs: {
            "artifact_handle": "artifact:decoded-limit",
            "size_bytes": writes.append(len(data)) or len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )

    assert writes == [MAX_ATTACHMENT_BYTES]
    assert result["attachment_metadata"]["size_bytes"] == MAX_ATTACHMENT_BYTES
    requested_bytes = sum(
        int(field.rsplit(".", 1)[1][:-1])
        for _uids, fields in client.fetches
        for field in fields
        if field.startswith("BODY.PEEK[1]<")
    )
    assert requested_bytes > MAX_ATTACHMENT_BYTES


def test_static_grant_size_is_enforced_before_attachment_body_fetch(monkeypatch) -> None:
    body = b"%PDF-1.7\ninvoice"
    client = _attachment_client(body, declared_size=len(base64.b64encode(body)))

    _raises_input_321_1 = _payload_with_output_grant(monkeypatch, max_size_bytes=12)
    _raises_input_321_2 = _attachment_input()
    with pytest.raises(ImapExtensionError) as error:
        get_attachment(client, _raises_input_321_1, _raises_input_321_2)

    assert error.value.code == "attachment_too_large"
    assert client.fetches == [([7], ("UID", "RFC822.SIZE", "BODYSTRUCTURE"))]


def test_missing_attachment_id_is_safe_and_contains_no_body_or_secret(monkeypatch) -> None:
    secret = "MAILBOX_SECRET_123"
    body = b"%PDF-1.7\nCONFIDENTIAL_BODY_456"
    payload = _payload_with_output_grant(monkeypatch)
    payload["action"] = {"target": {"connection": {"secrets": {"password": secret}}}}

    _raises_input_338_1 = _attachment_client(body)
    with pytest.raises(ImapExtensionError) as error:
        get_attachment(
            _raises_input_338_1, payload, {"mailbox": "INBOX", "uid": 7, "attachment_id": "99"}
        )

    rendered = f"{error.value.code} {error.value.message} {error.value}"
    assert error.value.code == "attachment_not_found"
    assert secret not in rendered
    assert "CONFIDENTIAL_BODY_456" not in rendered


def test_oversized_numeric_attachment_id_is_rejected_as_invalid_payload(monkeypatch) -> None:
    _raises_input_352_1 = _attachment_client(b"%PDF-1.7\ninvoice")
    _raises_input_352_2 = _payload_with_output_grant(monkeypatch)
    with pytest.raises(ImapExtensionError) as error:
        get_attachment(
            _raises_input_352_1,
            _raises_input_352_2,
            {"mailbox": "INBOX", "uid": 7, "attachment_id": "9" * 10000},
        )

    assert error.value.code == "invalid_payload"


def test_attachment_literals_use_bounded_partial_peek_fetches(monkeypatch) -> None:
    client = _attachment_client(b"%PDF-1.7\ninvoice")

    get_attachment(
        client,
        _payload_with_output_grant(monkeypatch),
        _attachment_input(),
        artifact_writer=lambda _payload, data, **_kwargs: {
            "artifact_handle": "artifact:bounded",
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )

    literal_fields = [
        field
        for _uids, fields in client.fetches
        for field in fields
        if field.startswith("BODY.PEEK[")
    ]
    assert literal_fields
    assert all("<" in field and field.endswith(">") for field in literal_fields)
