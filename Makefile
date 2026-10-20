PYTHON ?= python

all:
	$(PYTHON) -m build

test-python:
	$(PYTHON) -m pytest tests/python -v

test-rust:
	cargo test --workspace

test: test-python test-rust

.PHONY: docs
docs:
	mkdir -p docs/api
	$(PYTHON) -m sphinx.ext.apidoc -o docs/api -f -e src/python/pubby
	$(PYTHON) -m sphinx -b html docs docs/_build/html
