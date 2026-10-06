"""Approved artifact-plane output for selected IMAP attachments."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from flowsteward_extension_sdk import (
    ArtifactAccessError,
    find_artifact_descriptor,
    write_artifact_bytes,
)
from flowsteward_extension_sdk.http import verified_platform_grant_signature
from imap_errors import ImapExtensionError
from imap_messages import MAX_ATTACHMENT_BYTES, Attachment, load_attachment
from imap_transport import MailboxClient

try:
    from flowsteward_extension_sdk import report_progress
except ImportError:  # a Core with an SDK older than 0.3.0 shows no progress

    def report_progress(
        message: str = "", *, done: int | None = None, total: int | None = None
    ) -> None:
        return None

_BINDING_KEY = "attachment_artifact_handle"
_GRANT_CONTENT_TYPE = "application/octet-stream"
ArtifactWriter = Callable[..., dict[str, Any]]

_ALLOWED_TYPES: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".csv": frozenset({"text/csv", "application/csv"}),
    ".txt": frozenset({"text/plain"}),
    ".xls": frozenset(
        {
            "application/vnd.ms-excel",
            "application/msexcel",
            "application/x-msexcel",
        }
    ),
    ".xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
}
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def get_attachment(
    client: MailboxClient,
    payload: dict[str, Any],
    input_payload: Mapping[str, Any],
    *,
    artifact_writer: ArtifactWriter = write_artifact_bytes,
) -> dict[str, object]:
    """Write one allowed attachment only through its signed static output grant."""
    grant_limit = _validate_output_grant(payload)
    report_progress("Reading the message")
    mailbox, uid, attachment = load_attachment(
        client,
        input_payload,
        max_size_bytes=grant_limit,
        on_download=lambda name: report_progress(f"Downloading {name} from the mailbox"),
    )
    _validate_attachment_type(attachment)
    checksum = hashlib.sha256(attachment.data).hexdigest()
    report_progress(f"Saving {attachment.filename} ({_size_text(len(attachment.data))})")
    try:
        write_result = artifact_writer(
            payload,
            attachment.data,
            binding_key=_BINDING_KEY,
            content_type=_GRANT_CONTENT_TYPE,
        )
    except Exception as exc:
        raise _artifact_output_unavailable() from exc
    if not isinstance(write_result, Mapping):
        raise _artifact_output_unavailable()
    artifact_handle = write_result.get("artifact_handle")
    if not isinstance(artifact_handle, str) or not artifact_handle.strip():
        raise _artifact_output_unavailable()
    return {
        "attachment_artifact_handle": artifact_handle,
        "attachment_metadata": {
            "mailbox": mailbox,
            "uid": uid,
            "attachment_id": attachment.attachment_id,
            "original_filename": attachment.filename,
            "content_type": attachment.content_type,
            "size_bytes": len(attachment.data),
            "sha256": checksum,
        },
    }


def _size_text(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{max(1, round(size_bytes / 1024))} KB"


def _validate_output_grant(payload: dict[str, Any]) -> int:
    try:
        descriptor = find_artifact_descriptor(
            payload,
            binding_key=_BINDING_KEY,
            role="output",
        )
        access = descriptor.get("access")
        if not isinstance(access, dict):
            raise ArtifactAccessError("Output artifact descriptor has no access grant")
        max_size = access.get("max_size_bytes")
        if (
            descriptor.get("role") != "output"
            or descriptor.get("binding_key") != _BINDING_KEY
            or descriptor.get("content_type") != _GRANT_CONTENT_TYPE
            or access.get("transport") != "presigned_url"
            or access.get("mode") not in {"write", "read_write"}
            or not isinstance(access.get("upload_url"), str)
            or not access["upload_url"].strip()
            or isinstance(max_size, bool)
            or not isinstance(max_size, int)
            or max_size <= 0
            or not verified_platform_grant_signature(descriptor, access)
        ):
            raise ArtifactAccessError("Output artifact grant is not usable")
        return max_size
    except (ArtifactAccessError, TypeError, ValueError) as exc:
        raise _artifact_output_unavailable() from exc


def _validate_attachment_type(attachment: Attachment) -> None:
    filename = attachment.filename.lower()
    extension = next((suffix for suffix in _ALLOWED_TYPES if filename.endswith(suffix)), "")
    if not extension or attachment.content_type not in _ALLOWED_TYPES[extension]:
        _attachment_type_disallowed()
    body = attachment.data
    valid_magic = {
        ".pdf": body.startswith(b"%PDF-"),
        ".csv": _is_text(body),
        ".txt": _is_text(body),
        ".xls": body.startswith(_OLE_MAGIC),
        ".xlsx": body.startswith(b"PK\x03\x04"),
    }[extension]
    if not valid_magic:
        _attachment_type_disallowed()


def _is_text(body: bytes) -> bool:
    if b"\x00" in body:
        return False
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _artifact_output_unavailable() -> ImapExtensionError:
    return ImapExtensionError(
        "artifact_output_unavailable",
        "The approved attachment artifact output is unavailable",
    )


def _attachment_type_disallowed() -> None:
    raise ImapExtensionError(
        "attachment_type_disallowed",
        "The selected attachment type is not allowed",
    )


__all__ = ["MAX_ATTACHMENT_BYTES", "get_attachment"]
