"""Tests for the JAME orchestrator API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


class TestHealth:
    """Tests for GET /health."""

    def test_returns_ok(self) -> None:
        """Health endpoint returns 200 with status ok."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestPostRuns:
    """Tests for POST /runs."""

    def test_returns_202_with_run_id(self) -> None:
        """A valid request returns HTTP 202 and a run_id string."""
        with patch(
            "api.service.OrchestratorService._run_pipeline",
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/runs",
                json={"user_request": "A simple todo list API"},
            )
        assert response.status_code == 202
        body = response.json()
        assert "run_id" in body
        assert isinstance(body["run_id"], str)
        assert len(body["run_id"]) == 36  # UUID4 format
        assert body["status"] == "pending"

    def test_missing_user_request_returns_422(self) -> None:
        """Omitting the required 'user_request' field returns HTTP 422."""
        response = client.post("/runs", json={})
        assert response.status_code == 422

    def test_user_request_too_short_returns_422(self) -> None:
        """user_request shorter than 5 chars is rejected with HTTP 422."""
        response = client.post("/runs", json={"user_request": "hi"})
        assert response.status_code == 422

    def test_max_iterations_boundary_too_high_returns_422(self) -> None:
        """max_iterations > 10 is rejected with HTTP 422."""
        response = client.post(
            "/runs",
            json={"user_request": "A todo app", "max_iterations": 99},
        )
        assert response.status_code == 422

    def test_max_iterations_boundary_too_low_returns_422(self) -> None:
        """max_iterations < 1 is rejected with HTTP 422."""
        response = client.post(
            "/runs",
            json={"user_request": "A todo app", "max_iterations": 0},
        )
        assert response.status_code == 422


class TestGetRun:
    """Tests for GET /runs/{run_id}."""

    def test_unknown_run_id_returns_404(self) -> None:
        """Requesting an unknown run_id returns HTTP 404."""
        response = client.get("/runs/nonexistent-run-id")
        assert response.status_code == 404
        assert "nonexistent-run-id" in response.json()["detail"]

    def test_known_run_returns_status(self) -> None:
        """A known run_id returns status and metadata."""
        with patch(
            "api.service.OrchestratorService._run_pipeline",
            new_callable=AsyncMock,
        ):
            create_resp = client.post(
                "/runs",
                json={"user_request": "A simple API"},
            )
        assert create_resp.status_code == 202
        run_id = create_resp.json()["run_id"]

        status_resp = client.get(f"/runs/{run_id}")
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["run_id"] == run_id
        assert body["status"] in (
            "pending",
            "running",
            "succeeded",
            "failed",
            "cancelled",
        )


class TestCancelRun:
    """Tests for POST /runs/{run_id}/cancel."""

    def test_unknown_run_id_returns_404(self) -> None:
        """Cancelling an unknown run_id returns HTTP 404."""
        response = client.post("/runs/nonexistent-run-id/cancel")
        assert response.status_code == 404


class TestJuniorMode:
    """Tests for the junior_mode field on POST /runs."""

    def test_junior_mode_defaults_to_false(self) -> None:
        """Omitting junior_mode defaults to False and the run is still accepted."""
        with patch(
            "api.service.OrchestratorService._run_pipeline",
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/runs",
                json={"user_request": "A simple todo list API"},
            )
        assert response.status_code == 202
        assert "run_id" in response.json()

    def test_junior_mode_true_is_accepted(self) -> None:
        """Passing junior_mode=true returns HTTP 202 and a valid run_id."""
        with patch(
            "api.service.OrchestratorService._run_pipeline",
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/runs",
                json={
                    "user_request": "A simple todo list API",
                    "junior_mode": True,
                },
            )
        assert response.status_code == 202
        body = response.json()
        assert "run_id" in body
        assert isinstance(body["run_id"], str)
        assert len(body["run_id"]) == 36  # UUID4 format

    def test_junior_mode_false_is_accepted(self) -> None:
        """Passing junior_mode=false is explicitly accepted."""
        with patch(
            "api.service.OrchestratorService._run_pipeline",
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/runs",
                json={
                    "user_request": "A simple todo list API",
                    "junior_mode": False,
                },
            )
        assert response.status_code == 202

    def test_junior_mode_invalid_type_returns_422(self) -> None:
        """Passing a non-boolean, non-coercible junior_mode returns HTTP 422."""
        response = client.post(
            "/runs",
            json={
                "user_request": "A simple todo list API",
                "junior_mode": {"nested": "object"},
            },
        )
        assert response.status_code == 422
