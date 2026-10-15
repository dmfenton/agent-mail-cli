from dataclasses import replace
from datetime import timedelta
from email import policy
from email.parser import BytesParser

import pytest
from botocore.exceptions import ClientError
from conftest import NOW

from agent_mail_cli.mail import MailError
from agent_mail_cli.replies import prepare, send, status


def test_prepare_readonly_then_send_and_duplicate(inbox):
    plan = prepare(inbox, "inbox/one", "A general welcome.")
    assert not inbox.s3.writes and not inbox.ses.calls
    result = send(inbox, "inbox/one", plan["body"], plan["confirm"], "approval-1")
    assert result["status"] == "provider_accepted"
    assert send(inbox, "inbox/one", plan["body"], plan["confirm"], "approval-1") == result
    assert len(inbox.ses.calls) == 1
    sent = BytesParser(policy=policy.default).parsebytes(inbox.ses.calls[0]["RawMessage"]["Data"])
    assert sent["In-Reply-To"] == "<one@example.com>"
    assert inbox.ses.calls[0]["Destinations"] == ["peer@example.com"]
    state = b"".join(w["Body"] for w in inbox.s3.writes)
    for private in (b"A general welcome", b"peer@example.com", b"An introduction", b"Hello"):
        assert private not in state


def test_review_hash_changed_body_rejected(inbox):
    plan = prepare(inbox, "inbox/one", "One")
    with pytest.raises(MailError, match="review_hash_mismatch"):
        send(inbox, "inbox/one", "Two", plan["confirm"], "approval-1")
    assert not inbox.ses.calls and not inbox.s3.writes


def test_timeout_retains_claim_without_resend(inbox):
    plan = prepare(inbox, "inbox/one", "Welcome")
    inbox.ses.fail = True
    result = send(inbox, "inbox/one", "Welcome", plan["confirm"], "approval-1")
    assert result["status"] == "uncertain"
    assert (
        send(inbox, "inbox/one", "Welcome", plan["confirm"], "approval-1")["status"] == "uncertain"
    )
    assert len(inbox.ses.calls) == 1
    # Status remains available after source-body expiry, without reading it.
    inbox.s3.modified["inbox/one"] = NOW - timedelta(days=8)
    assert status(inbox, plan["receipt"], "approval-1")["status"] == "uncertain"


def test_receipt_persistence_failure_does_not_resend(inbox):
    plan = prepare(inbox, "inbox/one", "Welcome")
    inbox.s3.fail_result = True
    assert (
        send(inbox, "inbox/one", "Welcome", plan["confirm"], "approval-1")["status"] == "uncertain"
    )
    assert (
        send(inbox, "inbox/one", "Welcome", plan["confirm"], "approval-1")["status"] == "uncertain"
    )
    assert len(inbox.ses.calls) == 1


def test_changed_reply_cannot_reuse_approval(inbox):
    plan = prepare(inbox, "inbox/one", "One")
    send(inbox, "inbox/one", "One", plan["confirm"], "approval-1")
    second = prepare(inbox, "inbox/one", "Two")
    with pytest.raises(MailError, match="approval_already_bound"):
        send(inbox, "inbox/one", "Two", second["confirm"], "approval-1")


def test_conditional_race_does_not_send(inbox):
    plan = prepare(inbox, "inbox/one", "Welcome")

    def conflict(**kw):
        raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")

    inbox.s3.put_object = conflict
    with pytest.raises(MailError, match="send_claim_conflict"):
        send(inbox, "inbox/one", "Welcome", plan["confirm"], "approval-1")
    assert not inbox.ses.calls


def test_no_auth_optin_no_send(inbox):
    inbox.settings = replace(inbox.settings, trust_ses_headers=False)
    with pytest.raises(MailError, match="authentication_not_trusted"):
        prepare(inbox, "inbox/one", "Welcome")
    assert not inbox.s3.writes


def test_missing_provider_receipt_is_uncertain(inbox):
    inbox.ses.response = {}
    plan = prepare(inbox, "inbox/one", "Welcome")
    assert (
        send(inbox, "inbox/one", "Welcome", plan["confirm"], "approval-1")["status"] == "uncertain"
    )
