"""usali invite <email>: creates a pending invite and sends the link via the
Notifier; the CLI prints the invite link."""

from typer.testing import CliRunner

import usali.cli as cli
from tests.notifiers import CapturingNotifier
from usali.db import make_engine, make_session_factory
from tests.orgwall import app_role_url
from sqlalchemy import select
from usali.models import Invite


def test_invite_command_creates_row_and_sends_link(db_url, db_session, monkeypatch):
    # db_url sets USALI_DB_URL to the testcontainer (session-scoped;
    # get_settings() is uncached, so the CLI reads it fresh on every call).
    #
    # db_session is requested for its TRUNCATE, not its session. The container
    # is session-scoped and every test here commits real rows, so without it
    # this test inherits whatever the previous module left behind -- and the
    # scalar_one() below then fails on any earlier invite for the same address.
    # It passed only because nothing had happened to write a second one yet.
    captured = CapturingNotifier()
    monkeypatch.setattr(cli, "_notifier", lambda: captured, raising=False)
    result = CliRunner().invoke(cli.app, ["invite", "owner@example.test"])
    assert result.exit_code == 0, result.output
    assert "signup?token=" in result.output
    assert len(captured.emails) == 1 and captured.emails[0]["to"] == "owner@example.test"

    factory = make_session_factory(make_engine(app_role_url(db_url)))
    with factory() as s:
        row = s.execute(select(Invite).where(Invite.email == "owner@example.test")).scalar_one()
        assert row.status == "pending"
