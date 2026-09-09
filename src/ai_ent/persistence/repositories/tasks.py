from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.persistence.models import ExecutionClass, Task, TaskDependency, TaskStatus
from ai_ent.persistence.repositories.errors import (
    DuplicateDependencyError,
    DuplicateTaskError,
    MissingTaskError,
    RepositoryError,
)


class TaskRepository:
    def create(
        self,
        session: Session,
        *,
        task_id: str,
        project_id: str,
        title: str,
        objective: str | None = None,
        status: TaskStatus = "pending",
        execution_class: ExecutionClass = "implementation",
        schedulable: bool = True,
        fingerprint: str | None = None,
    ) -> Task:
        task = Task(
            id=task_id,
            project_id=project_id,
            title=title,
            objective=objective,
            status=status,
            execution_class=execution_class,
            schedulable=schedulable,
            fingerprint=fingerprint,
        )
        session.add(task)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DuplicateTaskError(f"task already exists: {task_id}") from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("task persistence failed") from exc
        return task

    def get(self, session: Session, task_id: str) -> Task | None:
        return session.get(Task, task_id)

    def require(self, session: Session, task_id: str) -> Task:
        task = self.get(session, task_id)
        if task is None:
            raise MissingTaskError(f"task not found: {task_id}")
        return task

    def exists(self, session: Session, task_id: str) -> bool:
        return session.get(Task, task_id) is not None

    def list_by_project(self, session: Session, project_id: str) -> list[Task]:
        statement = select(Task).where(Task.project_id == project_id).order_by(Task.created_at, Task.id)
        return list(session.scalars(statement).all())

    def list_by_status(
        self,
        session: Session,
        status: TaskStatus,
        *,
        project_id: str | None = None,
    ) -> list[Task]:
        statement = select(Task).where(Task.status == status).order_by(Task.created_at, Task.id)
        if project_id is not None:
            statement = statement.where(Task.project_id == project_id)
        return list(session.scalars(statement).all())

    def update_status(self, session: Session, task_id: str, status: TaskStatus) -> Task:
        task = self.require(session, task_id)
        task.status = status
        try:
            session.flush()
        except IntegrityError as exc:
            raise RepositoryError(f"invalid task status: {status}") from exc
        return task

    def add_dependency(self, session: Session, *, task_id: str, depends_on_task_id: str) -> None:
        dependency = TaskDependency(task_id=task_id, depends_on_task_id=depends_on_task_id)
        session.add(dependency)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DuplicateDependencyError(
                f"dependency already exists or is invalid: {task_id}->{depends_on_task_id}"
            ) from exc

    def remove_dependency(self, session: Session, *, task_id: str, depends_on_task_id: str) -> bool:
        dependency = session.get(TaskDependency, (task_id, depends_on_task_id))
        if dependency is None:
            return False
        session.delete(dependency)
        session.flush()
        return True

    def list_dependencies(self, session: Session, task_id: str) -> list[str]:
        statement = (
            select(TaskDependency.depends_on_task_id)
            .where(TaskDependency.task_id == task_id)
            .order_by(TaskDependency.depends_on_task_id)
        )
        return list(session.scalars(statement).all())

    def list_dependents(self, session: Session, task_id: str) -> list[str]:
        statement = (
            select(TaskDependency.task_id)
            .where(TaskDependency.depends_on_task_id == task_id)
            .order_by(TaskDependency.task_id)
        )
        return list(session.scalars(statement).all())
