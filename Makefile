.PHONY: default help venv install run worker worker-io worker-llm \
        celery-status celery-tasks celery-purge celery-flower \
        test test-fast test-integration test-cov \
        format lint check clean

# === Defaults (override in Makefile.local) ============================
PY        ?= python3.12
VENV      ?= .venv
BIN       := $(VENV)/bin

HOST      ?= 0.0.0.0
PORT      ?= 8000

CELERY_APP        ?= app.celery_app.celery_app
CELERY_LOGLEVEL   ?= INFO
CELERY_CONCURRENCY_IO  ?= 4
CELERY_CONCURRENCY_LLM ?= 2
FLOWER_PORT       ?= 5555

-include Makefile.local

default: help

# === Setup ============================================================

venv:
	$(PY) -m venv $(VENV)

install: venv
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt

# === Run ==============================================================

run:
	$(BIN)/uvicorn app.main:app --host $(HOST) --port $(PORT) --reload

# Dev-mode worker: consumes ALL queues in a single process.
worker:
	$(BIN)/celery -A $(CELERY_APP) worker \
		--loglevel=$(CELERY_LOGLEVEL) \
		--queues=io,llm \
		--concurrency=2 \
		--hostname=all@%h

# Production-like: split workers per queue so I/O-bound and LLM-bound
# tasks scale independently (convert/parse_invoice vs anonymize/extract).
worker-io:
	$(BIN)/celery -A $(CELERY_APP) worker \
		--loglevel=$(CELERY_LOGLEVEL) \
		--queues=io \
		--concurrency=$(CELERY_CONCURRENCY_IO) \
		--hostname=io@%h

worker-llm:
	$(BIN)/celery -A $(CELERY_APP) worker \
		--loglevel=$(CELERY_LOGLEVEL) \
		--queues=llm \
		--concurrency=$(CELERY_CONCURRENCY_LLM) \
		--hostname=llm@%h

# === Celery diagnostics ===============================================

celery-status:
	$(BIN)/celery -A $(CELERY_APP) inspect ping

celery-tasks:
	$(BIN)/celery -A $(CELERY_APP) inspect active

celery-purge:
	$(BIN)/celery -A $(CELERY_APP) purge -f

celery-flower:
	$(BIN)/celery -A $(CELERY_APP) flower --port=$(FLOWER_PORT)

# === Tests ============================================================

test:
	$(BIN)/pytest -q

test-fast:
	$(BIN)/pytest -x --tb=short -m "not integration and not slow"

test-integration:
	$(BIN)/pytest -v -m "integration"

test-cov:
	$(BIN)/pytest --cov=app --cov-report=html --cov-report=term

# === Code quality =====================================================

format:
	$(BIN)/ruff format app tests main.py
	$(BIN)/ruff check --fix app tests main.py

lint:
	$(BIN)/ruff check app tests main.py

check:
	$(BIN)/ruff format --check app tests main.py
	$(BIN)/ruff check app tests main.py

# === Housekeeping =====================================================

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage

# === Help =============================================================

help:
	@echo "Setup:"
	@echo "  make venv            — create virtualenv in $(VENV) ($(PY))"
	@echo "  make install         — install requirements into $(VENV)"
	@echo ""
	@echo "Run:"
	@echo "  make run             — uvicorn on http://$(HOST):$(PORT) (reload)"
	@echo "  make worker          — single Celery worker, all queues (dev)"
	@echo "  make worker-io       — Celery worker, queue=io (convert, parse_invoice)"
	@echo "  make worker-llm      — Celery worker, queue=llm (anonymize, extract)"
	@echo ""
	@echo "Celery diagnostics:"
	@echo "  make celery-status   — ping all workers"
	@echo "  make celery-tasks    — list active tasks"
	@echo "  make celery-purge    — purge all queues (-f)"
	@echo "  make celery-flower   — Flower UI on :$(FLOWER_PORT)"
	@echo ""
	@echo "Tests:"
	@echo "  make test            — pytest (all)"
	@echo "  make test-fast       — exclude integration/slow markers"
	@echo "  make test-integration — only integration-marked tests"
	@echo "  make test-cov        — coverage (html + term)"
	@echo ""
	@echo "Code quality (ruff):"
	@echo "  make format          — ruff format + ruff check --fix"
	@echo "  make lint            — ruff check"
	@echo "  make check           — ruff format --check + ruff check (no write)"
	@echo ""
	@echo "Housekeeping:"
	@echo "  make clean           — drop caches"
	@echo ""
	@echo "Personal overrides: create Makefile.local (see Makefile.local.example)."
