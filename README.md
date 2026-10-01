# Generic IMAP Mailbox

`flowsteward.imap-mailbox` reads and explicitly post-processes mail through a
project-scoped IMAP connection. It is intended for providers that do not have a
dedicated Flow Steward connector, including Fastmail, Yahoo, Zoho, shared-hosting
mailboxes, and self-hosted IMAP servers.

Use the official Gmail or Microsoft connector for Gmail, Google Workspace,
Outlook.com, or Microsoft 365. This extension rejects their known public mailbox
domains and hosts instead of attempting a password or OAuth fallback.

## Configure a connection

Create an `imap_mailbox` project connection. Store the mailbox or provider app
password in the connection's `password` secret; do not place it in workflow inputs
or non-secret config. Providers that support app passwords commonly require one
when multi-factor authentication is enabled. Generate and revoke that credential in
the provider account rather than reusing the account's interactive password.

The connection requires these non-secret fields:

- `provider_preset`
- `imap_host`
- `imap_port`
- `tls_mode` (`ssl` or `starttls`)
- `username` (normally the full email address)

`default_mailbox`, `processed_mailbox`, and `trash_mailbox` are optional. Configure
the latter two when workflows move processed messages or use the default
`move_to_trash` delete mode.

Presets are editable convenience defaults, not provider-specific runtime branches:

| Preset | Host | Port | TLS | Notes |
| --- | --- | ---: | --- | --- |
| `yahoo` | `imap.mail.yahoo.com` | 993 | SSL | Full email username; use a Yahoo app password. |
| `fastmail` | `imap.fastmail.com` | 993 | SSL | Full email username; use a Fastmail app password. |
| `zoho_personal` | `imap.zoho.com` | 993 | SSL | Confirm IMAP access is enabled for the account. |
| `zoho_pro` | `imappro.zoho.com` | 993 | SSL | Confirm the organization's current mail policy. |
| `cpanel_shared_hosting` | Operator supplied | 993 or 143 | SSL or STARTTLS | Use the exact hostname from the hosting account. |
| `manual` | Operator supplied | Operator supplied | SSL or STARTTLS | Use for other or self-hosted providers. |

`discover_settings` returns known preset defaults or unconfirmed `imap.<domain>` and
`mail.<domain>` candidates. It never scans ports or logs in. Confirm unknown settings
with the provider before saving them. `test_connection` is the explicit operation
that resolves the host, verifies TLS, and authenticates.

## Workflow operations

The extension exposes mailbox discovery, connection testing, mailbox listing,
UID-based search, bounded message and attachment retrieval, flag updates, message
moves, and deletion. Mutating operations are separate actions and report whether an
external mailbox effect succeeded, failed before any effect, or may be ambiguous.

This is an IMAP-only extension. It does not implement SMTP or send mail. Route
outbound messages through the separately configured outgoing-email or official
provider connector so read credentials and send credentials remain independent.

### Attachment handoff

`get_attachment` writes the selected attachment to the platform artifact grant and
returns `attachment_artifact_handle`. It never returns attachment bytes, base64
content, or a local file path. Pass that handle unchanged to the next workflow step:

- For invoice PDFs, pass `attachment_artifact_handle` to the Docling document
  extraction workflow and validate the extracted invoice fields before downstream
  accounting actions.
- For CSV, XLS, or XLSX attachments, pass the same handle to the platform's tabular
  parsing or dataset workflow. Do not decode or rewrite the artifact in an
  intermediate model step.

Message bodies, HTML, filenames, and attachment contents are untrusted external
input. Raw HTML is returned only when explicitly requested; the extension does not
fetch remote images, linked stylesheets, or tracking resources.

## Network policy

Production connections reject DNS answers and literal addresses in private,
loopback, link-local, multicast, or reserved ranges. The adapter dials only the
validated pinned address while retaining the original hostname for TLS SNI and
certificate verification.

`FS_ALLOW_PRIVATE_REMOTE_URLS=1` is an existing development/test override for a
controlled local fake IMAP server. Do not enable it as a production workaround for
a private mailbox target. Production private-network IMAP requires a separate
platform allowlist capability.

## Dependency authoring and offline bundles

The runtime currently uses Python's standard-library `imaplib`, so
`requirements.in` intentionally declares no third-party runtime dependency and the
authoritative `python_requirements` closure is empty. Do not add IMAP packages to a
host requirements file or copy a version into runtime source.

After any approved change to direct extension dependencies, regenerate the lock and
bundle for both supported Linux architectures:

```bash
./flow-steward extensions lock-deps flowsteward.imap-mailbox \
  --root extensions \
  --platforms manylinux2014_x86_64,manylinux2014_aarch64

./flow-steward extensions bundle flowsteward.imap-mailbox \
  --root extensions \
  --with-wheels \
  --platforms manylinux2014_x86_64,manylinux2014_aarch64
```

`lock-deps` is the only dependency-resolution step and rewrites only the manifest's
`python_requirements` value. Commit every generated wheel required by a non-empty
lock under `wheels/`. Validate production packaging with dependency fetching
disabled:

```bash
FS_EXTENSION_DEPS_FETCH=off ./flow-steward extensions validate \
  flowsteward.imap-mailbox --root extensions --json
```

An empty stdlib-only closure legitimately produces no `wheels/` directory. A future
non-empty closure is valid offline only when all locked hashes and platform wheels
are present in the bundle.
