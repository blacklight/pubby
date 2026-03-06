PYTHON ?= python

all:
	$(PYTHON) -m build

test:
	$(PYTHON) -m pytest tests/ -v

.PHONY: docs
docs:
	mkdir -p docs/api
	$(PYTHON) -m sphinx.ext.apidoc -o docs/api -f -e src/python/pubby
	$(PYTHON) -m sphinx -b html docs docs/_build/html
