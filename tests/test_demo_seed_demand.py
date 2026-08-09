"""J6: the demo world pulls the demand story — and it actually renders.

The seed makes ONE audited pull through the REAL Delphi adapter over the
in-process mock (the demo stack's exact path, the G6 posture): the fixed
mock world lands as a batch — the fat Thursday block on 2026-08-06 the
talking point — and the scheduler surfaces read it. A seed change that
breaks the demo breaks the suite, not the demo.

Posture pins: the talking points print FIGURES and dropped-field NAMES
only — block/event labels render on the scheduler page and never on
stdout (stdout is terminal scrollback and CI logs, the roster-import
lesson)."""

import importlib.util
from datetime import date
from pathlib import Path

from sqlalchemy import select

from usali.crm_pull import latest_demand
from usali.delphi_adapter import DelphiAdapter
from usali.delphi_mock import create_mock_delphi
from usali.gusto_adapter import GustoAdapter
from usali.gusto_mock import create_mock_gusto
from usali.models import AuditEvent, CrmDemandSnapshot, CrmPullBatch
from usali.opener import SoftwareOpener
from usali.qbo_client import SyncASGITransport

_SCRIPT = Path(__file__).parent.parent / "scripts" / "demo_seed.py"
_spec = importlib.util.spec_from_file_location("demo_seed_demand", _SCRIPT)
assert _spec is not None and _spec.loader is not None
demo_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_seed)


def _worker(ref, placements, pay_type="hourly"):
    return demo_seed.DemoWorker(
        ref=ref, full_name=f"Demo Person {ref}", pay_type=pay_type,
        placements=placements,
    )


_ROSTER = [
    _worker(10, (("HISJ", "Housekeeping"),)),
    _worker(11, (("HISJ", "Housekeeping"),)),
    _worker(12, (("HISJ", "Housekeeping"),)),
    _worker(13, (("HISJ", "Front Desk"),)),
    _worker(20, (("SSSJ", "Front Desk"),)),
    _worker(21, (("SSSJ", "Breakfast"),)),
    _worker(22, (("SSSJ", "Laundry"),)),
]


def _gusto():
    return GustoAdapter(base_url="http://mock-gusto", api_token="mock",
                        company_id="mock",
                        transport=SyncASGITransport(create_mock_gusto()))


def _delphi():
    return DelphiAdapter(
        base_url="http://mock-delphi", subscription_key="mock",
        transport=SyncASGITransport(create_mock_delphi()),
    )


def _seed(db_session, monkeypatch, tmp_path, *, feed=_delphi):
    opener = SoftwareOpener.generate(key_id="demo-demand-test")
    monkeypatch.setattr(demo_seed.SoftwareOpener, "from_settings",
                        classmethod(lambda cls, settings: opener))
    monkeypatch.setattr(demo_seed, "_payroll_provider", _gusto)
    # The J4 seam: the :9400 dev mock in the demo stack, in-process here.
    monkeypatch.setattr(demo_seed, "_crm_feed", feed)
    monkeypatch.setenv("USALI_CRM_PROVIDER", "delphi")
    demo_seed._seed_base(db_session)
    demo_seed._seed_people(db_session, _ROSTER)
    monkeypatch.setattr(demo_seed, "REPO_ROOT", tmp_path)
    notes, _ = demo_seed._seed_world(db_session, _ROSTER)
    return notes


def test_the_seed_pulls_the_fixed_demand_world(
    db_session, monkeypatch, tmp_path
):
    """One audited batch for HISJ (the crm_ref property), through the real
    adapter: the fat Thursday carries the merged blocks, labels land in
    the snapshot (the scheduler surface will show them), and the pull is
    audited exactly like an operator's refresh."""
    _seed(db_session, monkeypatch, tmp_path)

    batch = db_session.execute(select(CrmPullBatch)).scalar_one()
    assert batch.property_id == "HISJ"
    assert batch.provider == "delphi"

    thursday = db_session.execute(
        select(CrmDemandSnapshot)
        .where(CrmDemandSnapshot.stay_date == date(2026, 8, 6))
    ).scalar_one()
    assert thursday.rooms_on_books == 132
    assert thursday.group_rooms == 50  # Acme 40 + Delta Sigma 10
    assert thursday.labels == "Acme Corp Annual, Delta Sigma Reunion"
    assert thursday.event_covers is None  # Delphi speaks no covers

    audit = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "crm_refresh")
    ).scalar_one()
    assert audit.actor_subject == "demo-seed"
    assert audit.resource_id == str(batch.batch_id)

    # What the GM's surface will read for the demand week.
    rows = latest_demand(db_session, "HISJ",
                         date(2026, 8, 3), date(2026, 8, 9))
    assert [r.stay_date for r in rows] == [
        date(2026, 8, n) for n in range(3, 10)
    ]


def test_the_talking_points_name_figures_never_labels(
    db_session, monkeypatch, tmp_path
):
    """The demand note names the fat Thursday by FIGURES and the dropped
    fields by NAME; block labels stay off stdout — they belong to the
    scheduler page (terminal scrollback is a log)."""
    notes = _seed(db_session, monkeypatch, tmp_path)
    (demand_note,) = [n for n in notes if "CRM demand" in n]
    assert "2026-08-06" in demand_note
    assert "132" in demand_note and "50" in demand_note
    assert "Contact" in demand_note and "AverageRate" in demand_note
    assert "Acme" not in demand_note
    assert "Delta Sigma" not in demand_note


def test_feature_off_skips_the_demand_story_loudly(
    db_session, monkeypatch, tmp_path
):
    """No configured feed: the seed says so in the talking points and
    writes nothing — never a half-seeded demand world."""
    notes = _seed(db_session, monkeypatch, tmp_path, feed=lambda: None)
    assert any("USALI_CRM_PROVIDER" in n for n in notes)
    assert db_session.execute(select(CrmPullBatch)).scalars().all() == []


