from __future__ import annotations

import json

from ai_ent.manifest_intake import (
    MANIFEST_INTAKE_SCHEMA_VERSION,
    parse_approved_project_manifest,
    require_approved_project_manifest,
)


def test_fr_001_parses_approved_yaml_manifest_deterministically() -> None:
    first = parse_approved_project_manifest(_approved_manifest_yaml(), source_name="project.yaml")
    second = parse_approved_project_manifest(_approved_manifest_yaml(), source_name="project.yaml")

    assert first.ok
    assert first.manifest_hash == second.manifest_hash
    assert first.source_format == "yaml"
    assert first.manifest is not None
    assert first.manifest["metadata"]["schema_version"] == MANIFEST_INTAKE_SCHEMA_VERSION
    assert first.manifest["metadata"]["approval_status"] == "approved"
    assert first.as_dict()["manifest_id"] == "crm-v1"


def test_fr_001_parses_approved_json_manifest() -> None:
    manifest = require_approved_project_manifest(
        json.dumps(_approved_manifest_dict()),
        source_name="project.json",
    )

    assert manifest["metadata"]["id"] == "crm-v1"
    assert manifest["capabilities"][0]["owner"] == "Sales Operations"


def test_fr_001_rejects_unapproved_manifest() -> None:
    result = parse_approved_project_manifest(
        _approved_manifest_yaml().replace("approvalStatus: approved", "approvalStatus: proposed"),
        source_name="project.yaml",
    )

    assert not result.ok
    assert "metadata.approval_status must be approved before intake" in result.validation_errors


def test_fr_001_rejects_implementation_specific_sections() -> None:
    result = parse_approved_project_manifest(
        _approved_manifest_yaml() + "\ndatabaseSchema:\n  tables: [customers]\n",
        source_name="project.yaml",
    )

    assert not result.ok
    assert any("implementation-specific" in error for error in result.validation_errors)


def test_fr_001_validates_workflow_references() -> None:
    result = parse_approved_project_manifest(
        _approved_manifest_yaml().replace("capability: Track Opportunities", "capability: Generate Invoices"),
        source_name="project.yaml",
    )

    assert not result.ok
    assert any("references unknown capability Generate Invoices" in error for error in result.validation_errors)


def test_fr_001_rejects_circular_capability_dependencies() -> None:
    result = parse_approved_project_manifest(
        _approved_manifest_yaml().replace("dependsOn: [Manage Customers]", "dependsOn: [Track Opportunities]"),
        source_name="project.yaml",
    )

    assert not result.ok
    assert any("capability dependency cycle" in error for error in result.validation_errors)


def _approved_manifest_dict() -> dict[str, object]:
    return {
        "metadata": {
            "id": "crm-v1",
            "name": "Customer Growth CRM",
            "version": "1.0.0",
            "schemaVersion": MANIFEST_INTAKE_SCHEMA_VERSION,
            "registryVersion": "registry-2026.09",
            "generatorVersion": "generator-0.1",
            "compilerVersion": "compiler-0.1",
            "approvalStatus": "approved",
        },
        "organization": {
            "name": "Acme Ltd",
            "industry": "Retail",
            "country": "US",
        },
        "vision": "Give sales teams one approved view of customers and opportunities.",
        "domain": "CRM",
        "objectives": [
            {
                "id": "OBJ-001",
                "title": "Reduce opportunity follow-up delay",
                "indicator": "Median follow-up time below 4 business hours",
            }
        ],
        "users": [
            {"id": "USR-001", "name": "Sales Manager"},
            {"id": "USR-002", "name": "Sales Representative"},
        ],
        "entities": [
            {"id": "ENT-001", "name": "Customer"},
            {"id": "ENT-002", "name": "Opportunity"},
        ],
        "capabilities": [
            {
                "id": "CAP-001",
                "name": "Manage Customers",
                "owner": "Sales Operations",
            },
            {
                "id": "CAP-002",
                "name": "Track Opportunities",
                "owner": "Sales Operations",
                "dependsOn": ["Manage Customers"],
            },
        ],
        "workflows": [
            {
                "id": "WF-001",
                "name": "Opportunity follow-up",
                "steps": [
                    {"capability": "Manage Customers", "entity": "Customer"},
                    {"capability": "Track Opportunities", "entity": "Opportunity"},
                ],
            }
        ],
    }


def _approved_manifest_yaml() -> str:
    return """
metadata:
  id: crm-v1
  name: Customer Growth CRM
  version: 1.0.0
  schemaVersion: aepm-0.1
  registryVersion: registry-2026.09
  generatorVersion: generator-0.1
  compilerVersion: compiler-0.1
  approvalStatus: approved
organization:
  name: Acme Ltd
  industry: Retail
  country: US
vision: Give sales teams one approved view of customers and opportunities.
domain: CRM
objectives:
  - id: OBJ-001
    title: Reduce opportunity follow-up delay
    indicator: Median follow-up time below 4 business hours
users:
  - id: USR-001
    name: Sales Manager
  - id: USR-002
    name: Sales Representative
entities:
  - id: ENT-001
    name: Customer
  - id: ENT-002
    name: Opportunity
capabilities:
  - id: CAP-001
    name: Manage Customers
    owner: Sales Operations
  - id: CAP-002
    name: Track Opportunities
    owner: Sales Operations
    dependsOn: [Manage Customers]
workflows:
  - id: WF-001
    name: Opportunity follow-up
    steps:
      - capability: Manage Customers
        entity: Customer
      - capability: Track Opportunities
        entity: Opportunity
"""
