from sqlalchemy import select

from usali.mapping.schedules import seed_schedules
from usali.models import UsaliSchedule


def test_seed_schedules_loads_16_per_edition(db_session):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    db_session.commit()
    rows = db_session.execute(
        select(UsaliSchedule).where(UsaliSchedule.usali_edition == 12)
    ).scalars().all()
    assert len(rows) == 16
    assert any(r.schedule_id == 1 and r.name == "Rooms" for r in rows)


def test_seed_schedules_is_idempotent(db_session):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    db_session.commit()
    rows = db_session.execute(select(UsaliSchedule)).scalars().all()
    assert len(rows) == len({(r.usali_edition, r.schedule_id) for r in rows})
