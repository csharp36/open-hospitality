"""J2: the CRM demand tables — pull batches + append-only snapshots, empty.

`crm_pull_batch` is the as-of identity of one explicit, audited pull
(plan decision 6): property, provider, horizon, stamp. `property_id`
lives ONLY here — a snapshot's property is its batch's property, one
copy, never two that can disagree.

`crm_demand_snapshot` is append-only BY CONSTRUCTION: booking pace is a
comparison of snapshots over time, so the only unique is
(batch_id, stay_date) — one voice per batch per day — and deliberately
nothing wider: a re-pull is a NEW batch carrying the same stay dates,
and any constraint forbidding that would destroy the data pace is
computed from. Figures are nullable (NULL = the provider does not speak
that dimension — never zero, the D2 forecast-null rule) with
non-negative CHECKs per figure; labels are the bounded scheduler-only
display string.

Created EMPTY: no pulls exist historically — the mechanism is new with
this pillar — and inventing demand history would fabricate exactly the
pace data the append-only design exists to keep honest.

Downgrade drops both tables and any pulled history with them. That is
the honest pre-J shape and it silences nothing: demand is
decision-support data — no guard, no money path, and no report reads it
at this revision (surfaces arrive in J5 and degrade to "no demand data"
when the tables are gone with the code rolled back; code without the
tables fails its pull/read loudly).
"""

from alembic import op
import sqlalchemy as sa

revision = "j2a0crmdemand"
down_revision = "i2a0settle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crm_pull_batch",
        sa.Column("batch_id", sa.Integer, primary_key=True,
                  autoincrement=True),
        sa.Column("property_id", sa.String(length=50),
                  sa.ForeignKey("property.property_id"), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("horizon_start", sa.Date(), nullable=False),
        sa.Column("horizon_end", sa.Date(), nullable=False),
        sa.Column("pulled_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("horizon_end >= horizon_start",
                           name="ck_crm_pull_batch_horizon"),
    )
    # The pull/read path resolves batches by property; without this it is
    # a full scan of every batch ever pulled.
    op.create_index("ix_crm_pull_batch_property", "crm_pull_batch",
                    ["property_id"])

    op.create_table(
        "crm_demand_snapshot",
        sa.Column("snapshot_id", sa.BigInteger, primary_key=True,
                  autoincrement=True),
        sa.Column("batch_id", sa.Integer,
                  sa.ForeignKey("crm_pull_batch.batch_id"), nullable=False),
        sa.Column("stay_date", sa.Date(), nullable=False),
        sa.Column("rooms_on_books", sa.Integer, nullable=True),
        sa.Column("group_rooms", sa.Integer, nullable=True),
        sa.Column("event_covers", sa.Integer, nullable=True),
        sa.Column("labels", sa.String(length=300), nullable=False),
        sa.UniqueConstraint("batch_id", "stay_date"),
        sa.CheckConstraint("rooms_on_books >= 0",
                           name="ck_crm_demand_rooms_on_books_nonneg"),
        sa.CheckConstraint("group_rooms >= 0",
                           name="ck_crm_demand_group_rooms_nonneg"),
        sa.CheckConstraint("event_covers >= 0",
                           name="ck_crm_demand_event_covers_nonneg"),
    )
    # The J5 surfaces read a stay-date window across batches.
    op.create_index("ix_crm_demand_snapshot_stay_date",
                    "crm_demand_snapshot", ["stay_date"])


def downgrade() -> None:
    op.drop_index("ix_crm_demand_snapshot_stay_date",
                  table_name="crm_demand_snapshot")
    op.drop_table("crm_demand_snapshot")
    op.drop_index("ix_crm_pull_batch_property", table_name="crm_pull_batch")
    op.drop_table("crm_pull_batch")
