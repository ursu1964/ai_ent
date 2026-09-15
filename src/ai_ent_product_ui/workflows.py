from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ai_ent_product_ui.client import ProductIntakeClient
from ai_ent_product_ui.models import (
    NewProjectView,
    ProjectIntakeDraft,
    ProjectListItem,
    ProjectListView,
    ProjectOverviewView,
    ValidationView,
    sorted_projects,
)


@dataclass
class ProjectUiController:
    intake_client: ProductIntakeClient = field(default_factory=ProductIntakeClient)
    _projects: dict[str, ProjectListItem] = field(default_factory=dict)

    @classmethod
    def with_projects(
        cls,
        projects: Iterable[ProjectListItem],
        *,
        intake_client: ProductIntakeClient | None = None,
    ) -> ProjectUiController:
        return cls(
            intake_client=intake_client or ProductIntakeClient(),
            _projects={project.project_id: project for project in projects},
        )

    def project_list(self, *, selected_project_id: str | None = None) -> ProjectListView:
        return ProjectListView(
            projects=sorted_projects(tuple(self._projects.values())),
            boundary=self.intake_client.boundary(),
            selected_project_id=selected_project_id,
        )

    def new_project(self, draft: ProjectIntakeDraft) -> NewProjectView:
        return NewProjectView(
            draft=draft,
            boundary=self.intake_client.boundary(),
            validation=ValidationView.draft(),
        )

    def submit_new_project(self, draft: ProjectIntakeDraft) -> ProjectOverviewView:
        project = self.intake_client.create_project_intake(draft)
        self._projects[project.project_id] = project
        return ProjectOverviewView(project=project, boundary=self.intake_client.boundary())

    def project_overview(self, project_id: str) -> ProjectOverviewView:
        return ProjectOverviewView(
            project=self._projects[project_id],
            boundary=self.intake_client.boundary(),
        )
