"""workforce model tables

Revision ID: ece60731aa05
Revises: 0da53c099d83
Create Date: 2026-07-13 22:36:50.079412

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ece60731aa05'
down_revision: Union[str, Sequence[str], None] = '0da53c099d83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "department",
        sa.Column("department_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("usali_schedule_id", sa.Integer(), nullable=True),
        sa.Column("usali_edition", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["property_id"], ["property.property_id"]),
        sa.PrimaryKeyConstraint("department_id"),
        sa.UniqueConstraint("property_id", "name"),
    )
    op.create_table(
        "position",
        sa.Column("position_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("flsa_exempt", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["department_id"], ["department.department_id"]),
        sa.PrimaryKeyConstraint("position_id"),
    )
    op.create_table(
        "employee",
        sa.Column("employee_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("keycloak_subject", sa.String(length=64), nullable=True),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("pay_type", sa.String(length=10), nullable=False),
        sa.Column("hire_date", sa.Date(), nullable=True),
        sa.Column("termination_date", sa.Date(), nullable=True),
        sa.Column("manager_employee_id", sa.Integer(), nullable=True),
        sa.Column("compensation_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["property_id"], ["property.property_id"]),
        sa.ForeignKeyConstraint(["department_id"], ["department.department_id"]),
        sa.ForeignKeyConstraint(["position_id"], ["position.position_id"]),
        sa.ForeignKeyConstraint(["manager_employee_id"], ["employee.employee_id"]),
        sa.PrimaryKeyConstraint("employee_id"),
        sa.UniqueConstraint("keycloak_subject"),
    )
    op.create_table(
        "role_assignment",
        sa.Column("assignment_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("keycloak_subject", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["property_id"], ["property.property_id"]),
        sa.ForeignKeyConstraint(["department_id"], ["department.department_id"]),
        sa.PrimaryKeyConstraint("assignment_id"),
        sa.UniqueConstraint("keycloak_subject", "role", "property_id", "department_id"),
    )
    op.create_table(
        "audit_event",
        sa.Column("event_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_subject", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )


def downgrade() -> None:
    op.drop_table("audit_event")
    op.drop_table("role_assignment")
    op.drop_table("employee")
    op.drop_table("position")
    op.drop_table("department")
