"""Runtime frozen plan import metadata.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-09 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_plan_imports",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("plan_project_id", sa.String(length=80), nullable=False),
        sa.Column("plan_id", sa.String(length=80), nullable=False),
        sa.Column("plan_version", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("compiled_project_hash", sa.String(length=64), nullable=False),
        sa.Column("capability_resolution_hash", sa.String(length=64), nullable=False),
        sa.Column("trace_validation_hash", sa.String(length=64), nullable=False),
        sa.Column("implementation_plan_hash", sa.String(length=64), nullable=False),
        sa.Column("feasibility_hash", sa.String(length=64), nullable=False),
        sa.Column("dry_run_hash", sa.String(length=64), nullable=False),
        sa.Column("task_fingerprint_hash", sa.String(length=64), nullable=False),
        sa.Column("dependency_graph_hash", sa.String(length=64), nullable=False),
        sa.Column("importer_version", sa.String(length=40), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("dependency_count", sa.Integer(), nullable=False),
        sa.Column("human_gate_count", sa.Integer(), nullable=False),
        sa.Column("effective_concurrency", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('imported', 'conflict', 'failed')", name="ck_runtime_plan_imports_status"),
        sa.CheckConstraint("task_count >= 0", name="ck_runtime_plan_imports_task_count"),
        sa.CheckConstraint("dependency_count >= 0", name="ck_runtime_plan_imports_dependency_count"),
        sa.CheckConstraint("human_gate_count >= 0", name="ck_runtime_plan_imports_human_gate_count"),
        sa.CheckConstraint("effective_concurrency >= 1", name="ck_runtime_plan_imports_effective_concurrency"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "plan_version", name="uq_runtime_plan_imports_plan"),
    )
    op.create_index("ix_runtime_plan_imports_project", "runtime_plan_imports", ["project_id"])

    op.create_table(
        "runtime_task_plan_bindings",
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("import_id", sa.String(length=80), nullable=False),
        sa.Column("plan_id", sa.String(length=80), nullable=False),
        sa.Column("plan_version", sa.String(length=40), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("risk_level", sa.String(length=40), nullable=False),
        sa.Column("agent_role", sa.String(length=80), nullable=False),
        sa.Column("model_profile", sa.String(length=80), nullable=False),
        sa.Column("executor", sa.String(length=80), nullable=False),
        sa.Column("verification_profile", sa.String(length=80), nullable=False),
        sa.Column("feasibility_status", sa.String(length=80), nullable=False),
        sa.Column("policy_decision", sa.String(length=80), nullable=False),
        sa.Column("implements_json", sa.Text(), nullable=False),
        sa.Column("write_scope_json", sa.Text(), nullable=False),
        sa.Column("acceptance_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["runtime_plan_imports.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint("import_id", "task_id", name="uq_runtime_task_plan_bindings_import_task"),
    )
    op.create_index("ix_runtime_task_plan_bindings_import", "runtime_task_plan_bindings", ["import_id"])
    op.create_index(
        "ix_runtime_task_plan_bindings_plan",
        "runtime_task_plan_bindings",
        ["plan_id", "plan_version"],
    )

    op.create_table(
        "runtime_human_gates",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("import_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=80), nullable=False),
        sa.Column("plan_version", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(length=40), nullable=False),
        sa.Column("approval_boundary", sa.String(length=120), nullable=False),
        sa.Column("expected_evidence_json", sa.Text(), nullable=False),
        sa.Column("downstream_task_ids_json", sa.Text(), nullable=False),
        sa.Column("resume_semantics", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('pending', 'approved', 'rejected')", name="ck_runtime_human_gates_status"),
        sa.ForeignKeyConstraint(["import_id"], ["runtime_plan_imports.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id", "id", name="uq_runtime_human_gates_import_gate"),
    )
    op.create_index("ix_runtime_human_gates_import", "runtime_human_gates", ["import_id"])
    op.create_index("ix_runtime_human_gates_task_status", "runtime_human_gates", ["task_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_runtime_human_gates_task_status", table_name="runtime_human_gates")
    op.drop_index("ix_runtime_human_gates_import", table_name="runtime_human_gates")
    op.drop_table("runtime_human_gates")
    op.drop_index("ix_runtime_task_plan_bindings_plan", table_name="runtime_task_plan_bindings")
    op.drop_index("ix_runtime_task_plan_bindings_import", table_name="runtime_task_plan_bindings")
    op.drop_table("runtime_task_plan_bindings")
    op.drop_index("ix_runtime_plan_imports_project", table_name="runtime_plan_imports")
    op.drop_table("runtime_plan_imports")
