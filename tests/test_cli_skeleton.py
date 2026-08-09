from typer.testing import CliRunner

from usali.cli import app

runner = CliRunner()


def test_cli_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "seed-schedules",
        "seed-mappings",
        "ingest",
        "transform",
        "gen-opera-draft",
        "process",
        "watch",
        "serve",
        "report",
        "coverage",
        "export",
        "qbo-mock",
        "qbo-push",
        "qbo-status",
        "cpa-pack",
    ):
        assert cmd in result.output
