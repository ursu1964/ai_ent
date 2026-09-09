from __future__ import annotations

from sqlalchemy import delete, or_, select

from ai_ent.persistence.database import Database
from ai_ent.persistence.models import (
    BootstrapCheckpoint,
    BootstrapRun,
    BootstrapStateAuthority,
    Checkpoint,
    Execution,
    Project,
    RuntimeHumanGate,
    RuntimePlanImport,
    RuntimeTaskPlanBinding,
    Task,
    TaskDependency,
    TaskLease,
)

TEST_PROJECT_PREFIX = "test-project-"
LEGACY_TEST_PROJECT_PREFIX = "project-test-"
TEST_RUN_PREFIX = "test-run-"


def make_test_suffix(raw: str) -> str:
    return f"test-{raw}"


def make_test_project_id(suffix: str) -> str:
    return f"{TEST_PROJECT_PREFIX}{suffix}"


def make_test_run_id(suffix: str) -> str:
    return f"{TEST_RUN_PREFIX}{suffix}"


def clean_test_tables(database: Database) -> None:
    with database.session() as session:
        test_project_filter = or_(
            Project.id.like(f"{TEST_PROJECT_PREFIX}%"),
            Project.id.like(f"{LEGACY_TEST_PROJECT_PREFIX}%"),
        )
        test_task_filter = or_(
            Task.project_id.like(f"{TEST_PROJECT_PREFIX}%"),
            Task.project_id.like(f"{LEGACY_TEST_PROJECT_PREFIX}%"),
        )
        test_run_filter = or_(
            BootstrapRun.project_id.like(f"{TEST_PROJECT_PREFIX}%"),
            BootstrapRun.project_id.like(f"{LEGACY_TEST_PROJECT_PREFIX}%"),
        )
        task_ids = select(Task.id).where(test_task_filter)
        run_ids = select(BootstrapRun.id).where(test_run_filter)

        session.execute(
            delete(BootstrapStateAuthority).where(
                or_(
                    BootstrapStateAuthority.project_id.like(f"{TEST_PROJECT_PREFIX}%"),
                    BootstrapStateAuthority.project_id.like(f"{LEGACY_TEST_PROJECT_PREFIX}%"),
                )
            )
        )
        import_ids = select(RuntimePlanImport.id).where(RuntimePlanImport.project_id.like(f"{TEST_PROJECT_PREFIX}%"))
        session.execute(delete(RuntimeHumanGate).where(RuntimeHumanGate.import_id.in_(import_ids)))
        session.execute(delete(RuntimeTaskPlanBinding).where(RuntimeTaskPlanBinding.import_id.in_(import_ids)))
        session.execute(delete(RuntimePlanImport).where(RuntimePlanImport.id.in_(import_ids)))
        session.execute(delete(BootstrapCheckpoint).where(BootstrapCheckpoint.run_id.in_(run_ids)))
        session.execute(delete(BootstrapRun).where(test_run_filter))
        session.execute(delete(TaskLease).where(TaskLease.task_id.in_(task_ids)))
        session.execute(delete(Checkpoint).where(Checkpoint.task_id.in_(task_ids)))
        session.execute(delete(Execution).where(Execution.task_id.in_(task_ids)))
        session.execute(
            delete(TaskDependency).where(
                (TaskDependency.task_id.in_(task_ids)) | (TaskDependency.depends_on_task_id.in_(task_ids))
            )
        )
        session.execute(delete(Task).where(test_task_filter))
        session.execute(delete(Project).where(test_project_filter))
