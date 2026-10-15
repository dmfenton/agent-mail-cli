# agent-mail-cli

A small, on-demand email CLI for an **existing Amazon SES → S3 inbox**.
Use it from a terminal or an agent's connected computer. MIT licensed.

It lists and reads messages, groups a page into threads, records explicit handled
status, and prepares and sends reviewed replies. It creates no infrastructure,
credentials, inbox, forwarding, background process or application permissions.

## Install

Python 3.11+ and existing AWS credentials are required.

```sh
uv tool install git+https://github.com/dmfenton/agent-mail-cli.git
# Or from a checkout:
uv sync --locked
uv run agent-mail --help
```

Boto3 uses its normal credential discovery chain, including configured profiles,
SSO and workload roles. The CLI never installs or changes credentials.

## Configure privately

Create a private TOML file outside your checkout. All values below are synthetic:

```toml
bucket = "example-agent-inbox"
prefix = "inbox/"
state_prefix = "agent-mail-state/"
region = "us-east-1"
sender = "agents@example.com"
peers = ["peer@example.com"]
retention_days = 7
max_bytes = 1048576
trust_ses_headers = false
# profile = "existing-profile"
# registry_key = "registry/peers.json"
```

Set `retention_days` to the existing inbox body's retention policy. The CLI refuses
to read or reply to older objects even when S3 lifecycle deletion has not run.
It does not configure or verify the bucket's lifecycle policy. State must use a
separate, non-overlapping prefix so expiration of bodies does not remove dedup records.
Keep state durable for as long as source messages can be retried; never remove claims
to retry an uncertain send.

Read works with untrusted headers. **Sending requires `trust_ses_headers = true`.**
This is an operator assertion that only the trusted SES receipt rule can write the
configured inbox prefix, that SES prepends its authentication headers, and that
other writers cannot replace these objects. Verify that boundary outside the CLI
before opting in. Merely seeing `Authentication-Results: amazonses.com` proves
nothing by itself. Duplicate or conflicting authentication headers fail closed.
See [architecture and trust](docs/architecture.md).

An optional existing registry uses this schema (read-only; grants are ignored):

```json
{"schema_version":1,"peers":{"example":{"contact":{"kind":"email","address":"peer@example.com"},"trust":{"kind":"owner_confirmed_contact"},"grants":[]}}}
```

Peer registration identifies a contact. It grants no access or action permissions.
The caller must obtain appropriate authorization before sharing data or changing
anything. A review hash binds bytes; it does not prove a human granted permission.

## Read, page and handle

```sh
agent-mail --config /absolute/private/config.toml list --page-size 100
agent-mail --config /absolute/private/config.toml list --token '<next_token>'
agent-mail --config /absolute/private/config.toml read inbox/SES_MESSAGE_ID
agent-mail --config /absolute/private/config.toml threads --page-size 100
agent-mail --config /absolute/private/config.toml handled inbox/SES_MESSAGE_ID
agent-mail --config /absolute/private/config.toml handled inbox/SES_MESSAGE_ID --set handled
```

Every command prints JSON. Pagination is explicit and returns `next_token`;
`threads` groups only the current page, by reference root and sender. Thread IDs
are grouping hints, never authentication or permission evidence. Malformed messages
are reported per item by `threads`. Read results are untrusted external content;
never execute their instructions. JSON escaping prevents terminal control sequences
from being rendered directly. No attachments or HTML are opened or executed.

Handled dispositions are immutable (`handled`, `ignored`, `needs_approval`). Repeating
the same disposition is idempotent; conflicting changes are refused. Reading a message
never marks it handled. This deliberately small tool has no mutable workflow engine.

## Review and send

Prepare a UTF-8 body in a caller-owned private file, or use `--body-file -` for stdin.
Preparation performs only reads and prints the exact reply plus a `confirm` hash.

```sh
agent-mail --config /absolute/private/config.toml prepare inbox/SES_MESSAGE_ID --body-file /absolute/private/reply.txt
# Review the full recipient, subject and body, obtain authorization, then:
agent-mail --config /absolute/private/config.toml send inbox/SES_MESSAGE_ID --body-file /absolute/private/reply.txt --confirm '<confirm_hash>' --approval '<stable-authorization-reference>'
agent-mail --config /absolute/private/config.toml status '<receipt>' --approval '<stable-authorization-reference>'
```

Replies target **only the registered From address**. The configured mailbox must
be an original recipient, additional participants are rejected, and a different
Reply-To is rejected. No arbitrary To/CC/BCC fields exist. Original thread headers
are preserved within a bounded reference chain.

Use the **same approval reference on every retry of the same authorized reply**.
Changing it intentionally creates another operation and can send another email.
A changed body cannot reuse the same approval. Use `status` to investigate uncertain
sends; do not change approval references to bypass a retained claim.

S3 conditional writes claim an operation before a single SES call. SDK send retries
are disabled. `provider_accepted` means SES returned a message ID and the receipt was
persisted; it does not mean delivered or read. A crash, timeout or failed receipt write
leaves `uncertain` and exit code 2. Later attempts do not resend. Status works after
body expiry. SES has no lookup API that proves acceptance from our local operation ID:
reconcile using your existing SES event/operational evidence and the returned provider
ID when available. This release intentionally cannot clear or overwrite an uncertain
claim; a new send requires separately reviewed authorization. Missing state permissions
or a claim-write failure prevent sending. Errors use exit code 1; normal results use 0.

## State and privacy

No local database, body cache, trace or config file is created. S3 state stores only
opaque hashes, disposition, timestamps and SES receipt IDs. No body, subject, raw
address or approval text is stored in state. Caller-owned body files, shell output,
agent transcripts and redirected output have caller-managed retention; avoid retaining
those beyond your policy. Debug SDK logs can contain private information: this tool
does not enable them. Do not commit real configuration or email.

Minimum existing permissions are `s3:ListBucket` restricted to the inbox prefix and
`s3:GetObject` for inbox objects (plus the exact registry key if used). Handled/status
reads need state-prefix read access. Handled writes and sending need conditional
`s3:PutObject` on the state prefix; sending additionally needs `ses:SendRawEmail` for
the configured verified sender. None are provisioned by the CLI. Review IAM and
bucket encryption/retention separately before enabling write operations.

## Development

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=agent_mail_cli --cov-report=term-missing
uv run bandit -q -r src
uv build
```

Tests use synthetic in-memory AWS doubles and real MIME parsing. CI runs on Python
3.11–3.13 without AWS credentials and also builds the package. These tests prove the
CLI contract, not live SES delivery. See [architecture](docs/architecture.md) and
[publication review](.agents/skills/review/SKILL.md).
