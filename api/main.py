from __future__ import annotations

import os
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware


BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=3.0))
    yield
    await client.aclose()
    client = None


app = FastAPI(title="Daylist API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "PUT", "OPTIONS"],
    allow_headers=["Content-Type"],
)


async def forward(method: str, path: str, body: Any = None) -> Any:
    if client is None:
        raise HTTPException(status_code=503, detail="API is starting.")
    try:
        request_options = {"json": body} if body is not None else {}
        upstream = await client.request(method, f"{BACKEND_URL}{path}", **request_options)
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail="Backend is not available yet.") from error
    if upstream.status_code >= 400:
        detail: Any
        try:
            detail = upstream.json().get("detail", upstream.text)
        except ValueError:
            detail = upstream.text
        raise HTTPException(status_code=upstream.status_code, detail=detail)
    return upstream.json()


@app.get("/health")
async def health() -> dict[str, Any]:
    backend = await forward("GET", "/health")
    return {"status": "ok", "service": "api", "backend": backend}


@app.get("/api/tasks")
async def list_tasks() -> list[dict[str, Any]]:
    return await forward("GET", "/tasks")


@app.put("/api/tasks/sync")
async def sync_tasks(request: Request) -> Response:
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Expected a JSON task array.") from error
    if not isinstance(payload, list):
        raise HTTPException(status_code=400, detail="Expected a JSON task array.")
    result = await forward("PUT", "/tasks/sync", payload)
    return Response(content=json.dumps(result), media_type="application/json")
