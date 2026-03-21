backend-install:
	cd backend && python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

backend-run:
	cd backend && . .venv/bin/activate && uvicorn api.app:app --reload --host 0.0.0.0 --port 8000

backend-test:
	cd backend && . .venv/bin/activate && pytest

extension-install:
	cd extension && npm install

extension-build:
	cd extension && npm run compile
