"""L1 (Pillar L decision 1): the org_id denormalization — shape-level pins.

The POPULATED-world pins (backfill lands org 1 on every table, the
FK-bypass refusals, downgrade carrying rows through) live in
`test_migration_on_populated_data.py` beside every other backfill pin.
This module pins what needs no data:

- exactly ONE alembic head, and it is l1a0orgid — the denormalization
  must not fork the chain (the single-head verification is what makes
  "the deployed schema" a meaningful phrase);
- the empty-database round trip BOTH directions, with the FULL
  constraint inventory asserted structurally. The behavioral refusal
  tests sample a handful of composite FKs; this asserts every one of
  them EXISTS, because a mutant deleting a single entry from the
  migration's tuples would otherwise fail nothing. The tuples are
  imported from the migration module itself (no transcription drift)
  and their SIZES are pinned as literals — the standing differencing-
  oracle rule: an expectation derived from the code under test shrinks
  with it, so the counts must not.
"""

import importlib.util
import os

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from testcontainers.postgres import PostgresContainer  # noqa: E402

from usali.models import Base  # noqa: E402

from tests.orgwall import ensure_app_role  # noqa: E402

_PRE_L1 = "j3a0crmref"

# Pre-L1 model/migration drift, known and bounded: indexes that exist only
# in the migration chain (e1/e2/e4/j2 created them; the models never
# declared them) plus one nullable disagreement. Enumerated so the parity
# pin below fails on any NEW drift while not relitigating history — retiring
# these is a cleanup for a later task, recorded here rather than hidden.
_KNOWN_PRE_L1_DRIFT_INDEXES = {
    "ix_assignment_rate_lookup",
    "uq_one_open_rate_per_assignment_type",
    "ix_crm_demand_snapshot_stay_date",
    "ix_crm_pull_batch_property",
    "ix_employee_assignment_employee",
    "ix_employee_assignment_property",
    "uq_one_active_primary_per_employee",
    "ix_pay_run_line_property_run_property",
    "ix_sick_leave_ledger_employee",
}
_KNOWN_PRE_L1_DRIFT_NULLABLE = {("employee_assignment", "created_at")}

_spec = importlib.util.spec_from_file_location(
    "l1a0orgid", "migrations/versions/l1a0orgid_org_id_denormalization.py"
)
assert _spec is not None and _spec.loader is not None
_l1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_l1)


def _cfg(url: str) -> Config:
    # env.py reads USALI_DB_URL (see test_migration_on_populated_data._cfg;
    # the caller restores the previous value).
    os.environ["USALI_DB_URL"] = url
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_l1_sits_on_the_single_alembic_chain():
    """One head, with l1a0orgid an ancestor of it. A second head means
    two deployments can honestly claim 'at head' while holding different
    schemas — the drift class the whole migration discipline exists to
    prevent. (The exact-head pin lives with the newest migration's own
    module — test_l2_rls_wall since L2.)"""
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert "l1a0orgid" in {rev.revision for rev in script.walk_revisions()}


def test_l1_judgment_extent_is_pinned():
    """The literal sizes of the migration's judgment: 42 tables gain the
    column, 8 parents grow the composite-FK unique, 33 FKs go composite.
    Shrinking any tuple (deleting a table, a unique, or a composite FK)
    must fail HERE even though the structural test below imports the same
    tuples — that import is exactly the differencing oracle these literals
    exist to break."""
    assert len(_l1._ORG_TABLES) == 42
    assert len(_l1._PARENT_UNIQUES) == 8
    assert len(_l1._COMPOSITE_FKS) == 33


def test_l1_round_trips_on_an_empty_database():
    """Up through l1a0orgid, down, and up again on a fresh database, with
    the FULL constraint inventory asserted at head and the ORIGINAL FK
    names asserted after downgrade (older downgrades drop them BY NAME; a
    renamed restore would strand any deeper rollback). Every backfill
    statement is a no-op on empty tables, so this proves only DDL sanity —
    which is exactly why the populated module exists — but a downgrade
    that cannot run on an empty database cannot run anywhere."""
    previous = os.environ.get("USALI_DB_URL")
    try:
        with PostgresContainer("postgres:16", driver="psycopg") as pg:
            url = pg.get_connection_url()
            # l2a0rlswall (upstream of head) refuses without the app role.
            ensure_app_role(url)
            command.upgrade(_cfg(url), "head")
            engine = create_engine(url)

            def constraint_names() -> set[str]:
                with engine.begin() as conn:
                    return {
                        r[0] for r in conn.execute(text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE contype IN ('f', 'u')"
                        ))
                    }

            def org_indexes() -> set[str]:
                with engine.begin() as conn:
                    return {
                        r[0] for r in conn.execute(text(
                            "SELECT indexname FROM pg_indexes "
                            "WHERE schemaname = 'public' "
                            "AND indexname LIKE 'ix\\_%\\_org\\_id'"
                        ))
                    }

            def org_id_tables() -> set[str]:
                with engine.begin() as conn:
                    return {
                        r[0] for r in conn.execute(text(
                            "SELECT table_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' "
                            "AND column_name = 'org_id'"
                        ))
                    }

            # --- at head: the full inventory, every name, no sampling ----
            at_head = constraint_names()
            composite_new = {new for _, new, *_ in _l1._COMPOSITE_FKS}
            composite_old = {old for old, *_ in _l1._COMPOSITE_FKS}
            parent_uniques = {name for name, _, _ in _l1._PARENT_UNIQUES}
            org_fks = {f"fk_{t}_org" for t in _l1._ORG_TABLES} | {
                "fk_property_org"  # renamed from property_org_id_fkey
            }
            expected_at_head = (
                composite_new
                | parent_uniques
                | org_fks
                | {"uq_organization_kc_org_alias"}
            )
            assert expected_at_head <= at_head, (
                f"missing at head: {sorted(expected_at_head - at_head)}"
            )
            assert not (composite_old & at_head), (
                "a replaced single-column FK survived alongside its "
                f"composite: {sorted(composite_old & at_head)}"
            )
            assert org_indexes() == {
                f"ix_{t}_org_id" for t in _l1._ORG_TABLES
            } | {
                "ix_property_org_id",
                # m1a0propcfg (#8): three more org-scoped tables, each with
                # its own org_id index (unlike org_settings, whose org_id is
                # the primary key and so carries no separate ix_ index).
                "ix_room_inventory_org_id",
                "ix_out_of_order_room_org_id",
                "ix_fiscal_calendar_org_id",
                # m2a0perffoundations (#9): two more org-scoped tables, each
                # with its own org_id index.
                "ix_property_stat_config_org_id",
                "ix_ingestion_coverage_org_id",
            }
            assert set(_l1._ORG_TABLES) | {"property"} <= org_id_tables()

            # --- schema parity: the ORM and the migrated schema are the
            # same schema. Autogenerate's diff must be empty modulo the
            # ENUMERATED pre-L1 drift — any new divergence (a mixin column
            # the migration missed, a constraint declared once) fails here
            # mechanically instead of surfacing as 'same revision, two
            # schemas' in production.
            with engine.connect() as conn:
                diffs = compare_metadata(
                    MigrationContext.configure(conn), Base.metadata
                )
            unexpected = []
            for entry in diffs:
                d = entry[0] if isinstance(entry, list) else entry
                if (
                    d[0] == "remove_index"
                    and d[1].name in _KNOWN_PRE_L1_DRIFT_INDEXES
                ):
                    continue
                if (
                    d[0] == "modify_nullable"
                    and (d[2], d[3]) in _KNOWN_PRE_L1_DRIFT_NULLABLE
                ):
                    continue
                unexpected.append(d)
            assert unexpected == [], (
                "models and migrations disagree beyond the enumerated "
                f"pre-L1 drift: {unexpected}"
            )

            # --- downgrade: originals back, additions gone ---------------
            command.downgrade(_cfg(url), _PRE_L1)
            after_down = constraint_names()
            assert composite_old <= after_down, (
                "downgrade must restore the single FKs under their ORIGINAL "
                f"names: missing {sorted(composite_old - after_down)}"
            )
            assert "property_org_id_fkey" in after_down
            gone = (
                composite_new
                | parent_uniques
                | org_fks
                | {"uq_organization_kc_org_alias"}
            )
            assert not (gone & after_down), (
                f"L1 constraints survived the downgrade: "
                f"{sorted(gone & after_down)}"
            )
            assert org_indexes() == set()
            assert org_id_tables() == {"organization", "property"}, (
                "the pre-L1 world: only property carries org_id"
            )

            # --- and up again --------------------------------------------
            command.upgrade(_cfg(url), "head")
            assert constraint_names() == at_head
            engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous
