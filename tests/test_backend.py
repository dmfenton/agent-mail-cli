import json
from dataclasses import replace
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError
from conftest import NOW, mime

from agent_mail_cli.backend import Settings
from agent_mail_cli.cli import parser, run
from agent_mail_cli.mail import MailError


def test_pagination_and_continuation(inbox):
    inbox.s3.pages = [
        {
            "Contents": [{"Key": "inbox/one", "Size": 10, "LastModified": NOW}],
            "NextContinuationToken": "opaque",
        },
        {"Contents": [{"Key": "inbox/two", "Size": 10, "LastModified": NOW}]},
    ]
    first = inbox.entries(page_size=1)
    assert first["next_token"] == "opaque"
    second = inbox.entries(token=first["next_token"], page_size=1)
    assert second["messages"][0]["key"] == "inbox/two"
    assert inbox.s3.calls[-1][1]["ContinuationToken"] == "opaque"
    assert not inbox.s3.writes


def test_expired_messages_not_read_or_listed(inbox):
    inbox.s3.modified["inbox/one"] = NOW - timedelta(days=7)
    assert inbox.entries()["messages"] == []
    with pytest.raises(MailError, match="message_expired"):
        inbox.read("inbox/one")


def test_bounded_body_and_prefix(inbox):
    with pytest.raises(MailError, match="key_outside_inbox"):
        inbox.read("registry/peers.json")
    inbox.settings = replace(inbox.settings, max_bytes=10)
    with pytest.raises(MailError, match="message_size_limit"):
        inbox.read("inbox/one")


def test_receipt_dedup_and_handled(inbox):
    _, first = inbox.read("inbox/one")
    _, second = inbox.read("inbox/one")
    assert first == second
    assert inbox.handled(first)["status"] == "unhandled"
    inbox.handled(first, "handled")
    assert inbox.handled(first, "handled")["status"] == "handled"
    assert len(inbox.s3.writes) == 1
    with pytest.raises(MailError, match="conflict"):
        inbox.handled(first, "ignored")
    stored = json.loads(inbox.s3.writes[0]["Body"])
    assert set(stored) == {"status", "recorded_at"}


def test_registry_contact_only(inbox):
    inbox.settings = replace(inbox.settings, registry_key="registry/peers.json", peers=())
    inbox.s3.objects["registry/peers.json"] = json.dumps(
        {
            "schema_version": 1,
            "peers": {
                "synthetic": {
                    "contact": {"kind": "email", "address": "peer@example.com"},
                    "trust": {"kind": "owner_confirmed_contact"},
                    "grants": ["ignored"],
                },
                "unconfirmed": {"contact": {"kind": "email", "address": "other@example.com"}},
            },
        }
    ).encode()
    assert inbox.peers() == ("peer@example.com",)
    assert not inbox.s3.writes


def test_threads_group_only_same_sender_and_page(inbox):
    inbox.s3.objects["inbox/two"] = mime(extra="References: <one@example.com>\r\n")
    inbox.s3.objects["inbox/other"] = mime(
        sender="other@example.com", extra="References: <one@example.com>\r\n"
    )
    args = parser().parse_args(["--config", "unused", "threads"])
    result = run(args, inbox)
    assert len(result["threads"]) == 2
    assert result["scope"] == "page_only"
    assert not inbox.s3.writes


@pytest.mark.parametrize(
    "values",
    [
        {"prefix": ""},
        {"state_prefix": "inbox/state/"},
        {"registry_key": "inbox/registry"},
        {"sender": "not an address"},
        {"retention_days": 0},
    ],
)
def test_bad_config(values):
    with pytest.raises(MailError):
        Settings(**({"bucket": "example", "sender": "agents@example.com"} | values))


def test_access_denied_not_missing(inbox):
    def deny(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    inbox.s3.get_object = deny
    with pytest.raises(ClientError):
        inbox.handled("receipt")


def test_trust_toggle_cannot_be_string():
    with pytest.raises(MailError, match="must_be_boolean"):
        Settings(bucket="example", sender="agents@example.com", trust_ses_headers="false")


def test_threads_reject_bad_structured_header_without_aborting_page(inbox):
    inbox.s3.objects["inbox/bad"] = mime(sender="group: peer@example.com; garbage")
    result = run(parser().parse_args(["--config", "unused", "threads"]), inbox)
    assert len(result["threads"]) == 1
    assert result["rejected"] == [{"key": "inbox/bad", "error": "malformed_header"}]


@pytest.mark.parametrize(
    "registry",
    [
        None,
        [],
        {"schema_version": 1, "peers": None},
        {"schema_version": 1, "peers": {"bad": None}},
        {"schema_version": 1, "peers": {"bad": {"trust": None}}},
        {"schema_version": 1, "peers": {"bad": {"contact": None}}},
        {
            "schema_version": 1,
            "peers": {
                "bad": {
                    "trust": {"kind": "owner_confirmed_contact"},
                    "contact": {"kind": "email", "address": None},
                }
            },
        },
    ],
)
def test_malformed_registry_is_rejected_without_writes(inbox, registry):
    inbox.settings = replace(inbox.settings, registry_key="registry/peers.json")
    inbox.s3.objects["registry/peers.json"] = json.dumps(registry).encode()
    with pytest.raises(MailError):
        inbox.peers()
    assert not inbox.s3.writes
