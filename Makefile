SHELL := $(or $(shell which bash 2>/dev/null),/bin/sh)
.PHONY: help create-venv install-deps run test format lint-with-auto-fix extension

## Show available targets
help:
	@echo "Usage:"
	@echo "  make create-venv        Create the Python virtual environment"
	@echo "  make install-deps       Install Python dependencies"
	@echo "  make run                Start the JAME API server (http://localhost:8000)"
	@echo "  make test               Run test suite"
	@echo "  make format             Format the code using Ruff"
	@echo "  make lint-with-auto-fix Check code style and quality using Ruff (with auto-fixing)"
	@echo "  make extension          Install and compile the VS Code extension"

## Create a virtual environment
create-venv:
	@echo "Creating virtual environment..."
	@python3 -m venv venv
	@source venv/bin/activate && pip3 install --upgrade pip

## Install dependencies
install-deps:
	@echo "Installing dependencies..."
	@source venv/bin/activate && pip install -r requirements.txt

## Start the JAME API server
run:
	@echo "Starting JAME API server on http://localhost:8000 ..."
	@source venv/bin/activate && uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload --reload-dir api --reload-dir graph --reload-dir utils --reload-dir prompts --reload-dir integrations

## Run the test suite
test:
	@echo "Running tests..."
	@source venv/bin/activate && pytest test/ -v

## Format the code using Ruff
format:
	@echo "Formatting code with Ruff..."
	@source venv/bin/activate && ruff format .

## Check code style and quality using Ruff (with auto-fixing if possible)
lint-with-auto-fix:
	@echo "Running Ruff linter with auto-fix..."
	@source venv/bin/activate && ruff check . --fix

## Install and compile the VS Code extension
extension:
	@echo "Installing and compiling VS Code extension..."
	@cd extension && npm install && npm run compile
	@echo "Extension compiled. Load it with: code --extensionDevelopmentPath=$(PWD)/extension"
