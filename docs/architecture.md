# Architecture and invariants

One Python CLI uses boto3's S3/SES clients and Python's standard MIME parser.
AWS owns credential discovery, inbox delivery, raw body retention and provider
receipts. The CLI owns bounded parsing, explicit pagination, deterministic target
checks, reply review binding, conditional dedup claims and honest send outcomes.
The caller owns contact enrollment, permission decisions and any exported content.

```text
existing SES receipt rule -> existing S3 inbox/ -> in-memory MIME -> JSON view
                                                   |
existing peer config/registry -> validated one-peer reply -> reviewed hash
                                                   |
existing S3 state prefix <- conditional intent <- explicit send command
                                                   |
                                      one SES send (no SDK retries)
                                                   |
                             accepted receipt / retained uncertain intent
```

## Read flow

List one S3 page and return its continuation token. Filter setup notifications and
expired bodies. Read only keys below the configured inbox prefix. Bound bytes before
parsing, always close streams, reject ambiguous routing headers, ignore attachments
and HTML, and cap MIME parts. A message receipt hashes bucket, key, version, ETag
and raw-content hash. Repeated reads of the same object produce the same receipt.
Changing content or object version produces another receipt; this tool is not a
semantic spam or duplicate-message detector. Subject and sender dates are not identity.

Thread roots use bounded References/In-Reply-To/Message-ID headers. The CLI groups
by sender as well as root and only within the returned page. Spoofed references
cannot add recipients, authorize actions or merge another sender into a reply target.

## Authentication boundary

Headers remain untrusted by default. Opting into SES header trust is a deployment
assertion, not a feature that authenticates arbitrary uploaded mail. The operator
must establish bucket write restrictions and SES's header provenance. With that
boundary asserted, the parser requires one Amazon SES Authentication-Results header,
passing DMARC and DKIM, one SES receipt, and passing spam/virus results. Duplicate
claims fail closed. It does not validate DKIM cryptography locally or treat quoted
headers in a body as metadata. A compromised AWS writer invalidates the assertion.

Registration and authentication identify a peer; neither grants permission to read
private app information or act on behalf of an owner. Approval is outside the CLI.
`--confirm` protects reviewed reply content from accidental changes; `--approval`
is a stable reference provided by an already-authorized caller, not a security token.

## State and delivery

The inbox body is never persisted locally or copied into state. Metadata lives in
three namespaces of the same configured S3 state prefix: handled, replies and results.
Immutable create-only writes use `If-None-Match: *` and AES256 encryption. Races fail
closed. There is no second database, polling process, service or scheduled job.

Handled state stores a disposition and time. Reply intent identity combines the
source receipt and stable approval reference. An intent stores only a hash of the
reviewed payload and a time. A completed result stores provider ID, payload hash,
status and time. Raw addresses, source text, reply text and approval references are
not retained. The caller must protect metadata and retain send claims beyond the
retry horizon; deleting them permits another send. No commands delete inbox or state.

Serialize and validate the reply before creating its intent. An intent is created
before SES can begin. Never release it after the provider boundary. A concurrent
claim failure sends nothing. SES retries are disabled at SDK level. Persist a result
only after SES returns a nonempty message ID. Lost responses and failed result writes
are uncertain, including a crash between intent and SES. This sacrifices some delivery
for duplicate protection. There is no claim timeout or automatic uncertain retry.

No automatic acceptance reconciliation is possible without an existing provider
observation channel. `status` exposes the retained fact and any durably recorded
receipt. Operators must consult existing evidence; they cannot reset state via this
CLI. Provider acceptance never claims recipient delivery or reading.

## References

- [Boto3 credential chain](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html)
- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [SES raw sending](https://docs.aws.amazon.com/ses/latest/APIReference/API_SendRawEmail.html)
- [SES receiving headers](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-action-lambda-event.html)

## Scope

No IAM changes, credentials, recipient enrollment, standing grants, application
routing, mailbox forwarding or always-on access. Local invocation through a connected
computer remains dependent on that computer and its existing authorized environment.
