from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

ProjectStatus = Literal["active", "archived"]
TaskStatus = Literal["pending", "running", "passed", "failed", "blocked"]
ExecutionClass = Literal["implementation", "simulation"]
ExecutionStatus = Literal["pending", "running", "succeeded", "failed", "timeout", "cancelled"]
CheckpointType = Literal["task", "execution", "bootstrap", "runtime"]


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

    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'running', 'succeeded', 'failed', 'timeout', 'cancelled')",
            name="ck_executions_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_executions_attempt_positive"),
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
