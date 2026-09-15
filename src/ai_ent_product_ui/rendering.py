from __future__ import annotations

from html import escape

from pydantic import BaseModel

from ai_ent_product_ui.schemas import ArtifactBrowserView, EvidenceBrowserView, ProvenancePathView


def render_artifacts(view: ArtifactBrowserView) -> str:
    rows = "\n".join(
        _row(
            artifact.artifact_id,
            artifact.title,
            artifact.evidence_status,
            artifact.authority_state,
        )
        for artifact in view.artifacts
    )
    selected = (
        f"<section><h2>{escape(view.selected_artifact.title)}</h2>"
        f"<p>{escape(view.selected_artifact.relative_path)}</p></section>"
        if view.selected_artifact is not None
        else ""
    )
    return _page("Artifacts", _status(view), f"<table>{rows}</table>{selected}")


def render_evidence(view: EvidenceBrowserView) -> str:
    rows = "\n".join(
        _row(
            record.evidence_id,
            record.title,
            record.evidence_status,
            "verification required"
            if record.independent_verification_required
            else "verification missing",
        )
        for record in view.evidence_records
    )
    return _page("Evidence", _status(view), f"<table>{rows}</table>")


def render_provenance(view: ProvenancePathView) -> str:
    nodes = "\n".join(
        f"<li>{escape(node.node_type)}: {escape(node.label)} "
        f"<strong>{escape(node.status)}</strong></li>"
        for node in view.nodes
    )
    edges = "\n".join(
        f"<li>{escape(edge.source_id)} -> {escape(edge.target_id)} "
        f"{escape(edge.relationship)}</li>"
        for edge in view.edges
    )
    return _page(
        "Provenance",
        _status(view),
        f"<section><h2>Path</h2><ul>{nodes}</ul></section>"
        f"<section><h2>Links</h2><ul>{edges}</ul></section>",
    )


def _page(title: str, status: str, body: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title></head><body>"
        f"<header><h1>{escape(title)}</h1>{status}</header>"
        f"<main>{body}</main></body></html>"
    )


def _status(view: BaseModel) -> str:
    authority = view.model_dump(mode="json")["authority"]
    return (
        "<p>"
        "NON_AUTHORITATIVE evidence"
        " | explicit human gates"
        " | independent verification required"
        f" | control plane: {escape(str(authority['control_plane_authority']))}"
        "</p>"
    )


def _row(identifier: str, title: str, status: str, detail: str) -> str:
    return (
        "<tr>"
        f"<td>{escape(identifier)}</td>"
        f"<td>{escape(title)}</td>"
        f"<td>{escape(status)}</td>"
        f"<td>{escape(detail)}</td>"
        "</tr>"
    )
