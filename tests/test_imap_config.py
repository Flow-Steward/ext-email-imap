from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
if str(BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BUNDLE_ROOT))

from imap_config import (  # noqa: E402
    PRESETS,
    connection_config_from_payload,
    discover_settings,
    discovery_config_from_payload,
    normalize_host,
)
from imap_errors import ImapExtensionError  # noqa: E402


def _payload(
    *,
    config: dict[str, object] | None = None,
    secrets: dict[str, object] | None = None,
    input_payload: dict[str, object] | None = None,
    connection_id: str = "conn-imap",
) -> dict[str, object]:
    saved_config: dict[str, object] = {
        "provider_preset": "manual",
        "imap_host": "mail.example.test",
        "imap_port": 993,
        "tls_mode": "ssl",
        "username": "ops@example.test",
    }
    saved_config.update(config or {})
    return {
        "action": {
            "target": {
                "connection": {
                    "connection_id": connection_id,
                    "connection_type_id": "imap_mailbox",
                    "config": saved_config,
                    "secrets": {"password": "secret value  "} if secrets is None else secrets,
                }
            },
            "input": input_payload or {"connection_ref": connection_id},
        }
    }


def _config_from(*, username: str = "ops@example.test", provider_preset: str = "manual"):
    return connection_config_from_payload(
        _payload(config={"username": username, "provider_preset": provider_preset})
    )


def test_parses_saved_connection_and_preserves_secret_verbatim() -> None:
    config = connection_config_from_payload(
        _payload(
            config={
                "imap_host": "BÜCHER.Example.",
                "imap_port": 143,
                "tls_mode": "starttls",
                "default_mailbox": "INBOX",
                "processed_mailbox": "Processed",
                "trash_mailbox": "Trash",
            }
        )
    )

    assert config.imap_host == "xn--bcher-kva.example"
    assert config.imap_port == 143
    assert config.tls_mode == "starttls"
    assert config.password == "secret value  "
    assert config.default_mailbox == "INBOX"


@pytest.mark.parametrize(
    "preset, host, port, tls_mode",
    [
        ("yahoo", "imap.mail.yahoo.com", 993, "ssl"),
        ("fastmail", "imap.fastmail.com", 993, "ssl"),
        ("zoho_personal", "imap.zoho.com", 993, "ssl"),
        ("zoho_pro", "imappro.zoho.com", 993, "ssl"),
        ("cpanel_shared_hosting", "", 993, "ssl"),
        ("manual", "", None, None),
    ],
)
def test_all_presets_expose_editable_defaults(
    preset: str, host: str, port: int | None, tls_mode: str | None
) -> None:
    settings = PRESETS[preset]

    assert settings["provider_preset"] == preset
    assert settings["imap_host"] == host
    assert settings["imap_port"] == port
    assert settings["tls_mode"] == tls_mode


def test_connection_form_uses_the_canonical_cpanel_preset_id() -> None:
    form = yaml.safe_load((BUNDLE_ROOT / "ui/components/imap_connection_form.yaml").read_text())
    preset_values = {
        option["value"]
        for field in form["fields"]
        if field["path"] == "provider_preset"
        for option in field["options"]
    }

    assert "cpanel_shared_hosting" in preset_values
    assert "cpanel" not in preset_values


def test_connection_form_projects_provider_presets_as_editable_imap_defaults() -> None:
    form = yaml.safe_load((BUNDLE_ROOT / "ui/components/imap_connection_form.yaml").read_text())

    assert form["data"]["connection_type_field"] == "provider_preset"
    assert form["data"]["default_connection_type"] == "manual"
    assert form["data"]["connection_types"] == {
        "manual": {
            "connection_type": "imap_mailbox",
            "defaults": {"imap_host": "", "imap_port": 993, "tls_mode": "ssl"},
        },
        "yahoo": {
            "connection_type": "imap_mailbox",
            "defaults": {
                "imap_host": "imap.mail.yahoo.com",
                "imap_port": 993,
                "tls_mode": "ssl",
            },
        },
        "fastmail": {
            "connection_type": "imap_mailbox",
            "defaults": {
                "imap_host": "imap.fastmail.com",
                "imap_port": 993,
                "tls_mode": "ssl",
            },
        },
        "zoho_personal": {
            "connection_type": "imap_mailbox",
            "defaults": {
                "imap_host": "imap.zoho.com",
                "imap_port": 993,
                "tls_mode": "ssl",
            },
        },
        "zoho_pro": {
            "connection_type": "imap_mailbox",
            "defaults": {
                "imap_host": "imappro.zoho.com",
                "imap_port": 993,
                "tls_mode": "ssl",
            },
        },
        "cpanel_shared_hosting": {
            "connection_type": "imap_mailbox",
            "defaults": {"imap_host": "", "imap_port": 993, "tls_mode": "ssl"},
        },
    }


def test_discover_known_preset_returns_confirmed_defaults_without_network(monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("dns"))

    result = discover_settings(_config_from(provider_preset="fastmail"))

    assert result == {
        **PRESETS["fastmail"],
        "requires_user_confirmation": False,
        "candidates": [],
    }


def test_discover_manual_yahoo_address_resolves_to_confirmed_preset_without_network(
    monkeypatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("dns"))

    result = discover_settings(_config_from(username="ops@Yahoo.COM.", provider_preset="manual"))

    assert result == {
        **PRESETS["yahoo"],
        "requires_user_confirmation": False,
        "candidates": [],
    }


def test_discover_unknown_domain_returns_unconfirmed_candidates_without_network(
    monkeypatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("dns"))

    result = discover_settings(
        _config_from(username="ops@BÜCHER.Example.", provider_preset="manual")
    )

    assert result["provider_preset"] == "manual"
    assert result["requires_user_confirmation"] is True
    assert result["candidates"] == [
        {"imap_host": "imap.xn--bcher-kva.example", "requires_user_confirmation": True},
        {"imap_host": "mail.xn--bcher-kva.example", "requires_user_confirmation": True},
    ]


def test_discovery_config_validates_non_secret_settings_without_password_or_network(
    monkeypatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("dns"))
    payload = _payload(secrets={})
    del payload["action"]["target"]["connection"]["secrets"]

    config = discovery_config_from_payload(payload)

    assert config.provider_preset == "manual"
    assert config.imap_host == "mail.example.test"
    assert config.imap_port == 993
    assert config.tls_mode == "ssl"
    assert config.username == "ops@example.test"
    assert config.password == ""


@pytest.mark.parametrize(
    "invalid_config",
    [
        {"provider_preset": "unknown"},
        {"imap_host": "https://mail.example.test"},
        {"imap_port": "993"},
        {"tls_mode": "none"},
        {"username": ""},
    ],
)
def test_discovery_config_reuses_authenticated_non_secret_validation(
    invalid_config: dict[str, object],
) -> None:
    _raises_input_242_1 = _payload(config=invalid_config, secrets={})
    with pytest.raises(ImapExtensionError) as error:
        discovery_config_from_payload(_raises_input_242_1)

    assert error.value.code == "invalid_connection"


@pytest.mark.parametrize(
    "username",
    [
        "a@gmail.com",
        "a@googlemail.com",
        "a@outlook.com",
        "a@hotmail.com",
        "a@live.com",
        "a@msn.com",
    ],
)
def test_public_google_and_microsoft_domains_are_rejected_before_network(username: str) -> None:
    with pytest.raises(ImapExtensionError, match="Use the official") as error:
        _config_from(username=username)

    assert error.value.code == "unsupported_provider"


@pytest.mark.parametrize(
    "host", ["imap.gmail.com", "outlook.office365.com", "imap-mail.outlook.com"]
)
def test_public_google_and_microsoft_hosts_are_rejected(host: str) -> None:
    _raises_input_270_1 = _payload(config={"imap_host": host})
    with pytest.raises(ImapExtensionError) as error:
        connection_config_from_payload(_raises_input_270_1)

    assert error.value.code == "unsupported_provider"


@pytest.mark.parametrize(
    "host",
    [
        "https://mail.example.test",
        "user@mail.example.test",
        "mail.example.test/inbox",
        "mail.example.test?query=yes",
        "mail.example.test#fragment",
        "mail.example.test:993",
        "mail.example.test\n",
        "",
    ],
)
def test_rejects_non_host_syntax_and_control_characters(host: str) -> None:
    _raises_input_290_1 = _payload(config={"imap_host": host})
    with pytest.raises(ImapExtensionError) as error:
        connection_config_from_payload(_raises_input_290_1)

    assert error.value.code == "invalid_connection"


def test_normalizes_bare_ipv6_literal_without_treating_it_as_a_host_port() -> None:
    assert normalize_host("2001:0DB8:0:0:0:0:0:1") == "2001:db8::1"
    assert connection_config_from_payload(
        _payload(config={"imap_host": "2001:db8::1"})
    ).imap_host == ("2001:db8::1")


@pytest.mark.parametrize("host", ["[2001:db8::1]", "[2001:db8::1]:993", "mail.example.test:993"])
def test_rejects_bracketed_or_dns_host_port_syntax(host: str) -> None:
    with pytest.raises(ImapExtensionError) as error:
        normalize_host(host)

    assert error.value.code == "invalid_connection"


@pytest.mark.parametrize("port", [0, 65536, -1, "993", True, 993.0])
def test_rejects_invalid_imap_ports(port: object) -> None:
    _raises_input_313_1 = _payload(config={"imap_port": port})
    with pytest.raises(ImapExtensionError) as error:
        connection_config_from_payload(_raises_input_313_1)

    assert error.value.code == "invalid_connection"


@pytest.mark.parametrize("tls_mode", ["none", "tls", "SSL", "starttls ", ""])
def test_rejects_invalid_tls_mode(tls_mode: str) -> None:
    _raises_input_321_1 = _payload(config={"tls_mode": tls_mode})
    with pytest.raises(ImapExtensionError) as error:
        connection_config_from_payload(_raises_input_321_1)

    assert error.value.code == "invalid_connection"


@pytest.mark.parametrize(
    "payload",
    [
        _payload(config={"password": "config-secret"}),
        _payload(input_payload={"connection_ref": "conn-imap", "password": "input-secret"}),
        _payload(secrets={}),
    ],
)
def test_password_must_be_in_injected_secrets_and_errors_do_not_disclose_it(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ImapExtensionError) as error:
        connection_config_from_payload(payload)

    assert error.value.code == "invalid_connection"
    assert "config-secret" not in str(error.value)
    assert "input-secret" not in str(error.value)


def test_rejects_connection_ref_that_does_not_match_injected_identity() -> None:
    _raises_input_347_1 = _payload(input_payload={"connection_ref": "conn-other"})
    with pytest.raises(ImapExtensionError) as error:
        connection_config_from_payload(_raises_input_347_1)

    assert error.value.code == "invalid_connection"


def test_rejects_wrong_connection_type() -> None:
    payload = _payload()
    payload["action"]["target"]["connection"]["connection_type_id"] = "smtp"

    with pytest.raises(ImapExtensionError) as error:
        connection_config_from_payload(payload)

    assert error.value.code == "invalid_connection"
