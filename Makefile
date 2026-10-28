.PHONY: up stop run migrate init-qdrant reconcile-qdrant seed-kb seed-demo seed-uc1 verify-uc1 \
        test lint format typecheck check clean \
        frontend-install frontend-run frontend-build frontend-test frontend-check \
        check-all

# --- Backing services (Docker) ---------------------------------------------- #

# Everything the backend dials, in one command. `depends_on` in the compose
# file only orders the *containerised* backend; a backend run from the host
# with `make run` starts nothing, so before this target the honest instruction
# was six service names a person had to remember and would not.
#
# All five, not just `db`. Missing `qdrant` is the one that bites hardest and
# latest: upload succeeds, the ingest job fails in the background, and the
# first sign of it is an agent answering "tài liệu không đề cập" about a
# document sitting right there. `reranker` down is quieter but not harmless —
# search degrades to the unthresholded RRF order (`src.services.reranker`), so
# results come back visibly worse with only a log line to say why.
#
# `--wait` blocks until every healthcheck is green rather than until Docker
# has merely started the containers: `reranker` loads a 2.2 GB cross-encoder
# before it answers, so "started" and "usable" are about two minutes apart on
# a cold cache and this target would otherwise hand back a prompt that lies.
up:
	docker compose up -d --wait db qdrant reranker mailhog

# `stop`, never `down`: `down` removes the containers, and with them anything
# not on a named volume. This just parks them.
stop:
	docker compose stop

# --- Backend (FastAPI) ------------------------------------------------------ #

# `--loop`: the checkpointer's async psycopg cannot run on Windows' default
# proactor loop — see `src/server_loop.py`. A no-op on Linux.
run:
	uvicorn src.main:app --reload --host 0.0.0.0 --port 8000 --loop src.server_loop:new_event_loop

migrate:
	alembic upgrade head

init-qdrant:
	python scripts/init_qdrant.py

# Report-only by default (RAG-09) — pass through `--apply` by hand
# (`python scripts/reconcile_qdrant.py --apply`) to actually delete what it
# finds, since `make`'s own argument-passing is awkward enough that a
# separate direct invocation is the honest way to opt into a delete.
reconcile-qdrant:
	python scripts/reconcile_qdrant.py

# RAG-17 (#103): loads seed_corpus/ into two demo Knowledge Bases. Depends on
# init-qdrant so a fresh environment only needs this one command — both
# targets are already idempotent, so running init-qdrant again here costs
# nothing on a machine where it already ran.
seed-kb: init-qdrant
	python scripts/seed_kb.py

# UC1-15 (#116): the Olist-shaped AR rows `v_ar_open`/`v_customer_safe` read.
# Without `--reset` this fails loudly on a conflict rather than duplicating the
# dataset — pass it by hand (`python scripts/seed_uc1_demo_data.py --reset`) to
# replace a previous run's rows, the same reason `reconcile-qdrant` keeps its
# own delete flag out of the target.
seed-demo:
	python scripts/seed_uc1_demo_data.py

# The one command after `pytest` has run: the suite resets the schema on this
# same database (`tests/conftest.py::_fresh_postgres_schema`, no SQLite
# fallback per issue #3), which empties `fin_invoice`/`customers`/`orders`/
# `crm_sales_assignment`. Kept as an alias of `seed-demo` since the UC-1
# pipeline's credentials went with it (DS-21).
seed-uc1: seed-demo

# UC1-14 (#115): proves the grants and row-level security the seeded data is
# read through. Rolls everything it writes back, so it is safe to run after
# `seed-uc1` to confirm the surface is intact.
verify-uc1:
	python scripts/verify_uc1_data_surface.py

# `reranker/tests/` is listed explicitly: the rerank service (RAG-03) is its
# own image with its own dependency set, so its tests live beside it rather
# than under `tests/`, and a target that only names `tests/` never runs them.
# They import `app` directly and stub the scorer, so this needs no torch.
test:
	pytest tests/ reranker/tests/ -v

lint:
	ruff check src/ tests/ reranker/

format:
	ruff format src/ tests/ reranker/

typecheck:
	mypy src/ reranker/

check: lint format test

# --- Frontend (React + Vite) ------------------------------------------------ #
# Its own toolchain in `frontend/`; nothing here reaches into the Python build.
#
# Two checks, and they answer different questions. The build is where a module
# that cannot resolve, or a component that cannot compile, shows up. The Vitest
# suite is where a component that compiles perfectly and does the wrong thing
# shows up — a Deploy button enabled on a draft, a 409's problem list dropped
# on the floor.
#
# There is still no `frontend-lint` / `frontend-typecheck`, because the app has
# no such scripts to call, and a target that shells out to a script that does
# not exist is a check that reports success by never running.

frontend-install:
	cd frontend && npm ci

frontend-run:
	cd frontend && npm run dev

frontend-build:
	cd frontend && npm run build

frontend-test:
	cd frontend && npm test

frontend-check: frontend-build frontend-test

# --- Everything ------------------------------------------------------------- #

check-all: check frontend-check

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
