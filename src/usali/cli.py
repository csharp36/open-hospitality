import hashlib
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TypeVar, cast, get_args

import httpx
import typer
from sqlalchemy import select

from usali import invites, qbo_push, render, reporting
from usali.adaptors import autoclerk_transaction_summary as autoclerk
from usali.adaptors import opera_trial_balance as opera
from usali.adaptors.pdf import extract_words
from usali.config import get_settings
from usali.db import make_engine, make_session_factory
from usali.ingestion import ProcessingError, process_file
from usali.labor import promote_timecard
from usali.mapping.draft_gen import generate_draft
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.models import Timecard
from usali.keycloak_admin import KeycloakAdminClient, KeycloakAdminError
from usali.notifications import Notifier, notifier_from_settings
from usali.photo_store import LocalPhotoStore
from usali.qbo_client import QboClient
from usali.roster_seed import RosterError, load_roster, seed_roster
from usali.stage import stage_records
from usali.tenancy import FOUNDING_ORG_ID, OrgBoundSessionFactory
from usali.timecards import purge_approved_photos
from usali.transform import transform

app = typer.Typer(help="USALI ingestion & mapping engine")


def _session_factory() -> OrgBoundSessionFactory:
    # L2: CLI sessions are org-bound like the server's — in dev/cloud the
    # connection role sits behind FORCE RLS, so an unbound session would
    # read zero rows and refuse every write. FOUNDING_ORG_ID is the L3
    # seam (the CLI is a single-org operator surface until then).
    engine = make_engine(get_settings().db_url)
    return OrgBoundSessionFactory(make_session_factory(engine), FOUNDING_ORG_ID)


def _notifier() -> Notifier:
    return notifier_from_settings(get_settings())


def _file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _parse_date(value: str, option: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{option} must be YYYY-MM-DD, got {value!r}") from exc


R = TypeVar("R", bound=Callable[..., str])


def _pick(renderers: dict[str, R], fmt: str) -> R:
    renderer = renderers.get(fmt)
    if renderer is None:
        raise typer.BadParameter(f"--format must be one of {', '.join(sorted(renderers))}")
    return renderer


def _emit(text: str, out: str | None) -> None:
    """Write rendered output to --out FILE, or echo it to stdout."""
    if out is None:
        typer.echo(text, nl=not text.endswith("\n"))
    else:
        Path(out).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


@app.command("seed-schedules")
def seed_schedules_cmd(
    yaml_path: str = typer.Argument("mapping/usali_schedules.yaml"),
) -> None:
    """Load the USALI schedule reference table."""
    with _session_factory()() as s:
        n = seed_schedules(s, yaml_path)
        s.commit()
    typer.echo(f"Seeded {n} schedule rows")


@app.command("seed-mappings")
def seed_mappings_cmd(yaml_path: str = typer.Argument("mapping/opera.yaml")) -> None:
    """Load a curated PMS->USALI mapping file into the dictionary."""
    with _session_factory()() as s:
        n = load_mappings(s, yaml_path)
        s.commit()
    typer.echo(f"Loaded {n} mapping entries")


@app.command("seed-properties")
def seed_properties_cmd(
    yaml_path: str = typer.Argument("mapping/properties.yaml"),
) -> None:
    """Load the property registry (org + property + detection aliases)."""
    with _session_factory()() as s:
        n = seed_properties(s, yaml_path)
        s.commit()
    typer.echo(f"Seeded {n} properties")


@app.command("invite")
def invite_cmd(
    email: str = typer.Argument(..., help="Email address to invite (pilot gate)"),
) -> None:
    """Create a one-time invite for EMAIL and send the signup link (D-B4).

    Pilot invite origination: writes a pending invite row and emails the link
    via the configured Notifier (console in dev — the link is logged). A GA
    admin surface replaces this later; public signup CONSUMES the invite.
    """
    settings = get_settings()
    with _session_factory()() as s:
        invite, raw_token = invites.create_invite(s, email)
        s.commit()
    link = f"{settings.public_base_url.rstrip('/')}/signup?token={raw_token}"
    _notifier().send_email(
        to=email,
        subject="Your Open Hospitality workspace invite",
        body=f"You've been invited to create a workspace. Open: {link}",
    )
    typer.echo(f"Invited {email}: {link}")


@app.command("gen-opera-draft")
def gen_opera_draft_cmd(xml_path: str, out_path: str, edition: int = 12) -> None:
    """Generate a draft mapping YAML from the Opera transaction-code catalog."""
    n = generate_draft(xml_path, out_path, edition=edition)
    typer.echo(f"Wrote {n} draft mappings to {out_path}")


@app.command("ingest")
def ingest_cmd(
    pdf_path: str = typer.Argument(..., help="Path to the PDF report to ingest"),
    source: str = typer.Option(..., help="PMS source: OPERA or AUTOCLERK"),
    report: str = typer.Option(..., help="Report type: trial_balance or transaction_summary"),
    property_id: str = typer.Option(..., help="Property identifier"),
) -> None:
    """Parse a PDF report, derive its business date, and load it into the stage table."""
    if (source, report) not in {
        ("OPERA", "trial_balance"),
        ("AUTOCLERK", "transaction_summary"),
    }:
        raise typer.BadParameter(
            "supported: --source OPERA --report trial_balance, "
            "or --source AUTOCLERK --report transaction_summary"
        )
    words = extract_words(pdf_path)
    if (source, report) == ("OPERA", "trial_balance"):
        business_date = opera.extract_business_date(words)
        records = opera.parse_trial_balance(
            words, property_id=property_id, business_date=business_date
        )
    else:
        business_date = autoclerk.extract_business_date(words)
        records = autoclerk.parse_transaction_summary(
            words, property_id=property_id, business_date=business_date
        )
    with _session_factory()() as s:
        batch = stage_records(s, records, source_file=pdf_path, file_hash=_file_hash(pdf_path))
        s.commit()
        typer.echo(f"Staged {len(records)} rows for {business_date} in batch {batch.batch_id}")


@app.command("transform")
def transform_cmd(
    source: str = typer.Option(..., help="PMS source, e.g. OPERA"),
    business_date: str = typer.Option(..., help="Business date, YYYY-MM-DD"),
    edition: int = typer.Option(12, help="USALI edition (11 or 12)"),
) -> None:
    """Map staged rows to usali_financial_fact and reconcile."""
    with _session_factory()() as s:
        result = transform(
            s, source=source, business_date=date.fromisoformat(business_date), edition=edition
        )
        s.commit()
    typer.echo(
        f"mapped={result.mapped} unmapped={result.unmapped} "
        f"skipped={result.skipped} reconciled={result.reconciled}"
    )


@app.command("process")
def process_cmd(
    pdf_path: str = typer.Argument(..., help="PDF to run through the full pipeline"),
    processed_dir: str | None = typer.Option(None, help="Where successful files are filed"),
    failed_dir: str | None = typer.Option(None, help="Where failed files are quarantined"),
    edition: int = typer.Option(12, help="USALI edition"),
) -> None:
    """Detect, parse, stage, transform, and file one PDF (auto-detects source/report/property)."""
    settings = get_settings()
    with _session_factory()() as s:
        try:
            r = process_file(
                s,
                pdf_path,
                processed_dir=Path(processed_dir) if processed_dir else settings.processed_dir,
                failed_dir=Path(failed_dir) if failed_dir else settings.failed_dir,
                edition=edition,
            )
        except ProcessingError as exc:
            typer.echo(f"FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    typer.echo(
        f"{r.pms_source}/{r.report_type} {r.property_id} {r.business_date}: "
        f"staged={r.staged} mapped={r.mapped} unmapped={r.unmapped} skipped={r.skipped} "
        f"-> {r.destination}"
    )


@app.command("watch")
def watch_cmd(
    inbox_dir: str | None = typer.Option(None, help="Sentinel directory to watch for PDFs"),
    processed_dir: str | None = typer.Option(None, help="Where successful files are filed"),
    failed_dir: str | None = typer.Option(None, help="Where failed files are quarantined"),
    edition: int = typer.Option(12, help="USALI edition"),
) -> None:
    """Watch the inbox directory and run the full pipeline on every PDF that appears."""
    import time

    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    settings = get_settings()
    inbox = Path(inbox_dir) if inbox_dir else settings.inbox_dir
    processed = Path(processed_dir) if processed_dir else settings.processed_dir
    failed = Path(failed_dir) if failed_dir else settings.failed_dir
    inbox.mkdir(parents=True, exist_ok=True)

    def handle(path: Path) -> None:
        if path.suffix.lower() != ".pdf":
            return
        with _session_factory()() as s:
            try:
                r = process_file(
                    s, path,
                    processed_dir=processed,
                    failed_dir=failed,
                    edition=edition,
                )
                typer.echo(f"processed {path.name}: mapped={r.mapped} skipped={r.skipped}")
            except ProcessingError as exc:
                typer.echo(f"FAILED {path.name}: {exc}", err=True)

    class Handler(FileSystemEventHandler):
        def on_created(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                time.sleep(0.5)  # allow the writer (mail client, scp, cp) to finish
                handle(Path(str(event.src_path)))

    for existing in sorted(inbox.glob("*.pdf")):  # drain anything already waiting
        handle(existing)

    observer = Observer()
    observer.schedule(Handler(), str(inbox))
    observer.start()
    typer.echo(f"watching {inbox} (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


_SOS_RENDERERS: dict[str, Callable[[reporting.SosReport], str]] = {
    "text": render.render_sos_text,
    "json": render.render_sos_json,
    "csv": render.render_sos_csv,
}
_COVERAGE_RENDERERS: dict[str, Callable[[reporting.CoverageReport], str]] = {
    "text": render.render_coverage_text,
    "json": render.render_coverage_json,
}
_EXPORT_TABLES: tuple[str, ...] = get_args(reporting.ExportTable)


@app.command("report")
def report_cmd(
    property_id: str = typer.Option(..., "--property", help="Property identifier"),
    business_date: str | None = typer.Option(
        None, "--date", help="Single business date, YYYY-MM-DD"
    ),
    date_from: str | None = typer.Option(
        None, "--from", help="Range start (inclusive), YYYY-MM-DD"
    ),
    date_to: str | None = typer.Option(None, "--to", help="Range end (inclusive), YYYY-MM-DD"),
    fmt: str = typer.Option("text", "--format", help="Output format: text, json, or csv"),
    out: str | None = typer.Option(None, "--out", help="Write output to FILE instead of stdout"),
) -> None:
    """Render the Summary Operating Statement for one property."""
    renderer = _pick(_SOS_RENDERERS, fmt)
    single = business_date is not None
    ranged = date_from is not None or date_to is not None
    if single == ranged:
        raise typer.BadParameter("pass exactly one of --date, or both --from and --to")
    if ranged and (date_from is None or date_to is None):
        raise typer.BadParameter("--from and --to must be given together")
    parsed_date = _parse_date(business_date, "--date") if business_date else None
    parsed_from = _parse_date(date_from, "--from") if date_from else None
    parsed_to = _parse_date(date_to, "--to") if date_to else None
    with _session_factory()() as s:
        try:
            sos = reporting.summary_operating_statement(
                s,
                property_id=property_id,
                business_date=parsed_date,
                date_from=parsed_from,
                date_to=parsed_to,
            )
        except ValueError as exc:
            typer.echo(f"FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    _emit(renderer(sos), out)


@app.command("coverage")
def coverage_cmd(
    edition: int = typer.Option(12, help="USALI edition (11 or 12)"),
    fmt: str = typer.Option("text", "--format", help="Output format: text or json"),
    out: str | None = typer.Option(None, "--out", help="Write output to FILE instead of stdout"),
) -> None:
    """Render the mapping coverage and confidence report."""
    renderer = _pick(_COVERAGE_RENDERERS, fmt)
    with _session_factory()() as s:
        try:
            report = reporting.coverage_report(s, edition=edition)
        except ValueError as exc:
            typer.echo(f"FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    _emit(renderer(report), out)


@app.command("export")
def export_cmd(
    table: str = typer.Option(..., help="Fact table: financial, statistics, or segments"),
    date_from: str = typer.Option(..., "--from", help="Range start (inclusive), YYYY-MM-DD"),
    date_to: str = typer.Option(..., "--to", help="Range end (inclusive), YYYY-MM-DD"),
    property_id: str | None = typer.Option(None, "--property", help="Limit to one property"),
    fmt: str = typer.Option("csv", "--format", help="Output format: csv or json"),
    out: str | None = typer.Option(None, "--out", help="Write output to FILE instead of stdout"),
) -> None:
    """Export one fact table as flat CSV or JSON rows for ERP/BI hand-off."""
    if table not in _EXPORT_TABLES:
        raise typer.BadParameter(
            f"--table must be one of {', '.join(sorted(_EXPORT_TABLES))}, got {table!r}"
        )
    if fmt not in ("csv", "json"):
        raise typer.BadParameter("--format must be one of csv, json")
    export_table = cast(reporting.ExportTable, table)
    start = _parse_date(date_from, "--from")
    end = _parse_date(date_to, "--to")
    with _session_factory()() as s:
        try:
            rows = reporting.export_rows(
                s, export_table, date_from=start, date_to=end, property_id=property_id
            )
        except ValueError as exc:
            typer.echo(f"FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    if fmt == "csv":
        text = render.rows_to_csv(rows, fieldnames=reporting.export_columns(export_table))
    else:
        text = render.rows_to_json(rows)
    _emit(text, out)


_CPA_STREAM_RENDERERS: dict[str, Callable[[reporting.CpaPack], str]] = {
    "text": render.render_cpa_pack_text,
    "json": render.render_cpa_pack_json,
}
_CPA_STREAM_FILENAMES = {"text": "cpa-pack.txt", "json": "cpa-pack.json"}


@app.command("cpa-pack")
def cpa_pack_cmd(
    property_id: str = typer.Option(..., "--property", help="Property identifier"),
    month: str = typer.Option(..., "--month", help="Month, YYYY-MM"),
    fmt: str = typer.Option("text", "--format", help="Output format: text, json, or csv"),
    out: str | None = typer.Option(
        None, "--out", help="Directory to write output into (required for csv)"
    ),
) -> None:
    """Render the CPA monthly pack (sales, tax, A/R) for one property."""
    if fmt not in ("text", "json", "csv"):
        raise typer.BadParameter("--format must be one of csv, json, text")
    if fmt == "csv" and out is None:
        # Plain stderr line (not typer.BadParameter): the Rich-rendered usage
        # panel wraps/annotates the option token under a narrow terminal, so a
        # caller — or a test — can't reliably see "--out" in the message. Match
        # the loud-FAILED style used for the unknown-property case below.
        typer.echo("FAILED: --format csv writes three files; pass --out DIR", err=True)
        raise typer.Exit(code=1)
    try:
        reporting.month_bounds(month)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    with _session_factory()() as s:
        try:
            pack = reporting.cpa_pack(s, property_id=property_id, month=month)
        except ValueError as exc:
            typer.echo(f"FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    if fmt == "csv":
        assert out is not None  # guarded above
        out_dir = Path(out)
        out_dir.mkdir(parents=True, exist_ok=True)
        files = {
            "sales_report.csv": render.sales_report_csv(pack),
            "tax_report.csv": render.tax_report_csv(pack),
            "ar_report.csv": render.ar_report_csv(pack),
        }
        for name, text in files.items():
            (out_dir / name).write_text(text, encoding="utf-8")
        typer.echo(f"wrote {', '.join(files)} to {out_dir}")
        return
    text = _CPA_STREAM_RENDERERS[fmt](pack)
    if out is None:
        typer.echo(text, nl=not text.endswith("\n"))
    else:
        out_dir = Path(out)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / _CPA_STREAM_FILENAMES[fmt]
        target.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


@app.command("seed-roster")
def seed_roster_cmd(
    csv_path: str = typer.Argument(..., help="Roster CSV (see seed/roster.example.csv)"),
    property_id: str = typer.Option(..., "--property-id"),
) -> None:
    """Seed employees + Keycloak operators from a roster CSV.

    Identity and org placement only. The loader REFUSES a file carrying SSNs,
    bank details, or pay rates -- a realm user has no field for them, and a
    plaintext copy on disk defeats the sealed-PII design.
    """
    try:
        rows = load_roster(Path(csv_path))
    except RosterError as exc:
        typer.echo(f"Roster rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    kc = KeycloakAdminClient.from_settings(get_settings())
    with _session_factory()() as s:
        try:
            result = seed_roster(s, kc, rows, property_id=property_id)
        except KeycloakAdminError as exc:
            # seed_roster commits per row, so rows before this one are durable
            # and a re-run resumes. Say so -- a bare traceback here reads as
            # "everything failed" and invites a destructive manual cleanup.
            typer.echo(
                f"Seeding stopped at a Keycloak error: {exc}\n"
                "Rows before this one were committed. Fix the cause and re-run "
                "the same file; already-seeded rows are skipped.",
                err=True,
            )
            raise typer.Exit(code=1) from exc
    typer.echo(
        f"Seeded {result.created} employee(s), skipped {result.skipped} existing, "
        f"created {result.departments_created} department(s)"
    )


@app.command("purge-punch-photos")
def purge_punch_photos_cmd() -> None:
    """Delete punch photos for timecards approved beyond the retention window."""
    settings = get_settings()
    store = LocalPhotoStore(settings.photo_store_dir)
    with _session_factory()() as s:
        n = purge_approved_photos(
            s, store, retention_days=settings.punch_photo_retention_days
        )
        s.commit()
    typer.echo(f"Purged {n} punch photos")


@app.command("promote-labor")
def promote_labor_cmd() -> None:
    """Promote every approved timecard to labor facts (idempotent backfill)."""
    settings = get_settings()
    anchor = settings.payroll_period_anchor
    total = 0
    with _session_factory()() as s:
        cards = s.execute(
            select(Timecard).where(Timecard.status == "approved")
        ).scalars().all()
        for card in cards:
            total += promote_timecard(s, card, anchor=anchor)
        s.commit()
    typer.echo(f"Promoted {total} labor facts across {len(cards)} approved timecards")


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind address (localhost only; no auth)"),
    port: int = typer.Option(8100, help="Port"),
    proxy_headers: bool = typer.Option(
        False, "--proxy-headers",
        help="Trust X-Forwarded-* (behind a platform edge like Cloud "
             "Run, which overwrites them; never for a directly exposed "
             "server)",
    ),
) -> None:
    """Serve the local upload endpoint: curl -F file=@report.pdf http://127.0.0.1:8100/ingest"""
    import uvicorn

    from usali.server import create_app

    uvicorn.run(
        create_app(), host=host, port=port,
        proxy_headers=proxy_headers,
        forwarded_allow_ips="*" if proxy_headers else None,
    )


def _format_je_plan(plan: qbo_push.JePlan) -> str:
    """Fixed-width table of one JE plan (dry-run output)."""
    out = [
        f"JE plan {plan.property_id} {plan.business_date.isoformat()}"
        f"  request_hash={plan.request_hash[:12]}",
        f"  {'account':<8} {'name':<38} {'side':<7} {'amount':>14}",
    ]
    for line in plan.lines:
        out.append(
            f"  {line.gl_account_code:<8} {line.account_name:<38} "
            f"{line.posting:<7} {line.amount:>14,.2f}"
        )
    out.append(
        f"  totals: debits {plan.total_debits:,.2f} == credits {plan.total_credits:,.2f}"
    )
    return "\n".join(out)


def _qbo_client_from_settings() -> QboClient:
    settings = get_settings()
    return QboClient(
        settings.qbo_base_url,
        settings.qbo_client_id,
        settings.qbo_client_secret,
        settings.qbo_realm_id,
        settings.qbo_refresh_token,
    )


@app.command("qbo-push")
def qbo_push_cmd(
    property_id: str = typer.Option(..., "--property", help="Property identifier"),
    business_date: str | None = typer.Option(
        None, "--date", help="Single business date, YYYY-MM-DD"
    ),
    month: str | None = typer.Option(
        None, "--month", help="Push every fact date in the month, YYYY-MM"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the JE plan(s) without posting or recording anything"
    ),
) -> None:
    """Post one balanced journal entry per business date to QuickBooks Online."""
    if (business_date is None) == (month is None):
        raise typer.BadParameter("pass exactly one of --date or --month")
    with _session_factory()() as s:
        try:
            if business_date is not None:
                dates = [_parse_date(business_date, "--date")]
            else:
                assert month is not None
                dates = qbo_push.fact_dates(s, property_id=property_id, month=month)
            if dry_run:
                for d in dates:
                    plan = qbo_push.build_journal_entry(
                        s, property_id=property_id, business_date=d
                    )
                    typer.echo(_format_je_plan(plan))
                return
            with _qbo_client_from_settings() as client:
                if business_date is not None:
                    d = dates[0]
                    results = [
                        (d, qbo_push.push_day(s, client, property_id=property_id,
                                              business_date=d))
                    ]
                else:
                    assert month is not None
                    results = qbo_push.push_month(
                        s, client, property_id=property_id, month=month
                    )
        except qbo_push.UnmappedGlError as exc:
            typer.echo(f"FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        except ValueError as exc:
            typer.echo(f"FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        except httpx.HTTPError as exc:
            # Connection refused / DNS failure / timeout — the QBO endpoint
            # itself is unreachable, not a per-date outcome: fail loudly.
            typer.echo(
                f"FAILED: cannot reach QBO at {get_settings().qbo_base_url}: {exc}",
                err=True,
            )
            raise typer.Exit(code=1) from exc
    any_failed = False
    for d, result in results:
        parts = [d.isoformat(), result.status]
        if result.qbo_je_id is not None:
            parts.append(f"je={result.qbo_je_id}")
        if result.message:
            parts.append(f"- {result.message}")
        typer.echo(" ".join(parts))
        any_failed = any_failed or result.status == "failed"
    if any_failed:
        raise typer.Exit(code=1)


@app.command("qbo-status")
def qbo_status_cmd(
    property_id: str | None = typer.Option(None, "--property", help="Limit to one property"),
    month: str | None = typer.Option(None, "--month", help="Limit to one month, YYYY-MM"),
) -> None:
    """Show the QBO push ledger: what was pushed, failed, or went stale."""
    with _session_factory()() as s:
        try:
            rows = qbo_push.push_ledger_rows(s, property_id=property_id, month=month)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if not rows:
        typer.echo("no QBO pushes recorded")
        return
    typer.echo(
        f"{'property':<10} {'date':<12} {'status':<15} {'je_id':<8} "
        f"{'pushed_at':<26} message"
    )
    for r in rows:
        typer.echo(
            f"{r.property_id:<10} {r.business_date.isoformat():<12} {r.status:<15} "
            f"{(r.qbo_je_id or '-'):<8} {r.pushed_at.isoformat():<26} {r.message or ''}".rstrip()
        )


@app.command("qbo-mock")
def qbo_mock_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind address (localhost only; mock server)"),
    port: int = typer.Option(9200, help="Port"),
) -> None:
    """Run the mock QuickBooks Online server (in-memory; state resets on restart)."""
    import uvicorn

    from usali.qbo_mock import create_mock_qbo

    uvicorn.run(create_mock_qbo(), host=host, port=port)


@app.command("gusto-mock")
def gusto_mock_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind address (localhost only; mock server)"),
    port: int = typer.Option(9300, help="Port"),
) -> None:
    """Run the local Gusto-shaped payroll mock (dev only; synthetic data only)."""
    import uvicorn

    from usali.gusto_mock import create_mock_gusto

    uvicorn.run(create_mock_gusto(), host=host, port=port)


@app.command("adp-mock")
def adp_mock_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind address (localhost only; mock server)"),
    port: int = typer.Option(9301, help="Port"),
) -> None:
    """Run the local ADP-shaped payroll mock (dev only; synthetic data only)."""
    import uvicorn

    from usali.adp_mock import create_mock_adp

    uvicorn.run(create_mock_adp(), host=host, port=port)


@app.command("delphi-mock")
def delphi_mock_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind address (localhost only; mock server)"),
    port: int = typer.Option(9400, help="Port"),
) -> None:
    """Run the local Delphi-shaped sales-CRM mock (dev only; synthetic data only)."""
    import uvicorn

    from usali.delphi_mock import create_mock_delphi

    uvicorn.run(create_mock_delphi(), host=host, port=port)


@app.command("tripleseat-mock")
def tripleseat_mock_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind address (localhost only; mock server)"),
    port: int = typer.Option(9401, help="Port"),
) -> None:
    """Run the local Tripleseat-shaped events-CRM mock (dev only; synthetic data only)."""
    import uvicorn

    from usali.tripleseat_mock import create_mock_tripleseat

    uvicorn.run(create_mock_tripleseat(), host=host, port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
