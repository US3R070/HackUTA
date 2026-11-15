"""Throwaway FastAPI echo server for testing the HTTP sink locally.

  uvicorn escalation_mcp.dev_echo_server:app --port 8787
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, Request

app = FastAPI(title="escalation-echo")
RECEIVED: list[dict[str, Any]] = []


@app.post("/escalations")
async def create_escalation(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    body = await request.json()
    RECEIVED.append({"path": "/escalations", "auth": authorization, "body": body})
    return {"ok": True, "incident_id": body.get("incident_id")}


@app.post("/escalations/{incident_id}/report")
async def report(
    incident_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.json()
    RECEIVED.append(
        {
            "path": f"/escalations/{incident_id}/report",
            "auth": authorization,
            "body": body,
        }
    )
    return {"ok": True}


@app.post("/escalations/{incident_id}/events")
async def events(
    incident_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.json()
    RECEIVED.append(
        {
            "path": f"/escalations/{incident_id}/events",
            "auth": authorization,
            "body": body,
        }
    )
    return {"ok": True}


@app.get("/_received")
async def received() -> list[dict[str, Any]]:
    return RECEIVED
