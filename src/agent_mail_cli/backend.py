"""Existing AWS resources only. Raw bodies are never cached or written."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

from .mail import Mail, MailError, address, digest, parse


@dataclass(frozen=True)
class Settings:
    bucket: str
    sender: str
    prefix: str = "inbox/"
    state_prefix: str = "agent-mail-state/"
    region: str = "us-east-1"
    peers: tuple[str, ...] = ()
    registry_key: str | None = None
    retention_days: int = 7
    max_bytes: int = 1_048_576
    trust_ses_headers: bool = False
    profile: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trust_ses_headers, bool):
            raise MailError("trust_ses_headers_must_be_boolean")
        address(self.sender)
        for peer in self.peers:
            address(peer)
        if (
            not self.bucket
            or not self.prefix
            or not self.state_prefix
            or not self.prefix.endswith("/")
            or not self.state_prefix.endswith("/")
        ):
            raise MailError("invalid_bucket_or_prefix")
        if self.prefix.startswith(self.state_prefix) or self.state_prefix.startswith(self.prefix):
            raise MailError("overlapping_state_prefix")
        if self.registry_key and (
            self.registry_key.startswith(self.prefix)
            or self.registry_key.startswith(self.state_prefix)
        ):
            raise MailError("overlapping_registry_key")
        if self.retention_days < 1 or not 1 <= self.max_bytes <= 10_485_760:
            raise MailError("invalid_limits")
        if self.sender != self.sender.lower() or any(p != p.lower() for p in self.peers):
            raise MailError("addresses_must_be_lowercase")


class Inbox:
    def __init__(self, settings: Settings, s3: Any, ses: Any = None, *, now: Any = None):
        self.settings, self.s3, self.ses = settings, s3, ses
        self.now = now or (lambda: datetime.now(UTC))

    def fresh(self, modified: datetime) -> bool:
        return modified > self.now() - timedelta(days=self.settings.retention_days)

    def entries(self, *, token: str | None = None, page_size: int = 100) -> dict:
        if not 1 <= page_size <= 1000:
            raise MailError("invalid_page_size")
        request = dict(Bucket=self.settings.bucket, Prefix=self.settings.prefix, MaxKeys=page_size)
        if token:
            request["ContinuationToken"] = token
        result = self.s3.list_objects_v2(**request)
        entries = [
            {"key": item["Key"], "size": item["Size"], "modified": item["LastModified"].isoformat()}
            for item in result.get("Contents", [])
            if self.fresh(item["LastModified"])
            and item["Key"] != self.settings.prefix + "AMAZON_SES_SETUP_NOTIFICATION"
        ]
        return {"messages": entries, "next_token": result.get("NextContinuationToken")}

    def read(self, key: str) -> tuple[Mail, str]:
        if (
            not key.startswith(self.settings.prefix)
            or len(key) <= len(self.settings.prefix)
            or any(ord(c) < 32 for c in key)
        ):
            raise MailError("key_outside_inbox")
        result = self.s3.get_object(Bucket=self.settings.bucket, Key=key)
        stream = result["Body"]
        try:
            if not self.fresh(result["LastModified"]):
                raise MailError("message_expired")
            if result["ContentLength"] > self.settings.max_bytes:
                raise MailError("message_size_limit")
            raw = stream.read(self.settings.max_bytes + 1)
        finally:
            stream.close()
        mail = parse(
            raw,
            trust_ses_headers=self.settings.trust_ses_headers,
            max_bytes=self.settings.max_bytes,
        )
        receipt = digest(
            json.dumps(
                [
                    self.settings.bucket,
                    key,
                    result.get("VersionId"),
                    result["ETag"],
                    mail.content_hash,
                ]
            )
        )
        return mail, receipt

    def peers(self) -> tuple[str, ...]:
        peers = set(self.settings.peers)
        if self.settings.registry_key:
            result = self.s3.get_object(Bucket=self.settings.bucket, Key=self.settings.registry_key)
            stream = result["Body"]
            try:
                data = stream.read(65_537)
            finally:
                stream.close()
            if len(data) > 65_536:
                raise MailError("registry_size_limit")
            registry = json.loads(data)
            if not isinstance(registry, dict) or registry.get("schema_version") != 1:
                raise MailError("unsupported_registry")
            entries = registry.get("peers")
            if not isinstance(entries, dict):
                raise MailError("invalid_registry")
            for peer in entries.values():
                if not isinstance(peer, dict):
                    raise MailError("invalid_registry")
                trust, contact = peer.get("trust", {}), peer.get("contact", {})
                if not isinstance(trust, dict) or not isinstance(contact, dict):
                    raise MailError("invalid_registry")
                if (
                    trust.get("kind") == "owner_confirmed_contact"
                    and contact.get("kind") == "email"
                ):
                    mailbox = contact.get("address")
                    if not isinstance(mailbox, str):
                        raise MailError("invalid_registry")
                    peers.add(address(mailbox))
        return tuple(sorted(peers))

    def state_key(self, kind: str, identity: str) -> str:
        return f"{self.settings.state_prefix}{kind}/{digest(identity)}.json"

    def state(self, key: str) -> dict | None:
        try:
            result = self.s3.get_object(Bucket=self.settings.bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return None
            raise
        stream = result["Body"]
        try:
            raw = stream.read(16_385)
        finally:
            stream.close()
        if len(raw) > 16_384:
            raise MailError("state_size_limit")
        return json.loads(raw)

    def create_state(self, key: str, value: dict) -> None:
        self.s3.put_object(
            Bucket=self.settings.bucket,
            Key=key,
            Body=json.dumps(value).encode(),
            ContentType="application/json",
            ServerSideEncryption="AES256",
            IfNoneMatch="*",
        )

    def handled(self, receipt: str, disposition: str | None = None) -> dict:
        key = self.state_key("handled", receipt)
        previous = self.state(key)
        if disposition is None:
            return previous or {"status": "unhandled"}
        if disposition not in {"handled", "ignored", "needs_approval"}:
            raise MailError("invalid_disposition")
        value = {"status": disposition, "recorded_at": self.now().isoformat()}
        if previous:
            if previous["status"] != disposition:
                raise MailError("handled_disposition_conflict")
            return previous
        try:
            self.create_state(key, value)
        except ClientError as error:
            if error.response["Error"]["Code"] in {
                "PreconditionFailed",
                "ConditionalRequestConflict",
                "412",
                "409",
            }:
                raise MailError("state_conflict_read_status") from error
            raise
        return value
