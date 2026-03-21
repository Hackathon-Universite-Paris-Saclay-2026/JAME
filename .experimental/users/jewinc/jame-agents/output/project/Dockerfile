# ── Stage 1: builder ──────────────────────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /app
RUN python -m venv /venv
COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

# ── Stage 2: final ────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app
# Install curl for the HEALTHCHECK before switching user
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
# Copy the venv (contains all installed packages) from builder
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"
# Copy source code
COPY . .
# Create non-root user and take ownership AFTER copying files
RUN useradd -ms /bin/bash appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl --fail http://localhost:8000/health || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]