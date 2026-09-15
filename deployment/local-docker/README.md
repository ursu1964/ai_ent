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

### Prerequisites

Run these commands from the accepted release checkout. Confirm the release before
starting:

```bash
git rev-parse HEAD
git rev-parse HEAD^{tree}
```

For LDH-001 and later local validation, the product process expects:

- Docker daemon available.
- Existing `aient-postgres` container running.
- Existing `aient-postgres-data` volume preserved.
- Existing PostgreSQL database `ai_ent`.
- Alembic current revision equal to repository head.
- Private local environment available outside Git.

Check the existing database without printing secrets:

```bash
docker exec aient-postgres pg_isready -U ai_ent -d ai_ent
./aient/bin/python -m alembic current
```

Do not recreate or destroy `aient-postgres` or `aient-postgres-data` as part of
normal local product startup.

### Private Operator Environment

Create a private operator environment outside Git by copying the variable names
from `operator-env.template` into your local `.env` or shell environment. Secret
values must remain private. Generate the local operator password hash without
recording the password in repository artifacts:

```bash
aient-product-local hash-password '<operator-provided password>'
# or, before console scripts are refreshed:
python -m ai_ent_product_deployment hash-password '<operator-provided password>'
```

The required private variables are:

- `AIENT_USE_EXISTING_POSTGRES`
- `AIENT_PRODUCT_BIND_HOST`
- `AIENT_PRODUCT_PORT`
- `AIENT_DB_HOST`
- `AIENT_DB_PORT`
- `AIENT_DB_NAME`
- `AIENT_DB_USER`
- `AIENT_DB_PASSWORD`
- `AIENT_OPERATOR_USERNAME`
- `AIENT_OPERATOR_DISPLAY_NAME`
- `AIENT_OPERATOR_PASSWORD_HASH`
- `AIENT_OPERATOR_ROLES`

`AIENT_OPERATOR_ROLES` must include `authenticated_user`. For local operator
workflows use `authenticated_user,project_operator` unless a separate acceptance
review authorizes additional roles. Do not commit the private environment file
or copy secret values into logs, evidence, issue trackers, or shell history.

### Validate Configuration

Then validate and start the local product surface:

```bash
aient-product-local validate-config --env-file .env
# or:
python -m ai_ent_product_deployment validate-config --env-file .env
```

Successful validation proves:

- static local deployment configuration is valid;
- bind host remains loopback-only;
- required operator bootstrap values are present;
- PostgreSQL is reachable with the configured credentials;
- the connected database matches `AIENT_DB_NAME`;
- the database migration revision matches the repository Alembic head.

Validation is read-only. It does not create a database, initialize schema,
upgrade/downgrade migrations, start replacement PostgreSQL, or fall back to
SQLite/local state.

### Start

Start the local product surface:

```bash
aient-product-local serve --env-file .env
# or:
python -m ai_ent_product_deployment serve --env-file .env
```

The product launcher defaults to `127.0.0.1:8000`, requires
`AIENT_USE_EXISTING_POSTGRES=true`, requires a configured local operator, and
rejects wildcard/non-loopback binds for local deployment validation.

Verify the listener is local only:

```bash
ss -ltnp | grep ':8000'
```

The listener must show `127.0.0.1:8000` or another configured loopback address.
It must not show `0.0.0.0`, `::`, a LAN address, or a public address.

### Health And Readiness

Use these local URLs to confirm the process and dependencies:

```bash
curl -sS http://127.0.0.1:8000/api/v1/health
curl -sS http://127.0.0.1:8000/api/v1/runtime/status
curl -sS http://127.0.0.1:8000/api/v1/runtime/postgresql
curl -sS http://127.0.0.1:8000/ui/v1/health
```

Expected healthy signals include API/UI status `ok`, runtime status `ok`, and
PostgreSQL component status `available`. Diagnostics are bounded and redacted;
do not paste private environment values into troubleshooting output.

### Login And Dashboard

Open the login view:

```bash
curl -sS http://127.0.0.1:8000/ui/v1/login
```

Authenticate using the operator username and password that produced
`AIENT_OPERATOR_PASSWORD_HASH`. The password itself is never stored in the
repository. A successful login returns a session identifier for local UI
requests. A dashboard check should succeed with that session and fail with an
invalid session:

```bash
curl -sS -X POST http://127.0.0.1:8000/ui/v1/login \
  -H 'content-type: application/json' \
  --data '{"username":"<operator>","password":"<operator-provided password>"}'

curl -sS 'http://127.0.0.1:8000/ui/v1/dashboard?session_id=<session-id>'
```

Do not put real operator credentials in committed files, command transcripts, or
evidence artifacts.

### Shutdown And Restart

Stop the product process with a graceful interrupt in the terminal running
`aient-product-local serve`, or terminate only that product process. Normal
application shutdown must leave the authoritative database running:

```bash
ss -ltnp | grep ':8000' || true
docker ps --format '{{.Names}} {{.Image}} {{.Ports}}' | grep '^aient-postgres '
docker volume ls --format '{{.Name}}' | grep '^aient-postgres-data$'
```

Restart with the same command:

```bash
aient-product-local validate-config --env-file .env
aient-product-local serve --env-file .env
```

After restart, repeat the health checks and operator login. PostgreSQL-backed
state must remain authoritative; no fallback database should appear.

### Troubleshooting

- `validate-config` fails: read the failure category, correct the private
  environment, and rerun validation. Do not run migrations automatically unless
  a separate migration operation authorizes it.
- PostgreSQL unreachable: confirm `aient-postgres` is running and bound locally
  with `docker ps` and `docker exec aient-postgres pg_isready -U ai_ent -d ai_ent`.
- Wrong `AIENT_DB_NAME`: validation fails because the connected database does
  not match the configured name. Correct the private environment; do not create
  a new database for validation.
- Alembic revision mismatch: run `./aient/bin/python -m alembic current` and
  compare with the repository head. Treat migration changes as a separate
  controlled operation.
- Missing operator configuration: set `AIENT_OPERATOR_USERNAME`,
  `AIENT_OPERATOR_PASSWORD_HASH`, and `AIENT_OPERATOR_ROLES` in the private
  environment.
- Login failure: confirm the username, regenerate the password hash from the
  intended password, and avoid copying the password into evidence.
- Occupied port: stop the stale product process or choose another loopback port
  in the private environment.
- Invalid bind: use `127.0.0.1`, `localhost`, `::1`, or another `127.*`
  loopback address. LAN validation is a separate operation.
- Unhealthy readiness: inspect the redacted health endpoints above and correct
  the failing dependency before proceeding.
- Stale product process: identify it with `ss -ltnp | grep ':8000'` and stop
  only the product process, not PostgreSQL.

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
