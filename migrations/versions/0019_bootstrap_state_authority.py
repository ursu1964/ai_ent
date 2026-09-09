"""Bootstrap state authority marker.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-09 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bootstrap_state_authority",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("backend", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_snapshot_path", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("backend in ('local_json', 'postgresql')", name="ck_bootstrap_state_authority_backend"),
        sa.CheckConstraint("status in ('pending', 'active')", name="ck_bootstrap_state_authority_status"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["bootstrap_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bootstrap_state_authority_project_backend",
        "bootstrap_state_authority",
        ["project_id", "backend", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_bootstrap_state_authority_project_backend", table_name="bootstrap_state_authority")
    op.drop_table("bootstrap_state_authority")
