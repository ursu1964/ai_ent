"""Bootstrap run state.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-09 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bootstrap_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("manifest_ref", sa.String(length=240), nullable=False),
        sa.Column("baseline_commit", sa.String(length=128), nullable=True),
        sa.Column("current_stage", sa.String(length=80), nullable=True),
        sa.Column("current_task_id", sa.String(length=64), nullable=True),
        sa.Column("last_completed_task_id", sa.String(length=64), nullable=True),
        sa.Column("last_verified_commit", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_classification", sa.String(length=120), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('pending', 'running', 'blocked', 'failed', 'completed')",
            name="ck_bootstrap_runs_status",
        ),
        sa.ForeignKeyConstraint(["current_task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["last_completed_task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bootstrap_runs_project_status", "bootstrap_runs", ["project_id", "status"])
    op.create_index("ix_bootstrap_runs_current_task", "bootstrap_runs", ["current_task_id"])
    op.create_index("ix_bootstrap_runs_last_completed_task", "bootstrap_runs", ["last_completed_task_id"])

    op.create_table(
        "bootstrap_checkpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("checkpoint_kind", sa.String(length=40), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("execution_id", sa.String(length=64), nullable=True),
        sa.Column("verified_commit", sa.String(length=128), nullable=True),
        sa.Column("tree_hash", sa.String(length=128), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_bootstrap_checkpoints_sequence_positive"),
        sa.CheckConstraint(
            "checkpoint_kind in ('task_completed', 'run_blocked', 'run_failed', 'run_completed', 'state_snapshot')",
            name="ck_bootstrap_checkpoints_kind",
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["bootstrap_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_bootstrap_checkpoints_run_sequence"),
    )
    op.create_index(
        "ix_bootstrap_checkpoints_run_created",
        "bootstrap_checkpoints",
        ["run_id", "created_at"],
    )
    op.create_index("ix_bootstrap_checkpoints_task", "bootstrap_checkpoints", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_bootstrap_checkpoints_task", table_name="bootstrap_checkpoints")
    op.drop_index("ix_bootstrap_checkpoints_run_created", table_name="bootstrap_checkpoints")
    op.drop_table("bootstrap_checkpoints")
    op.drop_index("ix_bootstrap_runs_last_completed_task", table_name="bootstrap_runs")
    op.drop_index("ix_bootstrap_runs_current_task", table_name="bootstrap_runs")
    op.drop_index("ix_bootstrap_runs_project_status", table_name="bootstrap_runs")
    op.drop_table("bootstrap_runs")
