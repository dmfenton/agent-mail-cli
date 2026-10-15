"""JSON output is escaped; no implicit writes, logging, polling or credential setup."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import asdict
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import __version__
from .backend import Inbox, Settings
from .mail import MailError, digest
from .replies import prepare, send, status


def settings(path: str) -> Settings:
    source = Path(path).expanduser().resolve()
    try:
        values = tomllib.loads(source.read_text())
        if "peers" in values:
            values["peers"] = tuple(values["peers"])
        return Settings(**values)
    except (OSError, TypeError, ValueError) as error:
        raise MailError(f"invalid_config:{source}") from error


def client(settings: Settings, sending: bool = False) -> Inbox:
    session = boto3.Session(profile_name=settings.profile, region_name=settings.region)
    # Crucial: SES send_raw_email has no idempotency token. SDK transport retries
    # could duplicate a send even when our application never explicitly retries.
    ses = (
        session.client("ses", config=Config(retries={"mode": "standard", "total_max_attempts": 1}))
        if sending
        else None
    )
    return Inbox(settings, session.client("s3"), ses)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="On-demand SES/S3 email CLI. No credentials are created."
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument(
        "--config", required=True, help="Absolute or relative path to a private TOML configuration"
    )
    sub = root.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="List one page of unexpired inbox objects")
    listing.add_argument("--token")
    listing.add_argument("--page-size", type=int, default=100)
    read = sub.add_parser("read", help="Read one message; external text is untrusted")
    read.add_argument("key")
    threads = sub.add_parser(
        "threads", help="Group a page by references AND sender; no subject grouping"
    )
    threads.add_argument("--token")
    threads.add_argument("--page-size", type=int, default=100)
    handled = sub.add_parser(
        "handled", help="Read status, or explicitly record an immutable disposition"
    )
    handled.add_argument("key")
    handled.add_argument("--set", choices=["handled", "ignored", "needs_approval"])
    for command in ("prepare", "send"):
        reply = sub.add_parser(
            command,
            help="Review a reply" if command == "prepare" else "Explicitly send the reviewed reply",
        )
        reply.add_argument("key")
        reply.add_argument(
            "--body-file", required=True, help="Caller-owned UTF-8 file; use '-' for stdin"
        )
        if command == "send":
            reply.add_argument("--confirm", required=True, help="Exact hash from prepare")
            reply.add_argument(
                "--approval",
                required=True,
                help="Stable reference to the caller's actual authorization",
            )
    delivery = sub.add_parser(
        "status", help="Read an existing send outcome without rereading expired bodies"
    )
    delivery.add_argument("receipt")
    delivery.add_argument("--approval", required=True)
    return root


def body(path: str, maximum: int) -> str:
    if path == "-":
        raw = sys.stdin.buffer.read(maximum + 1)
    else:
        source = Path(path).expanduser().resolve()
        try:
            with source.open("rb") as stream:
                raw = stream.read(maximum + 1)
        except OSError as error:
            raise MailError(f"body_file_unreadable:{source}") from error
    if len(raw) > maximum:
        raise MailError("reply_body_size")
    try:
        return raw.decode("utf-8")
    except UnicodeError as error:
        raise MailError("body_file_not_utf8") from error


def run(args: argparse.Namespace, inbox: Inbox) -> object:
    if args.command == "list":
        return inbox.entries(token=args.token, page_size=args.page_size)
    if args.command == "status":
        return status(inbox, args.receipt, args.approval)
    if args.command == "threads":
        page = inbox.entries(token=args.token, page_size=args.page_size)
        groups: dict[str, dict] = {}
        rejected = []
        for entry in page["messages"]:
            try:
                mail, receipt = inbox.read(entry["key"])
            except MailError as error:
                rejected.append({"key": entry["key"], "error": str(error)})
                continue
            group = digest(mail.sender + "\0" + mail.thread)
            groups.setdefault(group, {"id": group, "sender": mail.sender, "messages": []})[
                "messages"
            ].append(
                {
                    "key": entry["key"],
                    "receipt": receipt,
                    "subject": mail.subject,
                    "authentication": mail.authentication,
                }
            )
        return {
            "threads": list(groups.values()),
            "rejected": rejected,
            "next_token": page["next_token"],
            "scope": "page_only",
        }
    if args.command in {"prepare", "send"}:
        text = body(args.body_file, inbox.settings.max_bytes)
        return (
            prepare(inbox, args.key, text)
            if args.command == "prepare"
            else send(inbox, args.key, text, args.confirm, args.approval)
        )
    mail, receipt = inbox.read(args.key)
    if args.command == "handled":
        return {"receipt": receipt, **inbox.handled(receipt, args.set)}
    return {"receipt": receipt, "external_content": True, **asdict(mail)}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = settings(args.config)
        result = run(args, client(config, sending=args.command == "send"))
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 2 if isinstance(result, dict) and result.get("status") == "uncertain" else 0
    except MailError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
    except (BotoCoreError, ClientError):
        print(
            json.dumps(
                {
                    "error": "aws_request_failed",
                    "hint": "Check network, existing credentials and resource permissions privately.",
                }
            ),
            file=sys.stderr,
        )
    except (ValueError, KeyError, TypeError):
        print(json.dumps({"error": "invalid_remote_data"}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
