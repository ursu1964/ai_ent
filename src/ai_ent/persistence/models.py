from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

ProjectStatus = Literal["active", "archived"]
TaskStatus = Literal["pending", "running", "passed", "failed", "blocked"]
ExecutionClass = Literal["implementation", "simulation"]
ExecutionStatus = Literal["pending", "running", "succeeded", "failed", "timeout", "cancelled"]
CheckpointType = Literal["task", "execution", "bootstrap", "runtime"]
LeaseStatus = Literal["active", "released", "completed", "expired"]
BootstrapRunStatus = Literal["pending", "running", "blocked", "failed", "completed"]
BootstrapCheckpointKind = Literal["task_completed", "run_blocked", "run_failed", "run_completed", "state_snapshot"]
BootstrapStateBackend = Literal["local_json", "postgresql"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for runtime persistence models."""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    status: Mapped[ProjectStatus] = mapped_column(String(40), default="active", nullable=False)

    tasks: Mapped[list[Task]] = relationship(back_populates="project")
    bootstrap_runs: Mapped[list[BootstrapRun]] = relationship(back_populates="project")

    __table_args__ = (
        CheckConstraint("status in ('active', 'archived')", name="ck_projects_status"),
        Index("ix_projects_status", "status"),
    )


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    objective: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(String(40), default="pending", nullable=False)
    execution_class: Mapped[ExecutionClass] = mapped_column(
        String(40),
        default="implementation",
        nullable=False,
    )
    schedulable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    fingerprint: Mapped[str | None] = mapped_column(String(128), unique=True)

    project: Mapped[Project] = relationship(back_populates="tasks")
    dependencies: Mapped[list[TaskDependency]] = relationship(
        back_populates="task",
        foreign_keys="TaskDependency.task_id",
    )
    executions: Mapped[list[Execution]] = relationship(back_populates="task")
    checkpoints: Mapped[list[Checkpoint]] = relationship(back_populates="task")
    leases: Mapped[list[TaskLease]] = relationship(back_populates="task")

    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'running', 'passed', 'failed', 'blocked')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "execution_class in ('implementation', 'simulation')",
            name="ck_tasks_execution_class",
        ),
        Index("ix_tasks_project_status", "project_id", "status"),
        Index("ix_tasks_schedulable", "schedulable"),
    )


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    depends_on_task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    task: Mapped[Task] = relationship(
        back_populates="dependencies",
        foreign_keys=[task_id],
    )
    depends_on_task: Mapped[Task] = relationship(foreign_keys=[depends_on_task_id])

    __table_args__ = (
        CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dependencies_not_self"),
        Index("ix_task_dependencies_depends_on", "depends_on_task_id"),
    )


class Execution(TimestampMixin, Base):
    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False)
    executor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(String(40), default="pending", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_state: Mapped[str | None] = mapped_column(String(80))
    error_classification: Mapped[str | None] = mapped_column(String(120))
    candidate_tree_hash: Mapped[str | None] = mapped_column(String(64))
    commit_hash: Mapped[str | None] = mapped_column(String(64))

    task: Mapped[Task] = relationship(back_populates="executions")
    checkpoints: Mapped[list[Checkpoint]] = relationship(back_populates="execution")
    lease: Mapped[TaskLease | None] = relationship(back_populates="execution")

    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'running', 'succeeded', 'failed', 'timeout', 'cancelled')",
            name="ck_executions_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_executions_attempt_positive"),
        UniqueConstraint("task_id", "attempt", name="uq_executions_task_attempt"),
        Index("ix_executions_task_status", "task_id", "status"),
    )


class Checkpoint(TimestampMixin, Base):
    __tablename__ = "checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(ForeignKey("executions.id", ondelete="RESTRICT"))
    checkpoint_type: Mapped[CheckpointType] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    commit_hash: Mapped[str | None] = mapped_column(String(64))
    tree_hash: Mapped[str | None] = mapped_column(String(64))

    task: Mapped[Task] = relationship(back_populates="checkpoints")
    execution: Mapped[Execution | None] = relationship(back_populates="checkpoints")

    __table_args__ = (
        CheckConstraint(
            "checkpoint_type in ('task', 'execution', 'bootstrap', 'runtime')",
            name="ck_checkpoints_type",
        ),
        Index("ix_checkpoints_task_created", "task_id", "created_at"),
        Index("ix_checkpoints_execution", "execution_id"),
    )


class TaskLease(TimestampMixin, Base):
    __tablename__ = "task_leases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("executions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[LeaseStatus] = mapped_column(String(40), default="active", nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[Task] = relationship(back_populates="leases")
    execution: Mapped[Execution] = relationship(back_populates="lease")

    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'released', 'completed', 'expired')",
            name="ck_task_leases_status",
        ),
        CheckConstraint("expires_at > acquired_at", name="ck_task_leases_expires_after_acquire"),
        Index(
            "uq_task_leases_one_active_per_task",
            "task_id",
            unique=True,
            postgresql_where=(status == "active"),
            sqlite_where=(status == "active"),
        ),
        Index("ix_task_leases_task_status", "task_id", "status"),
        Index("ix_task_leases_owner_status", "owner_id", "status"),
        Index("ix_task_leases_expires_at", "expires_at"),
    )


class BootstrapRun(TimestampMixin, Base):
    __tablename__ = "bootstrap_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[BootstrapRunStatus] = mapped_column(String(40), default="pending", nullable=False)
    manifest_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    baseline_commit: Mapped[str | None] = mapped_column(String(128))
    current_stage: Mapped[str | None] = mapped_column(String(80))
    current_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    last_completed_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    last_verified_commit: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_classification: Mapped[str | None] = mapped_column(String(120))
    blocked_reason: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="bootstrap_runs")
    current_task: Mapped[Task | None] = relationship(foreign_keys=[current_task_id])
    last_completed_task: Mapped[Task | None] = relationship(foreign_keys=[last_completed_task_id])
    bootstrap_checkpoints: Mapped[list[BootstrapCheckpoint]] = relationship(back_populates="run")

    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'running', 'blocked', 'failed', 'completed')",
            name="ck_bootstrap_runs_status",
        ),
        Index("ix_bootstrap_runs_project_status", "project_id", "status"),
        Index("ix_bootstrap_runs_current_task", "current_task_id"),
        Index("ix_bootstrap_runs_last_completed_task", "last_completed_task_id"),
    )


class BootstrapCheckpoint(TimestampMixin, Base):
    __tablename__ = "bootstrap_checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("bootstrap_runs.id", ondelete="RESTRICT"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint_kind: Mapped[BootstrapCheckpointKind] = mapped_column(String(40), nullable=False)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    execution_id: Mapped[str | None] = mapped_column(ForeignKey("executions.id", ondelete="RESTRICT"))
    verified_commit: Mapped[str | None] = mapped_column(String(128))
    tree_hash: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(Text, nullable=False)

    run: Mapped[BootstrapRun] = relationship(back_populates="bootstrap_checkpoints")
    task: Mapped[Task | None] = relationship(foreign_keys=[task_id])
    execution: Mapped[Execution | None] = relationship(foreign_keys=[execution_id])

    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_bootstrap_checkpoints_sequence_positive"),
        CheckConstraint(
            "checkpoint_kind in ('task_completed', 'run_blocked', 'run_failed', 'run_completed', 'state_snapshot')",
            name="ck_bootstrap_checkpoints_kind",
        ),
        UniqueConstraint("run_id", "sequence", name="uq_bootstrap_checkpoints_run_sequence"),
        Index("ix_bootstrap_checkpoints_run_created", "run_id", "created_at"),
        Index("ix_bootstrap_checkpoints_task", "task_id"),
    )


class BootstrapStateAuthority(TimestampMixin, Base):
    __tablename__ = "bootstrap_state_authority"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("bootstrap_runs.id", ondelete="RESTRICT"), nullable=False)
    backend: Mapped[BootstrapStateBackend] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_snapshot_path: Mapped[str | None] = mapped_column(String(500))

    project: Mapped[Project] = relationship(foreign_keys=[project_id])
    run: Mapped[BootstrapRun] = relationship(foreign_keys=[run_id])

    __table_args__ = (
        CheckConstraint("backend in ('local_json', 'postgresql')", name="ck_bootstrap_state_authority_backend"),
        CheckConstraint("status in ('pending', 'active')", name="ck_bootstrap_state_authority_status"),
        Index("ix_bootstrap_state_authority_project_backend", "project_id", "backend", "status"),
    )
