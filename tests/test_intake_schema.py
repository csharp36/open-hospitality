"""OH-23 Task 1 review (I4): the three constraints o1a0intake adds beyond the
FKs have semantics that compare_metadata does not compare. The reviewer
inverted the partial unique's WHERE to `revoked_at IS NOT NULL` in the
migration only, and separately deleted both CHECKs from the migration only;
tests/test_l1_org_wall_migration.py passed both times. Each is pinned here
on a migrated database: the catalog text against the model's own
declaration, and the refusal the constraint exists to produce.

`test_intake_outcomes_match_the_check_constraint` is the third leg: the CHECK
is also duplicated as `intake.INTAKE_OUTCOMES`, which no schema comparison
reaches either. Every other pin here reads its outcome set from the model
alone.
"""

import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError

from usali import intake
from usali.models import EmailIntakeEvent, Organization, Property, PropertyIntakeAddress

_ACTIVE_INDEX = "uq_property_intake_address_active"
_LOWER_CHECK = "ck_property_intake_address_local_part_lower"
_OUTCOME_CHECK = "ck_email_intake_event_outcome"


def _property(session, pid="HISJ"):
    session.merge(Organization(org_id=1, name="Org"))
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def _address(pid="HISJ", local_part="na-abc", revoked_at=None):
    return PropertyIntakeAddress(
        local_part=local_part, org_id=1, property_id=pid, revoked_at=revoked_at
    )


def _model_check(table, name: str) -> str:
    """The CheckConstraint sqltext as the model declares it, by name — so the
    pin is model<->DB, never a third literal."""
    found = [
        c for c in table.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == name
    ]
    assert len(found) == 1, f"{name} not declared exactly once on {table.__tablename__}"
    return str(found[0].sqltext)


def _normalize(sql: str) -> str:
    """Postgres rewrites a CHECK on the way in: wraps it in CHECK(...), casts
    every varchar to ::text, spells IN as = ANY (ARRAY[...]), and adds
    parentheses. Undo those, and only those, so the model's text and
    pg_get_constraintdef's compare by content."""
    s = sql.strip()
    s = re.sub(r"^CHECK\s*", "", s)
    s = re.sub(r"::(?:character varying|text|varchar)(?:\[\])?", "", s)
    s = re.sub(r"=\s*ANY\s*\(+\s*ARRAY\s*\[", "IN (", s)
    s = s.replace("]", "")
    s = re.sub(r"[()]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # `IN` needs its list re-parenthesised for a like-for-like text compare.
    s = re.sub(r"\bIN ('.*)$", r"IN (\1)", s)
    return s


def _db_check(session, name: str) -> str:
    ddl = session.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
        {"n": name},
    ).scalar_one_or_none()
    assert ddl is not None, f"CHECK {name} is not in the migrated database"
    return ddl


# --- catalog pins -------------------------------------------------------------


def test_the_active_address_index_is_a_partial_unique_on_un_revoked_rows(db_session):
    indexdef = db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
        {"n": _ACTIVE_INDEX},
    ).scalar_one_or_none()
    assert indexdef is not None, f"{_ACTIVE_INDEX} is not in the migrated database"
    assert indexdef.startswith("CREATE UNIQUE INDEX"), indexdef
    assert "(org_id, property_id)" in indexdef, indexdef
    assert indexdef.endswith("WHERE (revoked_at IS NULL)"), indexdef


def test_intake_outcomes_match_the_check_constraint():
    """`intake.INTAKE_OUTCOMES` is the third copy of D-OH23.7's closed outcome
    set (the model's CHECK and o1a0intake's `_OUTCOMES` are the other two).
    Parse the values back out of the model's own declaration so this compares
    the set the code branches on against the set the database will accept,
    with no third literal in between."""
    declared = set(re.findall(r"'([a-z_]+)'", _model_check(EmailIntakeEvent, _OUTCOME_CHECK)))
    assert declared, "no quoted outcomes found in the model's CHECK"
    assert declared == set(intake.INTAKE_OUTCOMES)


@pytest.mark.parametrize(
    ("model", "name"),
    [(PropertyIntakeAddress, _LOWER_CHECK), (EmailIntakeEvent, _OUTCOME_CHECK)],
    ids=[_LOWER_CHECK, _OUTCOME_CHECK],
)
def test_each_check_matches_the_models_declaration(db_session, model, name):
    assert _normalize(_db_check(db_session, name)) == _normalize(_model_check(model, name))


# --- behavior ----------------------------------------------------------------


def test_two_un_revoked_addresses_for_one_property_are_refused(db_session):
    _property(db_session)
    db_session.add(_address(local_part="na-first"))
    db_session.flush()
    db_session.add(_address(local_part="na-second"))
    with pytest.raises(IntegrityError) as excinfo:
        db_session.flush()
    assert _ACTIVE_INDEX in str(excinfo.value)
    db_session.rollback()


def test_a_revoked_address_and_an_active_one_may_coexist(db_session):
    _property(db_session)
    db_session.add(_address(local_part="na-old", revoked_at=datetime.now(timezone.utc)))
    db_session.flush()
    db_session.add(_address(local_part="na-new"))
    db_session.flush()
    assert db_session.query(PropertyIntakeAddress).count() == 2


def test_an_uppercase_local_part_is_refused(db_session):
    _property(db_session)
    db_session.add(_address(local_part="NA-Shouty"))
    with pytest.raises(IntegrityError) as excinfo:
        db_session.flush()
    assert _LOWER_CHECK in str(excinfo.value)
    db_session.rollback()


def test_an_unknown_outcome_is_refused(db_session):
    _property(db_session)
    db_session.add(
        EmailIntakeEvent(
            org_id=1, address_id=1, property_id="HISJ",
            envelope_from="gm@example.com", outcome="nonsense",
        )
    )
    with pytest.raises(IntegrityError) as excinfo:
        db_session.flush()
    assert _OUTCOME_CHECK in str(excinfo.value)
    db_session.rollback()
