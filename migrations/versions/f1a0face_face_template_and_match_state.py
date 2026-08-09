"""F1: the face-template table and the punch match columns.

`employee_face_template` is created EMPTY — enrollment (F3) is the only
writer, and it records which notice-at-collection version the employee was
shown (the pair CHECK refuses half a compliance fact). The embedding column
holds AES-GCM ciphertext (EncryptedBytes): a breach of unencrypted biometric
data is CA's one private-right-of-action scenario (Civ. Code §1798.150).

`punch.match_state`/`match_score` are nullable with NO backfill: every
existing punch predates matching, and NULL ("recorded before/without
matching") is deliberately distinct from 'no_template' ("matching ran,
found nobody enrolled"). The CHECKs are the approval gate's contract (F6):
'verified' cannot exist without a score, a score cannot float free of a
state, and the vocabulary is closed.
"""

from alembic import op
import sqlalchemy as sa

revision = "f1a0face"
down_revision = "e4a0sick"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "employee_face_template",
        sa.Column("template_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(),
                  sa.ForeignKey("employee.employee_id"), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("model_version", sa.String(50), nullable=False),
        sa.Column("notice_version", sa.String(50), nullable=True),
        sa.Column("notified_on", sa.Date(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # One row per employee: re-enrollment REPLACES, termination DELETES.
        sa.UniqueConstraint("employee_id"),
        sa.CheckConstraint(
            "(notice_version IS NULL) = (notified_on IS NULL)",
            name="ck_face_template_notice_pair",
        ),
    )

    op.add_column("punch", sa.Column("match_state", sa.String(12), nullable=True))
    op.add_column("punch", sa.Column("match_score", sa.Numeric(4, 3), nullable=True))
    op.create_check_constraint(
        "ck_punch_match_state", "punch",
        "match_state IN ('verified', 'unverified', 'no_template')",
    )
    op.create_check_constraint(
        "ck_punch_verified_has_score", "punch",
        "match_state IS DISTINCT FROM 'verified' OR match_score IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_punch_score_has_state", "punch",
        "match_score IS NULL OR match_state IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_punch_score_range", "punch",
        "match_score IS NULL OR (match_score >= 0 AND match_score <= 1)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_punch_score_range", "punch", type_="check")
    op.drop_constraint("ck_punch_score_has_state", "punch", type_="check")
    op.drop_constraint("ck_punch_verified_has_score", "punch", type_="check")
    op.drop_constraint("ck_punch_match_state", "punch", type_="check")
    op.drop_column("punch", "match_score")
    op.drop_column("punch", "match_state")
    # Drops enrolled templates — biometric data whose deletion is the SAFE
    # direction (unlike the sick-leave downgrade). Re-enrollment rebuilds.
    op.drop_table("employee_face_template")
