# Generic IMAP Mailbox

Generic IMAP Mailbox brings email from Fastmail, Yahoo, Zoho, shared hosting and other compatible IMAP servers into Flow Steward workflows. List folders, search by sender, subject, date or read state, read selected messages and download their attachments for later steps. Reading a message leaves its read state unchanged, so your workflow can decide when processing is complete.

Build invoice intake, document collection or mailbox triage workflows by passing attachments to document extraction or tabular processing. Once downstream work succeeds, update read or starred flags, move messages into a processed folder or send them to trash. Permanent deletion is available as a separate, explicitly confirmed operation where the server supports it.

Create a project connection using the provider's IMAP host, port, SSL or STARTTLS mode, username and password or app password. Provider presets help with initial settings, and a connection test confirms access. This connector handles incoming mail; outbound messages use a separate email connection. Use dedicated connectors for Gmail and Microsoft mailboxes. Attachments support approved file types up to 20 MiB, and production connections require publicly reachable server addresses.

Use the official Gmail or Microsoft connector for Gmail, Google Workspace,
Outlook.com, or Microsoft 365. This extension rejects their known public mailbox
domains and hosts instead of attempting a password or OAuth fallback.

Logo: `icon.png` (the Flow Steward wave mark), next to `extension.yaml`.

## Requirements

This release (0.2.3) speaks the host contract `extension_host_v2` with its final
execution scope (`group_id`, `for_each_id`, `step_id`, `step_execution_id`)
and is built on `flowsteward-extension-sdk` 0.4.0. A request with another
contract, a missing scope key, or both a Group and a For each as its owner is
refused with `invalid_host_envelope` before the mailbox is opened or an artifact
is written. Install it on a Flow Steward Core that speaks this contract; an older
Core does not run it.

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

### Attachment size metadata

`Get Message` reports each attachment's `size_bytes` from the MIME part size in
IMAP `BODYSTRUCTURE`. The response includes `size_bytes_basis: "bodystructure_octets"`
to make clear that this is the server-reported encoded MIME-part octet count. It is
metadata only; `Get Message` does not fetch an attachment body just to calculate it.

`Get Attachment` reports `attachment_metadata.size_bytes` as the number of bytes in
the selected decoded payload that it writes. The two values can differ for
base64- or quoted-printable-encoded attachments. Use the `Get Attachment` value
when you need the decoded payload size.

### Attachment presentation

The installed `get_attachment` output policy opts into the host's presentation
claim using `attachment_presentation`. After the SDK confirms the write, the
terminal result identifies the prepared output and supplies the validated source
filename, MIME type, decoded size and checksum. The host independently checks
this claim against its prepared output and finalized bytes before accepting it.
Upload grants remain `application/octet-stream`; output-ready notifications
remain ID-only. Business `attachment_metadata` is separate from host authority.

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
