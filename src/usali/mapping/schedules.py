"""Seed the usali_schedule reference table from curated YAML data."""

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from usali.models import UsaliSchedule


def seed_schedules(session: Session, yaml_path: str | Path) -> int:
    """Upsert USALI schedule rows from a YAML file into usali_schedule.

    Idempotent: re-running with the same file updates existing rows
    (keyed on usali_edition + schedule_id) rather than duplicating them.

    Returns the number of rows processed.
    """
    rows: list[dict[str, Any]] = yaml.safe_load(Path(yaml_path).read_text())
    for row in rows:
        stmt = insert(UsaliSchedule).values(
            usali_edition=row["edition"],
            schedule_id=row["schedule_id"],
            name=row["name"],
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["usali_edition", "schedule_id"],
            set_={"name": row["name"]},
        )
        session.execute(stmt)
    return len(rows)
