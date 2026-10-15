from datetime import UTC, datetime
from io import BytesIO

import pytest
from botocore.exceptions import ClientError

from agent_mail_cli.backend import Inbox, Settings

NOW = datetime(2026, 1, 10, tzinfo=UTC)


def mime(
    *,
    sender="peer@example.com",
    to="agents@example.com",
    subject="Hello",
    extra="",
    text="An introduction.",
):
    return (
        f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nMessage-ID: <one@example.com>\r\n"
        "Authentication-Results: amazonses.com; spf=pass; dkim=pass; dmarc=pass\r\n"
        "X-SES-RECEIPT: synthetic\r\nX-SES-Spam-Verdict: PASS\r\nX-SES-Virus-Verdict: PASS\r\n"
        f"{extra}MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{text}"
    ).encode()


class S3:
    def __init__(self):
        self.objects = {"inbox/one": mime()}
        self.modified = {}
        self.calls = []
        self.writes = []
        self.pages = None
        self.fail_result = False

    def get_object(self, **kw):
        self.calls.append(("get", kw))
        key = kw["Key"]
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        raw = self.objects[key]
        return {
            "Body": BytesIO(raw),
            "ContentLength": len(raw),
            "LastModified": self.modified.get(key, NOW),
            "ETag": '"synthetic"',
        }

    def list_objects_v2(self, **kw):
        self.calls.append(("list", kw))
        if self.pages is not None:
            return self.pages[1 if "ContinuationToken" in kw else 0]
        return {
            "Contents": [
                {"Key": k, "Size": len(v), "LastModified": self.modified.get(k, NOW)}
                for k, v in self.objects.items()
                if k.startswith(kw["Prefix"])
            ]
        }

    def put_object(self, **kw):
        if self.fail_result and "/results/" in kw["Key"]:
            raise OSError("synthetic persistence failure")
        assert kw["IfNoneMatch"] == "*"
        assert kw["ServerSideEncryption"] == "AES256"
        if kw["Key"] in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.writes.append(kw)
        self.objects[kw["Key"]] = kw["Body"]
        return {"ETag": '"new"'}


class SES:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.response = {"MessageId": "synthetic-provider-id"}

    def send_raw_email(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise TimeoutError("response lost")
        return self.response


@pytest.fixture
def inbox():
    return Inbox(
        Settings(
            bucket="example-agent-inbox",
            sender="agents@example.com",
            peers=("peer@example.com",),
            trust_ses_headers=True,
        ),
        S3(),
        SES(),
        now=lambda: NOW,
    )
