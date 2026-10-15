import json

import pytest

from agent_mail_cli import cli
from agent_mail_cli.mail import MailError


def test_cli_read_json_no_writes(inbox, monkeypatch, capsys):
    monkeypatch.setattr(cli, "settings", lambda _: inbox.settings)
    monkeypatch.setattr(cli, "client", lambda *a, **kw: inbox)
    assert cli.main(["--config", "unused", "read", "inbox/one"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["external_content"] is True
    assert data["body"].strip() == "An introduction."
    assert not inbox.s3.writes


def test_file_error_uses_absolute_path(tmp_path):
    path = tmp_path / "missing.toml"
    with pytest.raises(MailError, match=str(path)):
        cli.settings(str(path))
    with pytest.raises(MailError, match=str(path)):
        cli.body(str(path), 10)


def test_config_and_body_limits(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('bucket="example"\nsender="agents@example.com"\npeers=["peer@example.com"]\n')
    assert cli.settings(str(config)).peers == ("peer@example.com",)
    body = tmp_path / "body.txt"
    body.write_text("too long")
    with pytest.raises(MailError, match="reply_body_size"):
        cli.body(str(body), 1)


def test_ses_retries_disabled(monkeypatch, inbox):
    calls = []

    class Session:
        def client(self, name, **kw):
            calls.append((name, kw))
            return inbox.ses if name == "ses" else inbox.s3

    monkeypatch.setattr(cli.boto3, "Session", lambda **kw: Session())
    cli.client(inbox.settings, sending=True)
    assert calls[0][1]["config"].retries["total_max_attempts"] == 1


def test_cli_prepare_send_status_flow(inbox, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "settings", lambda _: inbox.settings)
    monkeypatch.setattr(cli, "client", lambda *a, **kw: inbox)
    source = tmp_path / "reply.txt"
    source.write_text("A synthetic welcome.")
    common = ["--config", "unused"]
    assert cli.main(common + ["prepare", "inbox/one", "--body-file", str(source)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert not inbox.s3.writes
    command = common + [
        "send",
        "inbox/one",
        "--body-file",
        str(source),
        "--confirm",
        plan["confirm"],
        "--approval",
        "synthetic-approval",
    ]
    assert cli.main(command) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "provider_accepted"
    assert cli.main(command) == 0
    capsys.readouterr()
    assert len(inbox.ses.calls) == 1
    assert cli.main(common + ["status", plan["receipt"], "--approval", "synthetic-approval"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "provider_accepted"


def test_cli_uncertain_exit_and_no_resend(inbox, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "settings", lambda _: inbox.settings)
    monkeypatch.setattr(cli, "client", lambda *a, **kw: inbox)
    source = tmp_path / "reply.txt"
    source.write_text("A synthetic welcome.")
    plan = cli.prepare(inbox, "inbox/one", source.read_text())
    inbox.ses.fail = True
    command = [
        "--config",
        "unused",
        "send",
        "inbox/one",
        "--body-file",
        str(source),
        "--confirm",
        plan["confirm"],
        "--approval",
        "synthetic-approval",
    ]
    assert cli.main(command) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "uncertain"
    assert cli.main(command) == 2
    capsys.readouterr()
    assert len(inbox.ses.calls) == 1
