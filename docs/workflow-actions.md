# Workflow actions

Create an IMAP mailbox project connection with the server host, port, SSL or STARTTLS mode, username and password, then select it for this step.

Select the connection in the workflow step. Map returned message IDs, file handles or dataset handles into later steps rather than pasting credentials or local paths.

## Inspect IMAP connection settings (`discover_settings`)

Inspect saved connection settings and return confirmed or manual provider guidance. Create an IMAP mailbox project connection with the server host, port, SSL or STARTTLS mode, username and password, then select it for this step. Example: Inspect the saved host and TLS settings while configuring a new mailbox.

Result fields: `settings`.

## Test IMAP connection (`test_connection`)

Authenticate to the selected IMAP mailbox and report supported capabilities. Create an IMAP mailbox project connection with the server host, port, SSL or STARTTLS mode, username and password, then select it for this step. Example: Check authentication and MOVE support before building mailbox triage.

Result fields: `capabilities`, `supports_idle`, `supports_move`, `supports_uidplus`, `supports_condstore`, `supports_qresync`.

## List IMAP mailboxes (`list_mailboxes`)

List the selected mailbox account folders without reading message content. Create an IMAP mailbox project connection with the server host, port, SSL or STARTTLS mode, username and password, then select it for this step. Example: Find the exact Inbox and processed folder names before searching or moving mail.

Result fields: `mailboxes`.

## Search IMAP messages (`search_messages`)

Search a mailbox with typed filters and return a bounded UID-ordered result set. Create an IMAP mailbox project connection with the server host, port, SSL or STARTTLS mode, username and password, then select it for this step. Example: Find unread invoice messages with filters {"subject":"invoice","unseen":true} in INBOX.

Inputs:

- `mailbox`: Exact mailbox name returned by List IMAP mailboxes, such as INBOX.
- `filters`: JSON object using from, to, cc, subject, text, message_id, since/before (YYYY-MM-DD), unseen/seen, flagged/unflagged, has_attachments or uid_gte/uid_lte. Do not combine opposing true flags.
- `limit`: Maximum number of results to return in this page.
- `cursor`: Leave empty for the first page. For later pages, map next_cursor returned by this search and keep the same connection, filters and page size.

Result fields: `messages`, `next_cursor`, `truncated`, `uidvalidity`.

## Read IMAP message (`get_message`)

Read one message by IMAP UID without marking it as read. Attachment size_bytes is the encoded MIME-part octet count reported by IMAP BODYSTRUCTURE; the size_bytes_basis value is bodystructure_octets. It may differ from the decoded byte count returned by Get Attachment, and reading this metadata does not fetch the attachment body. Create an IMAP mailbox project connection with the server host, port, SSL or STARTTLS mode, username and password, then select it for this step. Example: Read a search result UID to extract its subject and attachment metadata before processing an invoice.

Inputs:

- `mailbox`: Exact mailbox name returned by List IMAP mailboxes, such as INBOX.
- `uid`: Positive numeric IMAP UID returned by Search IMAP messages, from the same mailbox.
- `include_body`: Include the message body in the result for downstream text processing.
- `include_headers`: Include the message headers in the result.
- `include_attachment_metadata`: Include attachment identifiers, filenames and content types so a later download step can select a file.
- `include_raw_html`: Include the original HTML body when HTML content is needed by a later step.

Result fields: `message`.

## Download IMAP attachment (`get_attachment`)

Write one selected message attachment to an approved workflow artifact. Choose it by exactly one of attachment_id, filename or content_type; content_type accepts a wildcard such as image/*. A selector that matches nothing, or more than one attachment, fails and names what the message actually carries. Create an IMAP mailbox project connection with the server host, port, SSL or STARTTLS mode, username and password, then select it for this step. Example: Download the invoice.pdf attachment using its attachment_id from message metadata and pass the artifact to document extraction.

Inputs:

- `mailbox`: Exact mailbox name returned by List IMAP mailboxes, such as INBOX.
- `uid`: Positive numeric IMAP UID returned by Search IMAP messages, from the same mailbox.
- `attachment_id`: Attachment identifier from Read IMAP message. At runtime, supply exactly one of attachment_id, filename or content_type.

Result fields: `attachment_artifact_handle`, `attachment_metadata`, `attachment_presentation`.

## Set IMAP read and starred flags (`set_message_flags`)

Explicitly update Seen and Flagged state for selected message UIDs. Create an IMAP mailbox project connection with the server host, port, SSL or STARTTLS mode, username and password, then select it for this step. Example: Set seen=true after an invoice has been processed; leave flagged empty to preserve its state.

Inputs:

- `mailbox`: Exact mailbox name returned by List IMAP mailboxes, such as INBOX.
- `uids`: JSON array of positive IMAP UIDs from the selected source mailbox.
- `seen`: Set true to mark messages read or false to mark them unread. Leave empty to preserve read state; provide at least one of seen or flagged.
- `flagged`: Set true to star messages or false to remove the star. Leave empty to preserve starred state; provide at least one of seen or flagged.

Result fields: `results`, `external_effect_status`, `definitely_no_external_effect`.

## Move IMAP messages (`move_messages`)

Explicitly move selected message UIDs between IMAP mailboxes. Create an IMAP mailbox project connection with the server host, port, SSL or STARTTLS mode, username and password, then select it for this step. Example: Move processed UIDs from INBOX to an existing Processed folder after downstream work succeeds.

Inputs:

- `source_mailbox`: Existing mailbox containing the selected UIDs. UIDs are scoped to this mailbox.
- `target_mailbox`: Existing destination mailbox name returned by List IMAP mailboxes.
- `uids`: JSON array of positive IMAP UIDs from the selected source mailbox.

Result fields: `results`, `external_effect_status`, `definitely_no_external_effect`, `guidance`.

## Delete or trash IMAP messages (`delete_messages`)

Explicitly move selected message UIDs to trash or permanently delete them with confirmation. Create an IMAP mailbox project connection with the server host, port, SSL or STARTTLS mode, username and password, then select it for this step. Example: Move duplicate messages to the configured trash folder with mode=move_to_trash; permanent deletion requires confirm_permanent_delete=true.

Inputs:

- `mailbox`: Exact mailbox name returned by List IMAP mailboxes, such as INBOX.
- `uids`: JSON array of positive IMAP UIDs from the selected source mailbox.
- `mode`: move_to_trash moves messages to the connection's trash folder. permanent deletes them and also requires confirm_permanent_delete=true.
- `confirm_permanent_delete`: Enable only with mode=permanent to confirm removal of the selected messages.

Result fields: `results`, `external_effect_status`, `definitely_no_external_effect`, `guidance`.
