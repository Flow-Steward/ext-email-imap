from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from imap_errors import ImapExtensionError

_MESSAGE_USE_THE_FULL_EMAIL_ADDRESS = "Use the full email address."

SUPPORTED_TLS_MODES = frozenset({"ssl", "starttls"})
PUBLIC_PROVIDER_DOMAINS = frozenset(
    {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com"}
)
PUBLIC_PROVIDER_HOSTS = frozenset(
    {"imap.gmail.com", "outlook.office365.com", "imap-mail.outlook.com"}
)
_DOMAIN_PRESETS = {
    "yahoo.com": "yahoo",
    "fastmail.com": "fastmail",
    "zoho.com": "zoho_personal",
}
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CONNECTION_INPUT_FIELDS = frozenset(
    {
        "password",
        "secrets",
        "config",
        "connection_config",
        "provider_config",
        "provider_preset",
        "imap_host",
        "imap_port",
        "tls_mode",
        "username",
        "default_mailbox",
        "processed_mailbox",
        "trash_mailbox",
    }
)

# Values here are only form/discovery defaults.  The persisted connection remains
# the single source used by transport code.
PRESETS: dict[str, dict[str, object]] = {
    "yahoo": {
        "provider_preset": "yahoo",
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "tls_mode": "ssl",
        "username_guidance": _MESSAGE_USE_THE_FULL_EMAIL_ADDRESS,
        "password_guidance": "Use an app password.",
    },
    "fastmail": {
        "provider_preset": "fastmail",
        "imap_host": "imap.fastmail.com",
        "imap_port": 993,
        "tls_mode": "ssl",
        "username_guidance": _MESSAGE_USE_THE_FULL_EMAIL_ADDRESS,
        "password_guidance": "Use an app password.",
    },
    "zoho_personal": {
        "provider_preset": "zoho_personal",
        "imap_host": "imap.zoho.com",
        "imap_port": 993,
        "tls_mode": "ssl",
        "username_guidance": _MESSAGE_USE_THE_FULL_EMAIL_ADDRESS,
    },
    "zoho_pro": {
        "provider_preset": "zoho_pro",
        "imap_host": "imappro.zoho.com",
        "imap_port": 993,
        "tls_mode": "ssl",
        "username_guidance": _MESSAGE_USE_THE_FULL_EMAIL_ADDRESS,
    },
    "cpanel_shared_hosting": {
        "provider_preset": "cpanel_shared_hosting",
        "imap_host": "",
        "imap_port": 993,
        "tls_mode": "ssl",
        "username_guidance": _MESSAGE_USE_THE_FULL_EMAIL_ADDRESS,
        "host_guidance": "Use the IMAP host from the hosting account.",
        "starttls_port": 143,
    },
    "manual": {
        "provider_preset": "manual",
        "imap_host": "",
        "imap_port": None,
        "tls_mode": None,
        "host_guidance": "Enter the IMAP host supplied by the mailbox provider.",
    },
}


@dataclass(frozen=True)
class ImapConnectionConfig:
    provider_preset: str
    imap_host: str
    imap_port: int
    tls_mode: str
    username: str
    password: str = field(repr=False)
    default_mailbox: str = ""
    processed_mailbox: str = ""
    trash_mailbox: str = ""
    connect_timeout: float = 10.0
    read_timeout: float = 30.0


def connection_config_from_payload(payload: Mapping[str, Any] | None) -> ImapConnectionConfig:
    """Parse one host-injected IMAP connection without accepting user overrides."""
    return _validated_connection_config(payload, require_password=True)


def discovery_config_from_payload(payload: Mapping[str, Any] | None) -> ImapConnectionConfig:
    """Parse saved non-secret settings for local discovery without reading secrets."""
    return _validated_connection_config(payload, require_password=False)


def _validated_connection_config(
    payload: Mapping[str, Any] | None,
    *,
    require_password: bool,
) -> ImapConnectionConfig:
    root = _as_mapping(payload)
    operation_input = _operation_input(root)
    _reject_connection_input_overrides(operation_input)
    connection = _connection_payload(root)
    _validate_connection_ref(operation_input, connection)

    connection_type = _text(
        connection.get("connection_type_id")
        or connection.get("connection_type")
        or connection.get("type")
    ).lower()
    if connection_type and connection_type != "imap_mailbox":
        _invalid_connection()

    config = _as_mapping(
        connection.get("config")
        or connection.get("connection_config")
        or connection.get("provider_config")
    )
    if _contains_password_field(config):
        _invalid_connection()

    provider_preset = _required_text(config.get("provider_preset")).lower()
    if provider_preset not in PRESETS:
        _invalid_connection()
    imap_host = normalize_host(_required_text(config.get("imap_host")))
    imap_port = _port(config.get("imap_port"))
    tls_mode = _required_text(config.get("tls_mode"))
    if tls_mode not in SUPPORTED_TLS_MODES:
        _invalid_connection()
    username = _required_text(config.get("username"))
    _reject_control_characters(username)
    password = ""
    if require_password:
        password_value = _as_mapping(connection.get("secrets")).get("password")
        if not isinstance(password_value, str) or not password_value:
            _invalid_connection()
        password = password_value

    _reject_unsupported_provider(host=imap_host, username=username)
    return ImapConnectionConfig(
        provider_preset=provider_preset,
        imap_host=imap_host,
        imap_port=imap_port,
        tls_mode=tls_mode,
        username=username,
        password=password,
        default_mailbox=_optional_mailbox(config.get("default_mailbox")),
        processed_mailbox=_optional_mailbox(config.get("processed_mailbox")),
        trash_mailbox=_optional_mailbox(config.get("trash_mailbox")),
        connect_timeout=_timeout(config.get("connect_timeout"), default=10.0, maximum=60.0),
        read_timeout=_timeout(config.get("read_timeout"), default=30.0, maximum=300.0),
    )


def discover_settings(config: ImapConnectionConfig) -> dict[str, object]:
    """Return local preset guidance only; this function deliberately performs no I/O."""
    provider_preset = config.provider_preset
    if provider_preset == "manual":
        provider_preset = _DOMAIN_PRESETS.get(_email_domain(config.username), "manual")
    preset = PRESETS[provider_preset]
    if provider_preset != "manual":
        return {**preset, "requires_user_confirmation": False, "candidates": []}

    domain = _email_domain(config.username)
    candidates = (
        [
            {"imap_host": f"imap.{domain}", "requires_user_confirmation": True},
            {"imap_host": f"mail.{domain}", "requires_user_confirmation": True},
        ]
        if domain
        else []
    )
    return {
        **preset,
        "requires_user_confirmation": True,
        "candidates": candidates,
    }


def normalize_host(value: str) -> str:
    """Accept exactly a DNS hostname or literal IP and return its comparison form."""
    if not isinstance(value, str) or not value:
        _invalid_connection()
    _reject_control_characters(value)
    if any(character in value for character in ("://", "@", "/", "?", "#", "\\", "%", "[", "]")):
        _invalid_connection()
    host = value.lower().rstrip(".")
    if not host or value.endswith(".."):
        _invalid_connection()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        normalized = host.encode("idna").decode("ascii")
    except UnicodeError:
        _invalid_connection()
    if len(normalized) > 253 or any(
        not label or not _HOST_LABEL.fullmatch(label) for label in normalized.split(".")
    ):
        _invalid_connection()
    return normalized


def _connection_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    for candidate in (
        _as_mapping(_as_mapping(payload.get("action")).get("target")).get("connection"),
        _as_mapping(_as_mapping(payload.get("action_payload")).get("target")).get("connection"),
        _as_mapping(payload.get("target")).get("connection"),
    ):
        connection = _as_mapping(candidate)
        if connection:
            return connection
    provider = _as_mapping(payload.get("provider"))
    if provider:
        return {
            "connection_type_id": provider.get("connection_type_id"),
            "connection_id": provider.get("connection_id") or provider.get("connection_ref"),
            "config": _as_mapping(provider.get("provider_config") or provider.get("config")),
            "secrets": _as_mapping(provider.get("secrets")),
        }
    _invalid_connection()
    raise AssertionError("unreachable")


def _operation_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    action = _as_mapping(payload.get("action"))
    for candidate in (action.get("input"), payload.get("input"), payload.get("request")):
        value = _as_mapping(candidate)
        if value:
            return value
    return {}


def _validate_connection_ref(
    operation_input: Mapping[str, Any], connection: Mapping[str, Any]
) -> None:
    public_ref = operation_input.get("connection_ref")
    injected_ref = (
        connection.get("connection_id")
        or connection.get("project_connection_id")
        or connection.get("connection_ref")
    )
    if public_ref is not None and (not isinstance(public_ref, str) or not public_ref):
        _invalid_connection()
    if injected_ref is not None and (not isinstance(injected_ref, str) or not injected_ref):
        _invalid_connection()
    if public_ref is not None and injected_ref is not None and public_ref != injected_ref:
        _invalid_connection()


def _reject_connection_input_overrides(operation_input: Mapping[str, Any]) -> None:
    if any(str(key).lower() in _CONNECTION_INPUT_FIELDS for key in operation_input):
        _invalid_connection()
    if _contains_password_field(operation_input):
        _invalid_connection()


def _contains_password_field(value: Mapping[str, Any]) -> bool:
    for key, nested in value.items():
        if str(key).lower() == "password":
            return True
        if isinstance(nested, Mapping) and _contains_password_field(nested):
            return True
        if isinstance(nested, list) and any(
            isinstance(item, Mapping) and _contains_password_field(item) for item in nested
        ):
            return True
    return False


def _reject_unsupported_provider(*, host: str, username: str) -> None:
    if host in PUBLIC_PROVIDER_HOSTS or _email_domain(username) in PUBLIC_PROVIDER_DOMAINS:
        raise ImapExtensionError(
            "unsupported_provider",
            "Use the official Gmail or Microsoft connector for this mailbox.",
        )


def _email_domain(username: str) -> str:
    if "@" not in username:
        return ""
    raw_domain = username.rsplit("@", 1)[1]
    try:
        return normalize_host(raw_domain)
    except ImapExtensionError:
        return ""


def _port(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        _invalid_connection()
    return value


def _timeout(value: Any, *, default: float, maximum: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        _invalid_connection()
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        _invalid_connection()
    if not math.isfinite(timeout) or not 0 < timeout <= maximum:
        _invalid_connection()
    return timeout


def _optional_mailbox(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _invalid_connection()
    _reject_control_characters(value)
    return value


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        _invalid_connection()
    return value


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _reject_control_characters(value: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _invalid_connection()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _invalid_connection() -> None:
    raise ImapExtensionError("invalid_connection", "IMAP connection configuration is invalid.")
