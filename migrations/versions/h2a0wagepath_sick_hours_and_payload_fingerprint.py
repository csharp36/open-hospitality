"""H2: the run stores what it paid; the ref fingerprints what it shipped.

`pay_run_line.sick_hours` (decision 5): the statutory sick hours THIS run
paid for this employee, frozen at execute time. Until now "what did this
run pay as sick" was a re-derivation over a ledger that keeps moving after
submission — the whole G7 timestamp-race class. Stored, it becomes a fact
the §246(n) guard (H6) and the apportionment split (H5) can read without a
clock anywhere. Backfill 0 IS the truth, not a placeholder: pre-G3 runs
priced no sick at all, and G3-to-H runs recorded none per line — 0 is what
the pre-H schema can honestly say either way. The server default STAYS
after the backfill: H2 ships before H5 writes the column, so an insert
that omits it must land 0 through that window, and 0 remains the honest
value for a run that recorded no sick. The CHECK refuses a negative — a
negative would let a void REDUCE what a run claims it paid, flipping the
H6 comparison direction.

`provider_employee_ref.payload_fingerprint` (decision 8): SHA-256 hex over
exactly the fields the sync payload ships (ciphertext, never plaintext).
NO data backfill, deliberately: computing one here means hashing sealed
envelopes this migration has no business enumerating, and a WRONG stored
fingerprint reads "not stale" — a silently skipped re-send of edited
deposit data. NULL reads STALE instead; the first sync re-sends once and
self-heals every pre-H ref. Fail toward the extra send, never the skipped
one.

Downgrade is lossless because both columns are re-derivable: 0 is the
recorded pre-H truth, NULL self-heals through one re-send.
"""

from alembic import op
import sqlalchemy as sa

revision = "h2a0wagepath"
down_revision = "f1a0face"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pay_run_line",
        sa.Column("sick_hours", sa.Numeric(10, 2), nullable=False,
                  server_default=sa.text("0")),
    )
    op.create_check_constraint(
        "ck_pay_run_line_sick_hours_nonneg", "pay_run_line",
        "sick_hours >= 0",
    )
    op.add_column(
        "provider_employee_ref",
        sa.Column("payload_fingerprint", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_employee_ref", "payload_fingerprint")
    op.drop_constraint("ck_pay_run_line_sick_hours_nonneg", "pay_run_line")
    op.drop_column("pay_run_line", "sick_hours")
