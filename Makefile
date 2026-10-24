# Kinoloom - developer shortcuts (GNU make). The studio itself runs on
# Windows (start.bat); tests, lint and benchmarks also run elsewhere.
# `make benchmark` imports the studio modules from this tree directly (it is
# not sandboxed like the tests): run it on a development checkout.

PYTHON ?= python
PIP ?= $(PYTHON) -m pip

.PHONY: help install install-dev test test-quick benchmark lint typecheck clean run

help:
	@echo "Available targets:"
	@echo "  install       Install runtime dependencies"
	@echo "  install-dev   Install development and testing dependencies"
	@echo "  test          Run full pytest test suite"
	@echo "  test-quick    Run fast unit and regression tests"
	@echo "  benchmark     Run the studio subsystem benchmark suite"
	@echo "  lint          Run ruff code style and lint checks"
	@echo "  typecheck     Run mypy static type analysis"
	@echo "  clean         Clean cache directories and temporary build artifacts"
	@echo "  run           Launch the Kinoloom control room API server"

install:
	$(PIP) install -r tool/requirements.txt

install-dev:
	$(PIP) install -r tests/requirements-ci.txt mypy types-PyYAML types-psutil types-requests

test:
	$(PYTHON) -m pytest -v tests/

test-quick:
	$(PYTHON) -m pytest -q tests/unit

benchmark:
	$(PYTHON) -m benchmarks.run

lint:
	$(PYTHON) -m ruff check tool/ benchmarks/

typecheck:
	$(PYTHON) -m mypy tool/ benchmarks/

clean:
	$(PYTHON) -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
	$(PYTHON) -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('.pytest_cache')]"
	$(PYTHON) -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('.mypy_cache')]"
	$(PYTHON) -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('.ruff_cache')]"

run:
	$(PYTHON) tool/launcher.py
