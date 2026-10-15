"""A review hash binds the reply; conditional S3 claims prevent blind resends."""

from __future__ import annotations

import json
from email import policy
from email.message import EmailMessage

from botocore.exceptions import ClientError

from .backend import Inbox
from .mail import MailError, digest


def prepare(inbox: Inbox, key: str, body: str) -> dict:
    if not body.strip() or len(body.encode()) > inbox.settings.max_bytes:
        raise MailError("reply_body_size")
    mail, receipt = inbox.read(key)
    target = mail.reply_target(inbox.settings.sender, inbox.peers())
    refs = list(dict.fromkeys((*mail.references, *((mail.message_id,) if mail.message_id else ()))))
    if len(refs) > 20:
        refs = [refs[0], *refs[-19:]]
    payload = {
        "version": 1,
        "receipt": receipt,
        "from": inbox.settings.sender,
        "to": target,
        "subject": mail.subject
        if mail.subject.lower().startswith("re:")
        else f"Re: {mail.subject}",
        "in_reply_to": mail.message_id,
        "references": refs,
        "body": body,
    }
    return {**payload, "confirm": digest(json.dumps(payload, sort_keys=True, ensure_ascii=True))}


def status(inbox: Inbox, receipt: str, approval: str) -> dict:
    intent_key = inbox.state_key("replies", receipt + "\0" + approval)
    intent = inbox.state(intent_key)
    if intent is None:
        return {"status": "not_started"}
    result = inbox.state(inbox.state_key("results", receipt + "\0" + approval))
    return result or {
        "status": "uncertain",
        "reply_hash": intent["reply_hash"],
        "reason": "send_claim_exists_without_result",
    }


def send(inbox: Inbox, key: str, body: str, confirm: str, approval: str) -> dict:
    if not approval.strip() or len(approval) > 256:
        raise MailError("approval_reference_required")
    plan = prepare(inbox, key, body)
    if confirm != plan["confirm"]:
        raise MailError("review_hash_mismatch")
    identity = plan["receipt"] + "\0" + approval
    intent_key = inbox.state_key("replies", identity)
    previous = inbox.state(intent_key)
    if previous is not None:
        if previous["reply_hash"] != confirm:
            raise MailError("approval_already_bound_to_different_reply")
        return status(inbox, plan["receipt"], approval)
    if inbox.ses is None:
        raise MailError("sender_not_configured")
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"], message["Subject"] = plan["from"], plan["to"], plan["subject"]
    if plan["in_reply_to"]:
        message["In-Reply-To"] = plan["in_reply_to"]
    if plan["references"]:
        message["References"] = " ".join(plan["references"])
    message.set_content(body)
    raw = message.as_bytes()
    try:
        # Never release this claim after a provider call might have begun.
        inbox.create_state(
            intent_key,
            {
                "status": "send_claimed",
                "reply_hash": confirm,
                "started_at": inbox.now().isoformat(),
            },
        )
    except ClientError as error:
        if error.response["Error"]["Code"] in {
            "PreconditionFailed",
            "ConditionalRequestConflict",
            "412",
            "409",
        }:
            raise MailError("send_claim_conflict_read_status") from error
        raise
    try:
        result = inbox.ses.send_raw_email(
            Source=plan["from"], Destinations=[plan["to"]], RawMessage={"Data": raw}
        )
        provider_id = result.get("MessageId")
        if not isinstance(provider_id, str) or not provider_id:
            raise MailError("missing_provider_receipt")
    except Exception:  # Provider may have accepted even though the response was lost.
        return {
            "status": "uncertain",
            "reply_hash": confirm,
            "reason": "provider_outcome_unconfirmed",
        }
    value = {
        "status": "provider_accepted",
        "provider_message_id": provider_id,
        "reply_hash": confirm,
        "recorded_at": inbox.now().isoformat(),
    }
    try:
        inbox.create_state(inbox.state_key("results", identity), value)
    except Exception:
        return {
            "status": "uncertain",
            "reply_hash": confirm,
            "reason": "receipt_persistence_unconfirmed",
            "provider_message_id": provider_id,
        }
    return value
