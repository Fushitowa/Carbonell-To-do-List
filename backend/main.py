from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://daylist:daylist_local_dev_only@127.0.0.1:5432/daylist",
)
ALLOWED_COLORS = {"yellow", "mint", "rose", "peach", "sky", "lilac"}


class TaskItem(BaseModel):
    id: UUID
    title: str = Field(min_length=1, max_length=160)
    done: bool = False
    createdAt: int
    dueAt: str = ""
    reminded: bool = False
    notes: str = ""
    color: str = ""


def initialize_database() -> None:
    with psycopg.connect(DATABASE_URL, connect_timeout=5) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id UUID PRIMARY KEY,
                title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 160),
                done BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL,
                due_at TIMESTAMP WITHOUT TIME ZONE,
                reminded BOOLEAN NOT NULL DEFAULT FALSE,
                notes TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '' CHECK (color = '' OR color IN ('yellow', 'mint', 'rose', 'peach', 'sky', 'lilac'))
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS tasks_created_at_idx ON tasks (created_at DESC)")


def wait_for_database() -> None:
    last_error: Exception | None = None
    for _ in range(90):
        try:
            initialize_database()
            return
        except psycopg.OperationalError as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"PostgreSQL did not become ready: {last_error}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    wait_for_database()
    yield


app = FastAPI(title="Daylist Backend", version="1.0.0", lifespan=lifespan)


def task_from_row(row: dict[str, Any]) -> dict[str, Any]:
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    due_at = row["due_at"].isoformat(timespec="minutes") if row["due_at"] else ""
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "done": row["done"],
        "createdAt": int(created_at.timestamp() * 1000),
        "dueAt": due_at,
        "reminded": row["reminded"],
        "notes": row["notes"],
        "color": row["color"],
    }


def normalize_due_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="dueAt must be an ISO date and time.") from error
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


@app.get("/health")
def health() -> dict[str, str]:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error as error:
        raise HTTPException(status_code=503, detail="Database is not ready.") from error
    return {"status": "ok", "service": "backend", "database": "connected"}


@app.get("/tasks")
def list_tasks() -> list[dict[str, Any]]:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5, row_factory=dict_row) as connection:
            rows = connection.execute(
                "SELECT id, title, done, created_at, due_at, reminded, notes, color FROM tasks ORDER BY created_at DESC"
            ).fetchall()
        return [task_from_row(row) for row in rows]
    except psycopg.Error as error:
        raise HTTPException(status_code=503, detail="Database query failed.") from error


@app.put("/tasks/sync")
def sync_tasks(tasks: list[TaskItem]) -> dict[str, int]:
    rows = []
    for task in tasks:
        if task.color and task.color not in ALLOWED_COLORS:
            raise HTTPException(status_code=422, detail=f"Unsupported task color: {task.color}")
        rows.append((
            task.id,
            task.title.strip(),
            task.done,
            datetime.fromtimestamp(task.createdAt / 1000, tz=timezone.utc),
            normalize_due_date(task.dueAt),
            task.reminded,
            task.notes,
            task.color,
        ))
        if not task.title.strip():
            raise HTTPException(status_code=422, detail="Task title cannot be blank.")

    try:
        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                if rows:
                    cursor.executemany(
                        """
                        INSERT INTO tasks (id, title, done, created_at, due_at, reminded, notes, color)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            title = EXCLUDED.title,
                            done = EXCLUDED.done,
                            created_at = EXCLUDED.created_at,
                            due_at = EXCLUDED.due_at,
                            reminded = EXCLUDED.reminded,
                            notes = EXCLUDED.notes,
                            color = EXCLUDED.color
                        """,
                        rows,
                    )
                    task_ids = [row[0] for row in rows]
                    cursor.execute("DELETE FROM tasks WHERE NOT (id = ANY(%s::uuid[]))", (task_ids,))
                else:
                    cursor.execute("DELETE FROM tasks")
        return {"saved": len(rows)}
    except psycopg.Error as error:
        raise HTTPException(status_code=503, detail="Could not save tasks to PostgreSQL.") from error
