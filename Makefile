.PHONY: format lint typecheck test check

format:
	ruff format .

lint:
	ruff check .

typecheck:
	mypy

test:
	pytest

check:
	ruff format --check .
	ruff check .
	mypy
	pytest
