.PHONY: help install test lint format run verify docker-build

PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
VENV ?= .venv

help:
	@echo "make install   - create .venv and install dev dependencies"
	@echo "make test      - run the pytest suite"
	@echo "make lint      - run ruff"
	@echo "make format    - format the Python sources"
	@echo "make run       - run LoseSound locally on port 8092"
	@echo "make verify    - read-only check against a running installation"

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .

run:
	PYTHONPATH=src CONFIG_PATH=./data/config.json LISTEN_PORT=8092 $(PYTHON) -m losesound

verify:
	$(PYTHON) scripts/verify.py

docker-build:
	docker build -t losesound:dev .
