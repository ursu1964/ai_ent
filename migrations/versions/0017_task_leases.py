"""Task leases.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-09 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_executions_task_attempt",
        "executions",
        ["task_id", "attempt"],
    )
    op.create_table(
        "task_leases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("execution_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('active', 'released', 'completed', 'expired')",
            name="ck_task_leases_status",
        ),
        sa.CheckConstraint("expires_at > acquired_at", name="ck_task_leases_expires_after_acquire"),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id"),
    )
    op.create_index(
        "uq_task_leases_one_active_per_task",
        "task_leases",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_task_leases_task_status", "task_leases", ["task_id", "status"])
    op.create_index("ix_task_leases_owner_status", "task_leases", ["owner_id", "status"])
    op.create_index("ix_task_leases_expires_at", "task_leases", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_task_leases_expires_at", table_name="task_leases")
    op.drop_index("ix_task_leases_owner_status", table_name="task_leases")
    op.drop_index("ix_task_leases_task_status", table_name="task_leases")
    op.drop_index("uq_task_leases_one_active_per_task", table_name="task_leases")
    op.drop_table("task_leases")
    op.drop_constraint("uq_executions_task_attempt", "executions", type_="unique")
