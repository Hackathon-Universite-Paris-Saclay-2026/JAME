.PHONY: help create-venv install install-dev format lint-with-auto-fix lint-with-auto-unsafe-fix test check

help:
	@echo "Available targets:"
	@echo "  create-venv   Create a new virtual environment"
	@echo "  install       Install runtime dependencies"
	@echo "  install-dev   Install development dependencies"
	@echo "  format        Format code with ruff"
	@echo "  lint-with-auto-fix  Lint code with auto-fix"
	@echo "  lint-with-auto-unsafe-fix  Lint code with auto-fix (unsafe)"
	@echo "  test          Run tests"
	@echo "  check         Run lint and test"

create-venv:
	@echo "Creating virtual environment..."
	python3 -m venv venv

install:
	@echo "Installing runtime dependencies..."
	@source venv/bin/activate && pip install -r requirements.txt

install-dev:
	@echo "Installing development dependencies..."
	@source venv/bin/activate && pip install -r requirements-dev.txt

format:
	@echo "Formatting code..."
	@source venv/bin/activate && ruff format .

lint-with-auto-fix:
	@echo "Linting code with auto-fix..."
	@source venv/bin/activate && ruff check . --fix

lint-with-auto-unsafe-fix:
	@echo "Linting code with auto-fix (unsafe)..."
	@source venv/bin/activate && ruff check . --fix --unsafe-fixes

test:
	@echo "Running tests..."
	@source venv/bin/activate && pytest --tb=short

check:
	@echo "Running lint and test..."
	$(MAKE) lint-with-auto-fix
	$(MAKE) test