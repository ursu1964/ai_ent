from __future__ import annotations

from html import escape

from ai_ent_product_ui.models import (
    BoundaryView,
    NewProjectView,
    ProjectListItem,
    ProjectListView,
    ProjectOverviewView,
)


def render_project_list(view: ProjectListView) -> str:
    project_items = "\n".join(_project_card(project) for project in view.projects)
    empty_state = "" if view.projects else '<p class="empty">No project intake requests yet.</p>'
    return _page(
        "Projects",
        "\n".join(
            (
                "<h1>Projects</h1>",
                _boundary_panel(view.boundary),
                '<section aria-label="Project list">',
                empty_state,
                project_items,
                "</section>",
            )
        ),
    )


def render_new_project(view: NewProjectView) -> str:
    draft = view.draft
    return _page(
        "New Project",
        "\n".join(
            (
                "<h1>New Project Intake</h1>",
                _boundary_panel(view.boundary),
                '<form aria-label="New project intake request">',
                f'<input name="project_id" value="{escape(draft.project_id)}" />',
                f'<input name="name" value="{escape(draft.name)}" />',
                f'<textarea name="summary">{escape(draft.summary)}</textarea>',
                '<button type="submit">Submit intake request</button>',
                "</form>",
                (
                    f'<p data-validation-status="{view.validation.status}">'
                    f"Validation: {view.validation.status}</p>"
                ),
            )
        ),
    )


def render_project_overview(view: ProjectOverviewView) -> str:
    project = view.project
    validation = project.validation
    blockers = "".join(f"<li>{escape(blocker)}</li>" for blocker in validation.blockers)
    sequence = "".join(
        f"<li>{escape(step)}</li>" for step in validation.required_evolution_sequence
    )
    return _page(
        project.name,
        "\n".join(
            (
                f"<h1>{escape(project.name)}</h1>",
                f'<p class="project-summary">{escape(project.summary)}</p>',
                _boundary_panel(view.boundary),
                '<section aria-label="Project overview">',
                (
                    f'<p data-project-id="{escape(project.project_id)}">'
                    f"Project ID: {escape(project.project_id)}</p>"
                ),
                f"<p>Intake validation: {validation.status}</p>",
                f"<p>Human gate: {view.human_gate_status}</p>",
                f"<p>Independent verification: {view.independent_verification_status}</p>",
                f"<p>Control-plane project created: {str(view.control_plane_created).lower()}</p>",
                "<h2>Required Evolution Sequence</h2>",
                f"<ol>{sequence}</ol>",
                "<h2>Blockers</h2>",
                f"<ul>{blockers}</ul>",
                "</section>",
            )
        ),
    )


def _page(title: str, body: str) -> str:
    return "\n".join(
        (
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8" />',
            f"<title>{escape(title)}</title>",
            "</head>",
            "<body>",
            body,
            "</body>",
            "</html>",
        )
    )


def _boundary_panel(boundary: BoundaryView) -> str:
    verification = "".join(
        f"<li>{escape(command)}</li>" for command in boundary.mandatory_verification_commands
    )
    decisions = "".join(
        f"<li>{escape(decision)}</li>" for decision in boundary.decision_dependencies
    )
    return "\n".join(
        (
            '<aside aria-label="Control-plane boundary">',
            f"<p>Authority: {escape(boundary.control_plane_authority)}</p>",
            f"<p>Human gate policy: {boundary.human_gate_policy}</p>",
            (
                "<p>Grants control-plane authority: "
                f"{str(boundary.grants_control_plane_authority).lower()}</p>"
            ),
            (
                "<p>Implicit human gate approval: "
                f"{str(boundary.implicit_human_gate_approval).lower()}</p>"
            ),
            (
                "<p>Independent verification required: "
                f"{str(boundary.independent_verification_required).lower()}</p>"
            ),
            f"<p>Secret values exposed: {str(boundary.secret_values_exposed).lower()}</p>",
            f"<ul>{verification}</ul>",
            f"<ul>{decisions}</ul>",
            "</aside>",
        )
    )


def _project_card(project: ProjectListItem) -> str:
    return "\n".join(
        (
            f'<article data-project-id="{escape(project.project_id)}">',
            f"<h2>{escape(project.name)}</h2>",
            f"<p>{escape(project.summary)}</p>",
            f"<p>Validation: {project.validation.status}</p>",
            "</article>",
        )
    )
