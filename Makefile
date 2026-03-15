.PHONY: help create-venv install-deps format lint-with-auto-fix

## Show available targets
help:
	@echo "Usage:"
	@echo "  make create-venv  Create the Python virtual environment"
	@echo "  make install-deps Install Python dependencies"
	@echo "  make format    Format the code using Ruff"
	@echo "  make lint-with-auto-fix  Check code style and quality using Ruff (with auto-fixing if possible)"

## Create a virtual environment
create-venv:
	@echo "Creating virtual environment..."
	@python3 -m venv venv
	@source venv/bin/activate && pip3 install --upgrade pip

## Install dependencies
install-deps:
	@echo "Installing dependencies..."
	@source venv/bin/activate && pip install -r requirements.txt

## Format the code using Ruff
format:
	@echo "Formatting code with Ruff..."
	@source venv/bin/activate && ruff format .

## Check code style and quality using Ruff (with auto-fixing if possible)
lint-with-auto-fix:
	@echo "Running Ruff linter with auto-fix..."
	@source venv/bin/activate && ruff check . --fix