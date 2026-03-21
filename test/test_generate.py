"""Tests for the generation API endpoints (POST /generate, GET /stream/{id})."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


class TestPostGenerate:
    """Tests for POST /api/generate."""

    def test_returns_202_with_job_id(self) -> None:
        """A valid request returns HTTP 202 and a job_id string."""
        with patch("api.generate._run_graph", new_callable=AsyncMock):
            response = client.post(
                "/api/generate",
                json={"prompt": "A simple todo list API"},
            )
        assert response.status_code == 202
        body = response.json()
        assert "job_id" in body
        assert isinstance(body["job_id"], str)
        assert len(body["job_id"]) == 36  # UUID4 format

    def test_missing_prompt_returns_422(self) -> None:
        """Omitting the required 'prompt' field returns HTTP 422."""
        response = client.post("/api/generate", json={})
        assert response.status_code == 422

    def test_max_iterations_boundary_too_high_returns_422(self) -> None:
        """max_iterations > 5 is rejected with HTTP 422."""
        response = client.post(
            "/api/generate",
            json={"prompt": "App", "max_iterations": 99},
        )
        assert response.status_code == 422

    def test_max_iterations_boundary_too_low_returns_422(self) -> None:
        """max_iterations < 1 is rejected with HTTP 422."""
        response = client.post(
            "/api/generate",
            json={"prompt": "App", "max_iterations": 0},
        )
        assert response.status_code == 422


class TestGetStreamTraces:
    """Tests for GET /api/stream/{job_id}."""

    def test_unknown_job_id_returns_404(self) -> None:
        """Requesting an unknown job_id returns HTTP 404."""
        response = client.get("/api/stream/nonexistent-job-id")
        assert response.status_code == 404
        assert "nonexistent-job-id" in response.json()["detail"]

    def test_known_job_returns_event_stream(self) -> None:
        """A job that completes immediately returns a text/event-stream response."""
        from api.generate import _jobs

        job_id = "test-job-stream"
        _jobs[job_id] = {
            "status": "done",
            "traces": [
                {"agent": "pm", "phase": "plan", "content": "Planning…"}
            ],
            "result": {"code_files": [], "reasoning_logs": []},
        }

        response = client.get(
            f"/api/stream/{job_id}", headers={"Accept": "text/event-stream"}
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        # Clean up
        del _jobs[job_id]
