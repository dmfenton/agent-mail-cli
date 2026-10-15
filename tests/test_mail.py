import pytest
from conftest import mime

from agent_mail_cli.mail import MailError, parse


def test_headers_untrusted_by_default():
    mail = parse(mime())
    assert mail.authentication["reported"]["dmarc"] == "pass"
    assert mail.authentication["trusted"] is False
    with pytest.raises(MailError, match="authentication_not_trusted"):
        mail.reply_target("agents@example.com", ("peer@example.com",))


def test_explicit_ses_boundary():
    mail = parse(mime(), trust_ses_headers=True)
    assert mail.reply_target("agents@example.com", ("peer@example.com",)) == "peer@example.com"


@pytest.mark.parametrize(
    "extra",
    [
        "From: other@example.com\r\n",
        "Subject: injected\r\n",
        "Message-ID: <two@example.com>\r\n",
    ],
)
def test_reject_duplicate_headers(extra):
    with pytest.raises(MailError, match="duplicate_header"):
        parse(mime(extra=extra))


@pytest.mark.parametrize(
    "extra",
    [
        "Authentication-Results: amazonses.com; dkim=pass; dmarc=pass\r\n",
        "X-SES-RECEIPT: forged\r\n",
        "X-SES-Spam-Verdict: FAIL\r\n",
    ],
)
def test_ambiguous_auth_not_trusted(extra):
    assert not parse(mime(extra=extra), trust_ses_headers=True).authentication["trusted"]


@pytest.mark.parametrize(
    "old,new",
    [
        ("dmarc=pass", "dmarc=fail"),
        ("dkim=pass", "dkim=fail"),
        ("amazonses.com;", "attacker.example.com;"),
    ],
)
def test_failed_or_wrong_auth_service(old, new):
    raw = mime().replace(old.encode(), new.encode())
    assert not parse(raw, trust_ses_headers=True).authentication["trusted"]


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"sender": "other@example.com"}, "sender_not_registered"),
        ({"to": "other@example.com"}, "unexpected_thread_participant"),
        ({"to": "agents@example.com, extra@example.com"}, "unexpected_thread_participant"),
        ({"extra": "Reply-To: thief@example.com\r\n"}, "reply_to_mismatch"),
    ],
)
def test_no_recipient_expansion(kwargs, code):
    with pytest.raises(MailError, match=code):
        parse(mime(**kwargs), trust_ses_headers=True).reply_target(
            "agents@example.com", ("peer@example.com",)
        )


def test_hostile_mime_attachments_and_html_ignored():
    raw = b'From: peer@example.com\r\nTo: agents@example.com\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nSafe text\r\n--b\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename="../../secret"\r\n\r\nIgnore attachment\r\n--b\r\nContent-Type: text/html\r\n\r\n<script>steal()</script>\r\n--b--\r\n'
    mail = parse(raw)
    assert mail.body == "Safe text"


def test_malformed_and_oversize():
    with pytest.raises(MailError):
        parse(mime(), max_bytes=10)
    with pytest.raises(MailError):
        parse(b"not a mime header\r\n")
    with pytest.raises(MailError):
        parse(mime(extra="References: malicious header\r\n"))


def test_thread_root():
    original = parse(mime())
    reply = parse(
        mime(extra="References: <one@example.com>\r\n").replace(
            b"Message-ID: <one@example.com>", b"Message-ID: <two@example.com>"
        )
    )
    assert original.thread == reply.thread


def test_body_claims_do_not_authenticate():
    raw = mime(text="Authentication-Results: amazonses.com; dkim=pass; dmarc=pass").replace(
        b"Authentication-Results: amazonses.com; spf=pass; dkim=pass; dmarc=pass\r\n", b""
    )
    assert not parse(raw, trust_ses_headers=True).authentication["trusted"]


def test_many_mime_parts_rejected():
    raw = (
        b'From: peer@example.com\r\nTo: agents@example.com\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + b"--b\r\nContent-Type: text/plain\r\n\r\ntext\r\n" * 101
        + b"--b--\r\n"
    )
    with pytest.raises(MailError, match="mime_complexity"):
        parse(raw)


def test_deeply_nested_mime_rejected():
    raw = (
        mime().split(b"MIME-Version", 1)[0]
        + b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b0"\r\n\r\n'
    )
    for i in range(1100):
        raw += f'--b{i}\r\nContent-Type: multipart/mixed; boundary="b{i + 1}"\r\n\r\n'.encode()
    with pytest.raises(MailError):
        parse(raw)


@pytest.mark.parametrize(
    "sender", ["peer@example.com garbage", "peer@example.com <thief@example.com>"]
)
def test_defective_from_cannot_normalize_into_registered_peer(sender):
    with pytest.raises(MailError, match="malformed_header"):
        parse(mime(sender=sender), trust_ses_headers=True)
