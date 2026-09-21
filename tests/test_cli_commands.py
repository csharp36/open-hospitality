import re
from decimal import Decimal
from pathlib import Path

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
    # One demo property per registered source in mapping/properties.yaml.
    assert "Seeded 4 propert" in result.output


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


def test_watch_drains_every_accepted_suffix_already_in_the_inbox(tmp_path, monkeypatch):
    """The startup drain and the live handler accept the same suffixes: a file
    already waiting when watch starts is processed exactly as one arriving
    later would be. No DB and no observer thread: process_file is replaced by
    a recorder, the Observer by a stub, and the first main-loop sleep raises
    KeyboardInterrupt so the command exits through its own Ctrl-C path. The
    Observer and time.sleep patches work because watch_cmd imports both lazily
    and reaches them through the module attribute; if that import moves to
    module level the stub goes inert and a real observer thread would start."""
    import contextlib
    import time
    from types import SimpleNamespace

    import watchdog.observers

    import usali.cli as cli

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "b.pdf").touch()  # contents irrelevant: process_file is stubbed below
    (inbox / "a.xlsx").touch()
    (inbox / "notes.txt").write_text("not a report")

    seen: list[str] = []

    def fake_process_file(session, path, **kwargs):
        seen.append(Path(path).name)
        return SimpleNamespace(mapped=0, skipped=0)

    class StubObserver:
        def schedule(self, handler, path): ...
        def start(self): ...
        def stop(self): ...
        def join(self): ...

    def stop_now(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "process_file", fake_process_file)
    monkeypatch.setattr(cli, "_session_factory", lambda: lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(watchdog.observers, "Observer", StubObserver)
    monkeypatch.setattr(time, "sleep", stop_now)

    result = runner.invoke(app, [
        "watch", "--inbox-dir", str(inbox),
        "--processed-dir", str(tmp_path / "done"), "--failed-dir", str(tmp_path / "fail"),
    ])
    assert result.exit_code == 0, result.output
    assert seen == ["a.xlsx", "b.pdf"], seen  # sorted, both suffixes, the .txt ignored


def test_watch_deletes_the_inbox_file_after_processing(tmp_path, monkeypatch):
    """process_file no longer moves or deletes its input (Task 3): the
    redacted artifact it files is the trace, so watch must remove the inbox
    copy itself or the next start would re-read it. Same stub/observer setup
    as test_watch_drains_every_accepted_suffix_already_in_the_inbox."""
    import contextlib
    import time
    from datetime import date
    from types import SimpleNamespace

    import watchdog.observers

    import usali.cli as cli

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").touch()  # contents irrelevant: process_file is stubbed below

    def fake_process_file(session, path, **kwargs):
        return SimpleNamespace(
            pms_source="OPERA",
            report_type="trial_balance",
            property_id="HISJ",
            business_date=date(2026, 7, 7),
            staged=1,
            mapped=1,
            unmapped=0,
            skipped=0,
            destination=tmp_path / "done" / "a.pdf",
        )

    class StubObserver:
        def schedule(self, handler, path): ...
        def start(self): ...
        def stop(self): ...
        def join(self): ...

    def stop_now(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "process_file", fake_process_file)
    monkeypatch.setattr(cli, "_session_factory", lambda: lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(watchdog.observers, "Observer", StubObserver)
    monkeypatch.setattr(time, "sleep", stop_now)

    result = runner.invoke(app, [
        "watch", "--inbox-dir", str(inbox),
        "--processed-dir", str(tmp_path / "done"), "--failed-dir", str(tmp_path / "fail"),
    ])
    assert result.exit_code == 0, result.output
    assert not (inbox / "a.pdf").exists()


def test_watch_deletes_the_inbox_file_after_a_failure(tmp_path, monkeypatch):
    """A failure files an error record instead of an artifact, but the trace
    still lives outside the inbox, so the file must still go."""
    import contextlib
    import time

    import watchdog.observers

    import usali.cli as cli
    from usali.ingestion import ProcessingError

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.pdf").touch()  # contents irrelevant: process_file is stubbed below

    def fake_process_file(session, path, **kwargs):
        raise ProcessingError("a.pdf: could not detect report type")

    class StubObserver:
        def schedule(self, handler, path): ...
        def start(self): ...
        def stop(self): ...
        def join(self): ...

    def stop_now(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "process_file", fake_process_file)
    monkeypatch.setattr(cli, "_session_factory", lambda: lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(watchdog.observers, "Observer", StubObserver)
    monkeypatch.setattr(time, "sleep", stop_now)

    result = runner.invoke(app, [
        "watch", "--inbox-dir", str(inbox),
        "--processed-dir", str(tmp_path / "done"), "--failed-dir", str(tmp_path / "fail"),
    ])
    assert result.exit_code == 0, result.output
    assert not (inbox / "a.pdf").exists()
    assert "FAILED a.pdf" in result.output  # the failure is still reported


def test_process_leaves_the_argument_in_place(db_url, tmp_path):
    """D2: `process` is the operator's own invocation, not the drained inbox
    — the pipeline never moved the file (Task 3) and `process` must not
    either. Stubbed the way test_process_command_runs_full_pipeline is, but
    against a real pipeline run since process_file itself is not mocked
    here; a plain touch()'d PDF would fail detection before reaching the
    unlink question this test is actually about, so seed and copy a real
    sample the same way that test does."""
    import shutil

    # seed-properties find-or-creates org 1 (see
    # test_gl_seed_chart_fill_usali_reports_counts) — needed here because,
    # run alone, this test has no earlier test in the session to leave it
    # behind, unlike test_process_command_runs_full_pipeline above.
    runner.invoke(app, ["seed-properties", "mapping/properties.yaml"])
    runner.invoke(app, ["seed-schedules", "mapping/usali_schedules.yaml"])
    runner.invoke(app, ["seed-mappings", "mapping/opera.yaml"])
    pdf = tmp_path / "keep.pdf"
    shutil.copy(Path("docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf"), pdf)

    result = runner.invoke(
        app,
        ["process", str(pdf), "--processed-dir", str(tmp_path / "done"),
         "--failed-dir", str(tmp_path / "failed")],
    )
    assert result.exit_code == 0, result.output
    assert pdf.exists()
