"""Load the curated PMS -> USALI mapping dictionary from YAML into the DB."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from usali.models import UsaliMappingDictionary


class MappingEntry(BaseModel):
    source: str
    code: str
    edition: int
    schedule_id: int | None = None
    major: str
    sub: str
    line_item: str
    gl_account_code: str | None = None
    confidence: str = "MEDIUM"
    review_status: str = "unreviewed"
    notes: str | None = None


def load_mappings(session: Session, yaml_path: str | Path) -> int:
    """Upsert curated mapping rows from a YAML file into usali_mapping_dictionary.

    Idempotent: re-running with the same file updates existing rows (keyed on
    pms_source + pms_trx_code + usali_edition) rather than duplicating them.

    Returns the number of rows processed.
    """
    raw: list[dict[str, Any]] = yaml.safe_load(Path(yaml_path).read_text())
    entries = [MappingEntry(**row) for row in raw]
    for e in entries:
        stmt = insert(UsaliMappingDictionary).values(
            pms_source=e.source,
            pms_trx_code=e.code,
            usali_edition=e.edition,
            usali_schedule_id=e.schedule_id,
            usali_major_category=e.major,
            usali_sub_category=e.sub,
            usali_line_item=e.line_item,
            gl_account_code=e.gl_account_code,
            confidence=e.confidence,
            review_status=e.review_status,
            notes=e.notes,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["pms_source", "pms_trx_code", "usali_edition"],
            set_={
                "usali_schedule_id": e.schedule_id,
                "usali_major_category": e.major,
                "usali_sub_category": e.sub,
                "usali_line_item": e.line_item,
                "gl_account_code": e.gl_account_code,
                "confidence": e.confidence,
                "review_status": e.review_status,
                "notes": e.notes,
            },
        )
        session.execute(stmt)
    return len(entries)
