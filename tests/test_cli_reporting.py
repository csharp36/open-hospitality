"""CLI wiring for the P6 reporting commands (report / coverage / export).

Happy paths against a real database seeded through the CLI itself, plus the
validation errors. Report/render depth lives in the P6 Tasks 1-4 test modules;
these tests only prove the commands are wired to reporting + render correctly.
"""

import json
import shutil
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from usali.cli import app

runner = CliRunner()

SAMPLES = Path("docs/reference/samples")
SEED_PDFS = (
    "Trial Balance 07.07.2026 - Opera.pdf",  # financial facts (HISJ)
    "Manager Flash 07.07.2026 - Opera.pdf",  # statistic facts (HISJ)
)


def _seed_via_cli(tmp_path: Path) -> None:
    """Seed schedules + mappings, then run two sample PDFs through `process`."""
    for args in (
        ["seed-schedules", "mapping/usali_schedules.yaml"],
        ["seed-mappings", "mapping/opera.yaml"],
        ["seed-properties", "mapping/properties.yaml"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    for name in SEED_PDFS:
        pdf = tmp_path / name
        shutil.copy(SAMPLES / name, pdf)
        result = runner.invoke(
            app,
            ["process", str(pdf), "--processed-dir", str(tmp_path / "done"),
             "--failed-dir", str(tmp_path / "failed")],
        )
        assert result.exit_code == 0, result.output


def test_report_coverage_export_happy_paths(db_session, tmp_path):
    # db_session truncates the shared test database; CLI commands connect via
    # USALI_DB_URL (set by the session-scoped db_url fixture underneath).
    _seed_via_cli(tmp_path)

    # report --format json to stdout
    result = runner.invoke(
        app, ["report", "--property", "HISJ", "--date", "2026-07-07", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # Facts are Numeric(15,4), so the exact string carries the DB scale ("10866.3700").
    assert Decimal(data["total_operating_revenue"]) == Decimal("10866.37")
    assert data["pms_source"] == "OPERA"
    assert data["rooms_segments"] == []  # no segment PDFs seeded
    assert data["statistics"]  # Manager Flash seeded the statistics facts

    # report default text format via --out FILE
    out_file = tmp_path / "sos.txt"
    result = runner.invoke(
        app, ["report", "--property", "HISJ", "--date", "2026-07-07", "--out", str(out_file)]
    )
    assert result.exit_code == 0, result.output
    assert "SUMMARY OPERATING STATEMENT" in out_file.read_text()

    # report range mode (--from/--to)
    result = runner.invoke(
        app,
        ["report", "--property", "HISJ", "--from", "2026-07-01", "--to", "2026-07-31",
         "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    ranged = json.loads(result.output)
    assert Decimal(ranged["total_operating_revenue"]) == Decimal("10866.37")

    # coverage text
    result = runner.invoke(app, ["coverage"])
    assert result.exit_code == 0, result.output
    assert "MAPPING COVERAGE REPORT" in result.output
    assert "OPERA" in result.output

    # export csv via --out: header + seeded financial rows
    csv_file = tmp_path / "financial.csv"
    result = runner.invoke(
        app,
        ["export", "--table", "financial", "--from", "2026-07-01", "--to", "2026-07-31",
         "--property", "HISJ", "--out", str(csv_file)],
    )
    assert result.exit_code == 0, result.output
    lines = csv_file.read_text().splitlines()
    assert "business_date" in lines[0] and "amount" in lines[0]
    assert len(lines) > 1 and '"HISJ"' in lines[1]

    # export over an empty range still emits a header-only CSV
    result = runner.invoke(
        app, ["export", "--table", "segments", "--from", "2020-01-01", "--to", "2020-01-02"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip().count("\n") == 0
    assert "business_date" in result.output


def test_report_requires_exactly_one_date_mode():
    both = runner.invoke(
        app,
        ["report", "--property", "HISJ", "--date", "2026-07-07",
         "--from", "2026-07-01", "--to", "2026-07-31"],
    )
    assert both.exit_code != 0

    neither = runner.invoke(app, ["report", "--property", "HISJ"])
    assert neither.exit_code != 0


def test_export_rejects_unknown_table():
    result = runner.invoke(
        app, ["export", "--table", "bogus", "--from", "2026-07-01", "--to", "2026-07-02"]
    )
    assert result.exit_code != 0
