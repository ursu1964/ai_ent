"""Core runtime persistence schema.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-09 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('active', 'archived')", name="ck_projects_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_projects_status", "projects", ["status"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("objective", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("execution_class", sa.String(length=40), nullable=False),
        sa.Column("schedulable", sa.Boolean(), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('pending', 'running', 'passed', 'failed', 'blocked')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint(
            "execution_class in ('implementation', 'simulation')",
            name="ck_tasks_execution_class",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index("ix_tasks_project_status", "tasks", ["project_id", "status"])
    op.create_index("ix_tasks_schedulable", "tasks", ["schedulable"])

    op.create_table(
        "task_dependencies",
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("depends_on_task_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dependencies_not_self"),
        sa.ForeignKeyConstraint(["depends_on_task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("task_id", "depends_on_task_id"),
    )
    op.create_index(
        "ix_task_dependencies_depends_on",
        "task_dependencies",
        ["depends_on_task_id"],
    )

    op.create_table(
        "executions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("executor_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_state", sa.String(length=80), nullable=True),
        sa.Column("error_classification", sa.String(length=120), nullable=True),
        sa.Column("candidate_tree_hash", sa.String(length=64), nullable=True),
        sa.Column("commit_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('pending', 'running', 'succeeded', 'failed', 'timeout', 'cancelled')",
            name="ck_executions_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_executions_attempt_positive"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_executions_task_status", "executions", ["task_id", "status"])

    op.create_table(
        "checkpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("execution_id", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_type", sa.String(length=40), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("commit_hash", sa.String(length=64), nullable=True),
        sa.Column("tree_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "checkpoint_type in ('task', 'execution', 'bootstrap', 'runtime')",
            name="ck_checkpoints_type",
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_checkpoints_execution", "checkpoints", ["execution_id"])
    op.create_index("ix_checkpoints_task_created", "checkpoints", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_checkpoints_task_created", table_name="checkpoints")
    op.drop_index("ix_checkpoints_execution", table_name="checkpoints")
    op.drop_table("checkpoints")
    op.drop_index("ix_executions_task_status", table_name="executions")
    op.drop_table("executions")
    op.drop_index("ix_task_dependencies_depends_on", table_name="task_dependencies")
    op.drop_table("task_dependencies")
    op.drop_index("ix_tasks_schedulable", table_name="tasks")
    op.drop_index("ix_tasks_project_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("ix_projects_status", table_name="projects")
    op.drop_table("projects")
