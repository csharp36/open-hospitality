"""The demo seed's document step ingests every committed sample, the pack
included (issue #138). Runs the real scripts/demo_seed.py functions against
the real docs/reference/samples directory."""

import hashlib
import importlib.util
from pathlib import Path

from sqlalchemy import func, select

from usali.models import IngestBatch, MappingException, UsaliFinancialFact

_SCRIPT = Path(__file__).parent.parent / "scripts" / "demo_seed.py"
_spec = importlib.util.spec_from_file_location("demo_seed_documents", _SCRIPT)
assert _spec is not None and _spec.loader is not None
demo_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_seed)

SAMPLES = Path("docs/reference/samples")
PACK = SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_seed_documents_ingests_every_sample_including_the_pack(db_session):
    demo_seed._seed_base(db_session)
    demo_seed._seed_documents(db_session)

    rows = db_session.execute(
        select(IngestBatch.file_hash, IngestBatch.status, IngestBatch.pms_source)
    ).all()
    assert rows and {status for _, status, _ in rows} == {"transformed"}
    assert {h for h, _, _ in rows} == {_sha(p) for p in SAMPLES.glob("*.pdf")}
    pack_rows = [src for h, _, src in rows if h == _sha(PACK)]
    assert pack_rows == ["SKYTOUCH", "SKYTOUCH"]  # journal + statistics sections
    # _seed_base loads every sample's dictionary: nothing falls through to the
    # exception worklist, and the pack's journal rows become facts.
    assert db_session.scalar(select(func.count()).select_from(MappingException)) == 0
    assert db_session.scalar(
        select(func.count()).select_from(UsaliFinancialFact)
        .where(UsaliFinancialFact.pms_source == "SKYTOUCH")
    ) > 0

    before = db_session.scalar(select(func.count()).select_from(IngestBatch))
    demo_seed._seed_documents(db_session)  # per-file idempotent
    assert db_session.scalar(select(func.count()).select_from(IngestBatch)) == before
