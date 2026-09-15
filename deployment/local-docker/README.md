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

## Inventory

The normative package inventory is declared in `portability.yaml` and is limited
to this README, the Dockerfile, the Compose file, the portability contract, and
the private-environment template.

## Local Use

The preferred local validation mode is `USE_EXISTING_POSTGRES`. It preserves the
already-authoritative `aient-postgres` container and starts only the product
API/UI process on loopback.

Create a private operator environment outside Git by copying the variable names
from `operator-env.template` into your local `.env` or shell environment. Secret
values must remain private. Generate the local operator password hash without
recording the password in repository artifacts:

```bash
aient-product-local hash-password '<operator-provided password>'
# or, before console scripts are refreshed:
python -m ai_ent_product_deployment hash-password '<operator-provided password>'
```

Then validate and start the local product surface:

```bash
aient-product-local validate-config --env-file .env
aient-product-local serve --env-file .env
# or:
python -m ai_ent_product_deployment validate-config --env-file .env
python -m ai_ent_product_deployment serve --env-file .env
```

The product launcher defaults to `127.0.0.1:8000`, requires
`AIENT_USE_EXISTING_POSTGRES=true`, requires a configured local operator, and
rejects wildcard/non-loopback binds for local deployment validation.

The Docker Compose package remains available for explicit operator workflows.
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
