# Local Docker Deployment Package

This package runs the AI-Enterprise control plane on a local Docker host while
keeping future cloud deployment decisions outside the package boundary.

## Boundary

- PostgreSQL remains the control-plane state backend.
- Operator commands remain explicit; this package does not auto-complete work.
- Human approval remains required before guarded autonomous runs, migrations,
  restore operations, or cloud-target changes.
- Independent verification remains mandatory before accepting deployment changes.
- Cloud targets remain deferred behind `PRD-DEC-002` and `PRD-DEC-003`.
- No credential values are stored in this package.

## Local Use

Set database values in the operator environment before invoking Docker Compose:

```bash
export AIENT_DB_PASSWORD='<operator-provided value>'
docker compose -f deployment/local-docker/compose.yaml up -d postgres
docker compose -f deployment/local-docker/compose.yaml run --rm operator
```

Run guarded operations only as an explicit human action:

```bash
docker compose -f deployment/local-docker/compose.yaml run --rm operator \
  python scripts/bootstrap.py guarded-run
```

Run the package portability validator without starting services:

```bash
python scripts/deployment_portability.py validate
```

## Required Verification

The independent verifier must run:

```bash
/home/user/projects/ai_ent/aient/bin/python -m pytest -q
/home/user/projects/ai_ent/aient/bin/python -m ruff check .
/home/user/projects/ai_ent/aient/bin/python -m pyright
```
