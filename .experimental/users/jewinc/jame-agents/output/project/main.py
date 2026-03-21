"""Minimal Todo API — verifies all infrastructure connections on startup."""

from __future__ import annotations

import os

import psycopg2
import redis as redis_lib
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(title="Todo App", version="0.1.0")

# ── Connection helpers ────────────────────────────────────────────────────────

def _pg_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _redis_client():
    return redis_lib.from_url(os.environ["REDIS_URL"])


# ── Startup: create table if needed ──────────────────────────────────────────

@app.on_event("startup")
def startup() -> None:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id   SERIAL PRIMARY KEY,
                text TEXT NOT NULL,
                done BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )
        conn.commit()


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    """Liveness probe — checks postgres and redis reachability."""
    errors: list[str] = []

    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:
        errors.append(f"postgres: {exc}")

    try:
        _redis_client().ping()
    except Exception as exc:
        errors.append(f"redis: {exc}")

    if errors:
        raise HTTPException(status_code=503, detail=errors)
    return {"status": "ok"}


# ── Todo CRUD ─────────────────────────────────────────────────────────────────

class TodoIn(BaseModel):
    text: str


class Todo(BaseModel):
    id: int
    text: str
    done: bool


@app.get("/todos", response_model=list[Todo])
def list_todos() -> list[Todo]:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, text, done FROM todos ORDER BY id")
        return [Todo(id=row[0], text=row[1], done=row[2]) for row in cur.fetchall()]


@app.post("/todos", response_model=Todo, status_code=201)
def create_todo(body: TodoIn) -> Todo:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO todos (text) VALUES (%s) RETURNING id, text, done",
            (body.text,),
        )
        conn.commit()
        row = cur.fetchone()
        return Todo(id=row[0], text=row[1], done=row[2])


@app.patch("/todos/{todo_id}", response_model=Todo)
def toggle_todo(todo_id: int) -> Todo:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE todos SET done = NOT done WHERE id = %s RETURNING id, text, done",
            (todo_id,),
        )
        conn.commit()
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Todo not found")
        return Todo(id=row[0], text=row[1], done=row[2])


@app.delete("/todos/{todo_id}", status_code=204)
def delete_todo(todo_id: int) -> None:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM todos WHERE id = %s RETURNING id", (todo_id,))
        conn.commit()
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Todo not found")
