import re
from decimal import Decimal

from sqlalchemy import select
from typer.testing import CliRunner

from usali import gl_chart
from usali.cli import app
from usali.models import JournalEntry, JournalLine

from tests.test_gl_parity import _post_all_grains

runner = CliRunner()


def test_seed_schedules_command_runs(db_url):
    # db_url fixture sets USALI_DB_URL to the testcontainer and migrates it.
    result = runner.invoke(app, ["seed-schedules", "mapping/usali_schedules.yaml"])
    assert result.exit_code == 0, result.output
    assert "16" in result.output


def test_seed_properties_command_runs(db_url):
    result = runner.invoke(app, ["seed-properties", "mapping/properties.yaml"])
    assert result.exit_code == 0, result.output
    assert "Seeded 3 propert" in result.output


def test_ingest_opera_trial_balance(db_url):
    result = runner.invoke(
        app,
        [
            "ingest",
            "docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf",
            "--source", "OPERA",
            "--report", "trial_balance",
            "--property-id", "HISJ",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2026-07-07" in result.output  # business date derived from the report header


def test_ingest_autoclerk_transaction_summary(db_url):
    result = runner.invoke(
        app,
        [
            "ingest",
            "docs/reference/samples/Autoclerk - Transaction Summary 07.07.2026.pdf",
            "--source", "AUTOCLERK",
            "--report", "transaction_summary",
            "--property-id", "SSSJ",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2026-07-07" in result.output  # business date derived from the report


def test_ingest_rejects_unsupported_combo(db_url):
    result = runner.invoke(
        app,
        ["ingest", "x.pdf", "--source", "OPERA", "--report", "manager_flash", "--property-id", "H"],
    )
    assert result.exit_code != 0


def test_process_command_runs_full_pipeline(db_url, tmp_path):
    import shutil
    from pathlib import Path

    runner.invoke(app, ["seed-schedules", "mapping/usali_schedules.yaml"])
    runner.invoke(app, ["seed-mappings", "mapping/opera.yaml"])
    pdf = tmp_path / "tb.pdf"
    shutil.copy(Path("docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf"), pdf)

    result = runner.invoke(
        app,
        ["process", str(pdf), "--processed-dir", str(tmp_path / "done"),
         "--failed-dir", str(tmp_path / "failed")],
    )
    assert result.exit_code == 0, result.output
    assert "OPERA" in result.output and "HISJ" in result.output


def test_gl_seed_chart_fill_usali_reports_counts(db_url):
    # seed-properties find-or-creates org 1, which the chart rows FK onto.
    runner.invoke(app, ["seed-properties", "mapping/properties.yaml"])
    result = runner.invoke(app, ["gl-seed-chart", "--fill-usali"])
    assert result.exit_code == 0, result.output
    assert "Seeded" in result.output
    assert "filled" in result.output and "left alone" in result.output


def test_gl_parity_command_reports_parity_holds(
    db_url, db_session, founding_org, seed_six_pdfs
):
    gl_chart.seed_chart(db_session, org_id=1)
    pairs = _post_all_grains(db_session)
    db_session.commit()

    prop = pairs[0][0]
    days = sorted(d for p, d in pairs if p == prop)
    result = runner.invoke(
        app, ["gl-parity", prop, days[0].isoformat(), days[-1].isoformat()]
    )
    assert result.exit_code == 0, result.output
    assert "parity holds" in result.output


def test_gl_parity_command_all_properties_reports_parity_holds(
    db_url, db_session, founding_org, seed_six_pdfs
):
    gl_chart.seed_chart(db_session, org_id=1)
    _post_all_grains(db_session)
    db_session.commit()

    result = runner.invoke(app, ["gl-parity", "--all-properties"])
    assert result.exit_code == 0, result.output
    assert "parity holds" in result.output


def test_gl_parity_command_reports_a_broken_journal_line(
    db_url, db_session, founding_org, seed_six_pdfs
):
    gl_chart.seed_chart(db_session, org_id=1)
    _post_all_grains(db_session)
    # Commit the posting first: the balance trigger is a DEFERRABLE INITIALLY
    # DEFERRED constraint trigger queued by INSERT and evaluated at COMMIT
    # against the row's THEN-current value, so mutating a just-inserted,
    # still-uncommitted line would make its own posting look unbalanced.
    db_session.commit()

    # A journal_line UPDATE, on the superuser session db_session runs as —
    # tests/test_gl_wall.py::test_the_journal_refuses_update_and_delete_by_grant
    # is where that revoke is proved against the app role, not this one.
    row = db_session.execute(
        select(JournalLine, JournalEntry.property_id, JournalEntry.business_date)
        .join(JournalEntry, JournalEntry.entry_id == JournalLine.entry_id)
        .where(JournalLine.fact_id.is_not(None))
    ).first()
    assert row is not None
    line, prop, day = row
    account = line.account_code
    line.amount = line.amount + Decimal("15.00")
    db_session.commit()

    result = runner.invoke(
        app, ["gl-parity", prop, day.isoformat(), day.isoformat()]
    )
    assert result.exit_code == 1
    assert account in result.output
    # The range rides on the diff line so an operator can re-run this exact
    # grain with the single-property form without re-deriving dates.
    assert f"{day} to {day}" in result.output
    assert "FAILED: 1 account(s) diverge" in result.output


def test_gl_parity_command_all_properties_on_an_empty_db_fails(db_url, db_session):
    # db_session's per-test TRUNCATE is what makes this DB actually empty —
    # db_url alone is the session-scoped container, carrying whatever an
    # earlier test in this file already committed to it.
    # No posting has ever happened here: zero properties checked is not the
    # same claim as zero diffs, and the T6 gate must not read it as green.
    result = runner.invoke(app, ["gl-parity", "--all-properties"])
    assert result.exit_code != 0
    assert "nothing was checked" in result.output


def _squashed(output: str) -> str:
    """Rich wraps the BadParameter panel to the terminal width, splitting
    long tokens (at their hyphens) across box-bordered lines and, on CI,
    threading ANSI codes through — so a substring match against the raw
    output depends on where the wrap landed. Strip the decoration and ALL
    whitespace; needles must be squashed the same way."""
    text = re.sub(r"\x1b\[[^m]*m", "", output)
    return re.sub(r"[\s│╭╮╰╯─]+", "", text)


def test_gl_parity_command_rejects_partial_args():
    result = runner.invoke(app, ["gl-parity", "HISJ", "2026-07-01"])
    assert result.exit_code != 0
    assert "PROPERTY,DATE_FROM,andDATE_TO" in _squashed(result.output)


def test_gl_parity_command_rejects_args_with_all_properties():
    result = runner.invoke(
        app, ["gl-parity", "HISJ", "2026-07-01", "2026-07-01", "--all-properties"]
    )
    assert result.exit_code != 0
    assert "--all-properties" in _squashed(result.output)


def test_gl_parity_command_rejects_start_after_end():
    result = runner.invoke(app, ["gl-parity", "HISJ", "2026-07-02", "2026-07-01"])
    assert result.exit_code != 0
    assert "isafter" in _squashed(result.output)
