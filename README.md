# Credit backend

Backend-only MVP for the credit application API. The HTTP contract is in [docs/openapi.yaml](docs/openapi.yaml); business and technical rules are in [docs/BACKEND_RULES.md](docs/BACKEND_RULES.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Start the local demo

Requirements: Docker with the Compose plugin and Python 3 for the local launcher. PostgreSQL and the application, mock and SQL dependencies use pinned versions.

Start the complete demo with one command:

```sh
./scripts/dev-up.sh
```

The launcher creates a local `.env` with a random HS256 secret when needed, then starts PostgreSQL, S3/LocalStack, API, worker and the integration mock. It does not print or overwrite an existing secret. Compose applies database migrations and seed data before the API and worker become ready. The API is bound to the local machine at <http://localhost:8000>; interactive API documentation is at <http://localhost:8000/docs>.

The S3 emulator uses the archived LocalStack community image 4.14.0, tested without an auth token. Its `PERSISTENCE=1` feature did not restore an S3 bucket after container recreation, so S3 state is temporary. The tag can still be pulled, but upstream archived this repository in March 2026 and no longer maintains it. The PostgreSQL database is kept in a separate Docker volume.

The local-only demo accounts are `client`, `representative`, `other`, and `banker`; their passwords and roles are listed in `backend/src/credit/seed.py`. The seed command does not print passwords into service logs. Never reuse local Compose credentials outside development.

To run the HTTP happy path through simulated disbursement:

```sh
uv run --project backend --locked python tools/demo.py
```

View service logs with `docker compose logs -f api worker integration-mock`. Stop the stack with `docker compose down`; this preserves PostgreSQL data. `docker compose down -v` deletes the database volume.

LocalStack's S3 objects do not survive a container restart. After restarting LocalStack, rerun `docker compose --profile tools run --rm seed` to initialize the bucket and reset integration-mock fixtures, then upload demo documents again.

To retry cleanup of failed uploads, soft-deleted documents and old unreferenced S3 objects, run:

```sh
docker compose --profile tools run --rm cleanup
```

Cleanup uses a ten-minute grace period by default. It is a one-shot command that can be scheduled with the host's task scheduler.

## Development and checks

Install the locked environment and validate the HTTP contract and tests:

```sh
uv sync --project backend --all-groups --locked
uv run --project backend --locked python tools/validate_contract.py
uv run --project backend --locked python -m pytest
```

The container can be rebuilt with:

```sh
docker build -f backend/Dockerfile -t credit-backend:local .
```

SQL requests use generated Python code from SQL files with sqlc. The reproducible stack check and PostgreSQL tests are documented in [backend/STACK_CHECK.md](backend/STACK_CHECK.md).

To run the full PostgreSQL/S3 acceptance suite, start its isolated local services as described in [docs/TEST_SCENARIOS.md](docs/TEST_SCENARIOS.md). It uses a separate `credit_test` database and dedicated `credit-tests-*` bucket.

## API walkthrough

The HTTP walkthrough and required test cases are in [docs/TEST_SCENARIOS.md](docs/TEST_SCENARIOS.md). Example requests for all 38 endpoints are in [docs/requests.http](docs/requests.http).
