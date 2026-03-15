#!/usr/bin/env python3
"""
PrusaDisconnect — Self-hosted Prusa Connect alternative
Talks to printers via PrusaLink local API (OpenAPI v1)
No cloud dependency. Runs on your LAN.
"""

import asyncio
import json
import time
import uuid
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / "data" / "prusa_disconnect.db"

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS printers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            api_key TEXT DEFAULT '',
            username TEXT DEFAULT 'maker',
            password TEXT DEFAULT '',
            printer_type TEXT DEFAULT '',
            added_at TEXT NOT NULL,
            last_seen TEXT,
            enabled INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS print_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id TEXT NOT NULL,
            file_name TEXT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            progress REAL DEFAULT 0,
            time_printing INTEGER DEFAULT 0,
            FOREIGN KEY (printer_id) REFERENCES printers(id)
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            data BLOB,
            FOREIGN KEY (printer_id) REFERENCES printers(id)
        );
    """)
    conn.close()

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

# ---------------------------------------------------------------------------
# PrusaLink Client — talks to each printer's local API
# ---------------------------------------------------------------------------
class PrusaLinkClient:
    """HTTP client for a single printer's PrusaLink v1 API."""

    def __init__(self, host: str, username: str = "", password: str = "",
                 api_key: str = ""):
        self.base = f"http://{host}"
        self.username = username
        self.password = password
        self.api_key = api_key

    def _auth(self) -> dict:
        """Build auth for httpx — digest auth or API key header."""
        return {}

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        return h

    def _client(self) -> httpx.AsyncClient:
        auth = None
        if self.username and self.password:
            auth = httpx.DigestAuth(self.username, self.password)
        return httpx.AsyncClient(
            base_url=self.base, headers=self._headers(),
            auth=auth, timeout=8.0, follow_redirects=True
        )

    async def get_version(self) -> dict:
        async with self._client() as c:
            r = await c.get("/api/version")
            r.raise_for_status()
            return r.json()

    async def get_info(self) -> dict:
        async with self._client() as c:
            r = await c.get("/api/v1/info")
            r.raise_for_status()
            return r.json()

    async def get_status(self) -> dict:
        async with self._client() as c:
            r = await c.get("/api/v1/status")
            r.raise_for_status()
            return r.json()

    async def get_job(self) -> Optional[dict]:
        async with self._client() as c:
            r = await c.get("/api/v1/job")
            if r.status_code == 204:
                return None
            r.raise_for_status()
            return r.json()

    async def get_storage(self) -> dict:
        async with self._client() as c:
            r = await c.get("/api/v1/storage")
            r.raise_for_status()
            return r.json()

    async def get_files(self, storage: str = "local", path: str = "") -> dict:
        async with self._client() as c:
            r = await c.get(f"/api/v1/files/{storage}/{path}")
            r.raise_for_status()
            return r.json()

    async def upload_file(self, storage: str, path: str, data: bytes,
                          print_after: bool = False) -> bool:
        async with self._client() as c:
            headers = {
                "Content-Type": "application/octet-stream",
                "Print-After-Upload": "?1" if print_after else "?0",
                "Overwrite": "?1",
            }
            r = await c.put(f"/api/v1/files/{storage}/{path}",
                            content=data, headers=headers)
            return r.status_code == 201

    async def start_print(self, storage: str, path: str) -> bool:
        async with self._client() as c:
            r = await c.post(f"/api/v1/files/{storage}/{path}")
            return r.status_code == 204

    async def pause_job(self, job_id: int) -> bool:
        async with self._client() as c:
            r = await c.put(f"/api/v1/job/{job_id}/pause")
            return r.status_code == 204

    async def resume_job(self, job_id: int) -> bool:
        async with self._client() as c:
            r = await c.put(f"/api/v1/job/{job_id}/resume")
            return r.status_code == 204

    async def stop_job(self, job_id: int) -> bool:
        async with self._client() as c:
            r = await c.delete(f"/api/v1/job/{job_id}")
            return r.status_code == 204

    async def delete_file(self, storage: str, path: str) -> bool:
        async with self._client() as c:
            r = await c.delete(f"/api/v1/files/{storage}/{path}")
            return r.status_code == 204

    async def get_camera_snap(self) -> Optional[bytes]:
        async with self._client() as c:
            r = await c.get("/api/v1/cameras/snap")
            if r.status_code == 200:
                return r.content
            return None

    async def get_thumbnail(self, thumb_path: str) -> Optional[bytes]:
        async with self._client() as c:
            r = await c.get(thumb_path)
            if r.status_code == 200:
                return r.content
            return None

# ---------------------------------------------------------------------------
# In-memory telemetry store (updated by background poller)
# ---------------------------------------------------------------------------
telemetry_store: dict[str, dict] = {}
printer_clients: dict[str, PrusaLinkClient] = {}
ws_clients: list[WebSocket] = []

async def broadcast_telemetry():
    """Push telemetry to all connected WebSocket clients."""
    data = json.dumps({"type": "telemetry", "printers": telemetry_store})
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)

async def poll_printer(printer_id: str, client: PrusaLinkClient, db_path: str):
    """Poll a single printer and update telemetry store."""
    try:
        status = await client.get_status()
        job = await client.get_job()
        telemetry_store[printer_id] = {
            "online": True,
            "status": status,
            "job": job,
            "last_poll": datetime.now(timezone.utc).isoformat(),
        }
        # Update last_seen in DB
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE printers SET last_seen = ? WHERE id = ?",
                      (datetime.now(timezone.utc).isoformat(), printer_id))
        conn.commit()
        conn.close()
    except Exception as e:
        telemetry_store[printer_id] = {
            "online": False,
            "error": str(e),
            "last_poll": datetime.now(timezone.utc).isoformat(),
        }

async def polling_loop():
    """Background task: poll all printers every 3 seconds."""
    while True:
        tasks = []
        for pid, client in printer_clients.items():
            tasks.append(poll_printer(pid, client, str(DB_PATH)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await broadcast_telemetry()
        await asyncio.sleep(3)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Load saved printers
    conn = get_db()
    for row in conn.execute("SELECT * FROM printers WHERE enabled = 1"):
        pid = row["id"]
        printer_clients[pid] = PrusaLinkClient(
            host=row["host"], username=row["username"],
            password=row["password"], api_key=row["api_key"]
        )
    conn.close()
    task = asyncio.create_task(polling_loop())
    yield
    task.cancel()

app = FastAPI(title="PrusaDisconnect", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class PrinterAdd(BaseModel):
    name: str
    host: str
    username: str = "maker"
    password: str = ""
    api_key: str = ""

class PrinterUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None
    enabled: Optional[bool] = None

# ---------------------------------------------------------------------------
# API: Printers CRUD
# ---------------------------------------------------------------------------
@app.get("/api/printers")
async def list_printers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM printers ORDER BY name").fetchall()
    conn.close()
    printers = []
    for r in rows:
        p = dict(r)
        p["telemetry"] = telemetry_store.get(r["id"], {"online": False})
        printers.append(p)
    return printers

@app.post("/api/printers")
async def add_printer(body: PrinterAdd):
    pid = str(uuid.uuid4())[:8]
    # Test connection first
    client = PrusaLinkClient(host=body.host, username=body.username,
                             password=body.password, api_key=body.api_key)
    try:
        version = await client.get_version()
    except Exception as e:
        raise HTTPException(400, f"Cannot reach printer at {body.host}: {e}")

    conn = get_db()
    conn.execute(
        "INSERT INTO printers (id, name, host, api_key, username, password, printer_type, added_at) VALUES (?,?,?,?,?,?,?,?)",
        (pid, body.name, body.host, body.api_key, body.username, body.password,
         version.get("text", ""), datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    printer_clients[pid] = client
    return {"id": pid, "version": version}

@app.put("/api/printers/{printer_id}")
async def update_printer(printer_id: str, body: PrinterUpdate):
    conn = get_db()
    row = conn.execute("SELECT * FROM printers WHERE id = ?", (printer_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Printer not found")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    for k, v in updates.items():
        conn.execute(f"UPDATE printers SET {k} = ? WHERE id = ?", (v, printer_id))
    conn.commit()
    # Reload client
    row = conn.execute("SELECT * FROM printers WHERE id = ?", (printer_id,)).fetchone()
    conn.close()
    if row["enabled"]:
        printer_clients[printer_id] = PrusaLinkClient(
            host=row["host"], username=row["username"],
            password=row["password"], api_key=row["api_key"]
        )
    else:
        printer_clients.pop(printer_id, None)
    return {"ok": True}

@app.delete("/api/printers/{printer_id}")
async def delete_printer(printer_id: str):
    conn = get_db()
    conn.execute("DELETE FROM printers WHERE id = ?", (printer_id,))
    conn.commit()
    conn.close()
    printer_clients.pop(printer_id, None)
    telemetry_store.pop(printer_id, None)
    return {"ok": True}

# ---------------------------------------------------------------------------
# API: Printer actions (proxy to PrusaLink)
# ---------------------------------------------------------------------------
@app.get("/api/printers/{printer_id}/status")
async def printer_status(printer_id: str):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404, "Printer not found or disabled")
    try:
        return await client.get_status()
    except Exception as e:
        raise HTTPException(502, str(e))

@app.get("/api/printers/{printer_id}/job")
async def printer_job(printer_id: str):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    try:
        job = await client.get_job()
        return job or {"status": "idle"}
    except Exception as e:
        raise HTTPException(502, str(e))

@app.post("/api/printers/{printer_id}/job/{job_id}/pause")
async def pause_job(printer_id: str, job_id: int):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    ok = await client.pause_job(job_id)
    return {"ok": ok}

@app.post("/api/printers/{printer_id}/job/{job_id}/resume")
async def resume_job(printer_id: str, job_id: int):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    ok = await client.resume_job(job_id)
    return {"ok": ok}

@app.delete("/api/printers/{printer_id}/job/{job_id}")
async def stop_job(printer_id: str, job_id: int):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    ok = await client.stop_job(job_id)
    return {"ok": ok}

@app.get("/api/printers/{printer_id}/files/{storage}")
async def list_files(printer_id: str, storage: str, path: str = ""):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    try:
        return await client.get_files(storage, path)
    except Exception as e:
        raise HTTPException(502, str(e))

@app.post("/api/printers/{printer_id}/files/{storage}/{path:path}")
async def upload_file(printer_id: str, storage: str, path: str,
                      file: UploadFile = File(...),
                      print_after: bool = Form(False)):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    data = await file.read()
    ok = await client.upload_file(storage, path or file.filename, data, print_after)
    return {"ok": ok}

@app.post("/api/printers/{printer_id}/print/{storage}/{path:path}")
async def start_print(printer_id: str, storage: str, path: str):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    ok = await client.start_print(storage, path)
    # Log to history
    if ok:
        conn = get_db()
        conn.execute(
            "INSERT INTO print_history (printer_id, file_name, started_at, status) VALUES (?,?,?,?)",
            (printer_id, path, datetime.now(timezone.utc).isoformat(), "PRINTING")
        )
        conn.commit()
        conn.close()
    return {"ok": ok}

@app.get("/api/printers/{printer_id}/storage")
async def printer_storage(printer_id: str):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    try:
        return await client.get_storage()
    except Exception as e:
        raise HTTPException(502, str(e))

@app.get("/api/printers/{printer_id}/camera/snap")
async def camera_snap(printer_id: str):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    data = await client.get_camera_snap()
    if data:
        return Response(content=data, media_type="image/png")
    raise HTTPException(204, "No snapshot available")

# ---------------------------------------------------------------------------
# API: Print history
# ---------------------------------------------------------------------------
@app.get("/api/history")
async def print_history(printer_id: Optional[str] = None, limit: int = 50):
    conn = get_db()
    if printer_id:
        rows = conn.execute(
            "SELECT h.*, p.name as printer_name FROM print_history h JOIN printers p ON h.printer_id = p.id WHERE h.printer_id = ? ORDER BY h.started_at DESC LIMIT ?",
            (printer_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT h.*, p.name as printer_name FROM print_history h JOIN printers p ON h.printer_id = p.id ORDER BY h.started_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# WebSocket: live telemetry
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        # Send current state immediately
        await websocket.send_text(json.dumps({
            "type": "telemetry", "printers": telemetry_store
        }))
        while True:
            # Keep connection alive; handle commands from UI
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        ws_clients.remove(websocket)
    except Exception:
        if websocket in ws_clients:
            ws_clients.remove(websocket)

# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------
FRONTEND_DIR = Path(__file__).parent / "frontend"

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return index.read_text()
    return "<h1>PrusaDisconnect</h1><p>Frontend not found. Place index.html in ./frontend/</p>"

@app.get("/assets/{path:path}")
async def serve_assets(path: str):
    fp = FRONTEND_DIR / "assets" / path
    if fp.exists():
        ct = "text/css" if path.endswith(".css") else "application/javascript"
        return Response(content=fp.read_bytes(), media_type=ct)
    raise HTTPException(404)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8484, reload=True)
