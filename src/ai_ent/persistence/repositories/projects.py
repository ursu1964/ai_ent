from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_ent.persistence.models import Project, ProjectStatus
from ai_ent.persistence.repositories.errors import (
    DuplicateProjectError,
    MissingProjectError,
    RepositoryError,
)


class ProjectRepository:
    def create(self, session: Session, *, project_id: str, name: str) -> Project:
        project = Project(id=project_id, name=name)
        session.add(project)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DuplicateProjectError(f"project already exists: {project_id}") from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("project persistence failed") from exc
        return project

    def get(self, session: Session, project_id: str) -> Project | None:
        return session.get(Project, project_id)

    def require(self, session: Session, project_id: str) -> Project:
        project = self.get(session, project_id)
        if project is None:
            raise MissingProjectError(f"project not found: {project_id}")
        return project

    def exists(self, session: Session, project_id: str) -> bool:
        return session.get(Project, project_id) is not None

    def update_status(
        self,
        session: Session,
        project_id: str,
        status: ProjectStatus,
    ) -> Project:
        project = self.require(session, project_id)
        project.status = status
        try:
            session.flush()
        except IntegrityError as exc:
            raise RepositoryError(f"invalid project status: {status}") from exc
        return project

    def list(self, session: Session, *, status: ProjectStatus | None = None) -> list[Project]:
        statement = select(Project).order_by(Project.created_at, Project.id)
        if status is not None:
            statement = statement.where(Project.status == status)
        return list(session.scalars(statement).all())
