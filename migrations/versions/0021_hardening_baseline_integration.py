"""Hardening baseline integration provenance.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-16 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("baseline_commit", sa.String(length=128), nullable=True))
    op.add_column("executions", sa.Column("baseline_tree", sa.String(length=128), nullable=True))
    op.add_column("executions", sa.Column("baseline_generation", sa.Integer(), nullable=True))

    op.create_table(
        "runtime_baseline_integrations",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("import_id", sa.String(length=80), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=80), nullable=False),
        sa.Column("plan_version", sa.String(length=40), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("source_execution_id", sa.String(length=64), nullable=False),
        sa.Column("source_commit", sa.String(length=128), nullable=False),
        sa.Column("source_tree", sa.String(length=128), nullable=False),
        sa.Column("prior_baseline_generation", sa.Integer(), nullable=False),
        sa.Column("prior_baseline_commit", sa.String(length=128), nullable=False),
        sa.Column("prior_baseline_tree", sa.String(length=128), nullable=False),
        sa.Column("integrated_commit", sa.String(length=128), nullable=False),
        sa.Column("integrated_tree", sa.String(length=128), nullable=False),
        sa.Column("integration_strategy", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("verification_evidence_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('pending', 'integrated_unverified', 'verified', 'verification_failed', 'conflict', 'rejected')",
            name="ck_runtime_baseline_integrations_status",
        ),
        sa.CheckConstraint("prior_baseline_generation >= 0", name="ck_runtime_baseline_integrations_generation"),
        sa.ForeignKeyConstraint(["import_id"], ["runtime_plan_imports.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_execution_id"], ["executions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id", "id", name="uq_runtime_baseline_integrations_import_id"),
        sa.UniqueConstraint(
            "plan_id",
            "plan_version",
            "task_id",
            "source_execution_id",
            "prior_baseline_generation",
            name="uq_runtime_baseline_integrations_task_execution_generation",
        ),
    )
    op.create_index(
        "ix_runtime_baseline_integrations_import",
        "runtime_baseline_integrations",
        ["import_id"],
    )
    op.create_index(
        "ix_runtime_baseline_integrations_plan",
        "runtime_baseline_integrations",
        ["plan_id", "plan_version"],
    )
    op.create_index(
        "ix_runtime_baseline_integrations_task_status",
        "runtime_baseline_integrations",
        ["task_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_baseline_integrations_task_status", table_name="runtime_baseline_integrations")
    op.drop_index("ix_runtime_baseline_integrations_plan", table_name="runtime_baseline_integrations")
    op.drop_index("ix_runtime_baseline_integrations_import", table_name="runtime_baseline_integrations")
    op.drop_table("runtime_baseline_integrations")
    op.drop_column("executions", "baseline_generation")
    op.drop_column("executions", "baseline_tree")
    op.drop_column("executions", "baseline_commit")
