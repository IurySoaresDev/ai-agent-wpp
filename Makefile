.PHONY: install run dev lint typecheck test

install:
	uv sync --all-extras

run:
	uv run uvicorn ai_wpp.main:app --host $${HOST:-0.0.0.0} --port $${PORT:-8000}

dev:
	uv run uvicorn ai_wpp.main:app --reload --host 0.0.0.0 --port 8000

lint:
	uv run ruff check src

typecheck:
	uv run mypy src

test:
	uv run pytest -q
