.PHONY: install-local test wheel
install-local:
	bash scripts/install_local.sh

test:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q

wheel:
	.venv/bin/python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
