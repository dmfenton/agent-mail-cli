"""Bounded MIME parsing and deliberately conservative reply targets."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses


class MailError(ValueError):
    """An actionable error code safe to expose without message content."""


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def address(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(
        r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", value
    ):
        raise MailError("invalid_address")
    return value


def addresses(values: list[str]) -> tuple[str, ...]:
    parsed = getaddresses([value for value in values if value])
    if any(not mailbox for _, mailbox in parsed):
        raise MailError("invalid_address_header")
    return tuple(dict.fromkeys(address(mailbox) for _, mailbox in parsed))


def header(message: EmailMessage, name: str) -> str:
    values = message.get_all(name, [])
    if len(values) > 1:
        raise MailError("duplicate_header")
    value = str(values[0]) if values else ""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise MailError("unsafe_header")
    return value


def message_ids(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    ids = re.findall(r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~@:-]{1,250}>", value)
    if len(ids) > 20 or re.sub(r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~@:-]{1,250}>", "", value).strip():
        raise MailError("invalid_message_ids")
    return tuple(dict.fromkeys(ids))


@dataclass(frozen=True)
class Mail:
    sender: str
    recipients: tuple[str, ...]
    reply_to: tuple[str, ...]
    subject: str
    message_id: str | None
    references: tuple[str, ...]
    body: str
    authentication: dict[str, object]
    content_hash: str

    @property
    def thread(self) -> str:
        # This is only a display grouping hint; it never grants authority.
        return digest(
            self.references[0] if self.references else self.message_id or self.content_hash
        )

    def reply_target(self, mailbox: str, peers: tuple[str, ...]) -> str:
        if self.sender not in peers or self.sender == mailbox:
            raise MailError("sender_not_registered")
        if mailbox not in self.recipients or set(self.recipients) - {mailbox, self.sender}:
            raise MailError("unexpected_thread_participant")
        if self.reply_to and self.reply_to != (self.sender,):
            raise MailError("reply_to_mismatch")
        if self.authentication.get("trusted") is not True:
            raise MailError("authentication_not_trusted")
        return self.sender


def parse(raw: bytes, *, trust_ses_headers: bool = False, max_bytes: int = 1_048_576) -> Mail:
    if not raw or len(raw) > max_bytes:
        raise MailError("message_size_limit")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except (RecursionError, ValueError) as error:
        raise MailError("malformed_mime") from error
    if message.defects:
        raise MailError("malformed_mime")
    senders = addresses([header(message, "From")])
    if len(senders) != 1:
        raise MailError("sender_count")
    subject = header(message, "Subject")
    recipients = addresses([header(message, "To"), header(message, "Cc")])
    reply_to = addresses([header(message, "Reply-To")]) if message.get("Reply-To") else ()
    own_ids = message_ids(header(message, "Message-ID"))
    if len(own_ids) > 1:
        raise MailError("message_id_count")
    refs = message_ids(header(message, "References")) or message_ids(header(message, "In-Reply-To"))
    parts: list[str] = []
    for count, part in enumerate(message.walk(), 1):
        if count > 100 or part.defects:
            raise MailError("mime_complexity_or_defect")
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() == "text/plain":
            try:
                content = part.get_content()
            except (LookupError, UnicodeError, ValueError) as error:
                raise MailError("invalid_text_encoding") from error
            if isinstance(content, str):
                parts.append(content)
    auth_values = message.get_all("Authentication-Results", [])
    trusted = False
    reported: dict[str, str] = {}
    if len(auth_values) == 1:
        value = str(auth_values[0])
        if value.partition(";")[0].strip().lower() == "amazonses.com":
            for mechanism in ("spf", "dkim", "dmarc"):
                results = re.findall(rf"(?:^|[;\s]){mechanism}=([a-z_]+)\b", value, re.I)
                if results:
                    reported[mechanism] = (
                        "pass" if all(r.lower() == "pass" for r in results) else "fail"
                    )
            # Opt-in asserts a deployment boundary, never trust inferred from email text.
            trusted = (
                trust_ses_headers
                and reported.get("dmarc") == "pass"
                and reported.get("dkim") == "pass"
                and len(message.get_all("X-SES-RECEIPT", [])) == 1
                and all(
                    message.get_all(f"X-SES-{kind}-Verdict", []) == ["PASS"]
                    for kind in ("Spam", "Virus")
                )
            )
    return Mail(
        sender=senders[0],
        recipients=recipients,
        reply_to=reply_to,
        subject=subject,
        message_id=own_ids[0] if own_ids else None,
        references=refs,
        body="\n\n".join(parts),
        content_hash=digest(raw),
        authentication={
            "trusted": trusted,
            "basis": "operator_asserted_ses_boundary" if trusted else "untrusted_headers",
            "reported": reported,
        },
    )
