from typer.testing import CliRunner

from usali.cli import app

runner = CliRunner()


def test_seed_schedules_command_runs(db_url):
    # db_url fixture sets USALI_DB_URL to the testcontainer and migrates it.
    result = runner.invoke(app, ["seed-schedules", "mapping/usali_schedules.yaml"])
    assert result.exit_code == 0, result.output
    assert "16" in result.output


def test_seed_properties_command_runs(db_url):
    result = runner.invoke(app, ["seed-properties", "mapping/properties.yaml"])
    assert result.exit_code == 0, result.output
    assert "Seeded 2 propert" in result.output


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
