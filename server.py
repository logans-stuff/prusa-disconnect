#!/usr/bin/env python3
"""
PrusaDisconnect — Self-hosted Prusa Connect alternative
Talks to printers via PrusaLink local API (OpenAPI v1)
No cloud dependency. Runs on your LAN.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
import os
import sqlite3
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, List

log = logging.getLogger("prusadisconnect")

import httpx
from fastapi import (FastAPI, HTTPException, UploadFile, File, Form, Query,
                     WebSocket, WebSocketDisconnect, Request)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / "data" / "prusa_disconnect.db"
GCODE_DIR = Path(__file__).parent / "data" / "gcodes"

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    GCODE_DIR.mkdir(parents=True, exist_ok=True)
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
            camera_url TEXT DEFAULT '',
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
        CREATE TABLE IF NOT EXISTS gcodes (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            display_name TEXT NOT NULL,
            size INTEGER DEFAULT 0,
            uploaded_at TEXT NOT NULL,
            checksum TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS print_queue (
            id TEXT PRIMARY KEY,
            printer_id TEXT NOT NULL,
            gcode_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            status TEXT DEFAULT 'queued',
            added_at TEXT NOT NULL,
            started_at TEXT,
            FOREIGN KEY (printer_id) REFERENCES printers(id),
            FOREIGN KEY (gcode_id) REFERENCES gcodes(id)
        );
    """)
    # Add columns if missing (upgrade path)
    for col, default in [("camera_url", "''"), ("discord_webhook", "''")]:
        try:
            conn.execute(f"SELECT {col} FROM printers LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE printers ADD COLUMN {col} TEXT DEFAULT {default}")
    conn.commit()
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

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        return h

    def _client(self, timeout: float = 8.0) -> httpx.AsyncClient:
        auth = None
        if self.username and self.password:
            auth = httpx.DigestAuth(self.username, self.password)
        return httpx.AsyncClient(
            base_url=self.base, headers=self._headers(),
            auth=auth, timeout=timeout, follow_redirects=True
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

    async def get_default_storage(self) -> str:
        """Return the first available writable storage name (usb, local, etc.)."""
        try:
            info = await self.get_storage()
            for name, meta in info.get("storage_list", {}).items():
                if meta.get("available", False):
                    return name
        except Exception:
            pass
        return "usb"

    async def get_files(self, storage: str = "local", path: str = "") -> dict:
        async with self._client() as c:
            r = await c.get(f"/api/v1/files/{storage}/{path}")
            r.raise_for_status()
            return r.json()

    async def upload_file(self, storage: str, path: str, data: bytes,
                          print_after: bool = False) -> dict:
        """Upload file to printer. Returns {"ok": bool, "status": int, "detail": str}."""
        async with self._client(timeout=120.0) as c:
            # Pre-flight GET to prime digest auth
            try:
                pfr = await c.get("/api/v1/info")
                log.info("Pre-flight GET /api/v1/info → %d", pfr.status_code)
            except Exception as exc:
                log.warning("Pre-flight GET failed: %s", exc)
            ct = "application/gcode+binary" if path.endswith(".bgcode") else "text/x.gcode"
            headers = {
                "Content-Type": ct,
                "Print-After-Upload": "?1" if print_after else "?0",
                "Overwrite": "?1",
            }
            encoded_path = quote(path, safe="")
            r = await c.put(f"/api/v1/files/{storage}/{encoded_path}",
                            content=data, headers=headers)
            ok = r.status_code in (201, 204)
            detail = ""
            if not ok:
                try:
                    detail = r.text
                except Exception:
                    detail = f"HTTP {r.status_code}"
                log.warning("Upload to %s/%s/%s returned %d: %s",
                            self.base, storage, path, r.status_code, detail)
            else:
                log.info("Upload to %s/%s/%s OK (%d)", self.base, storage, path, r.status_code)
            return {"ok": ok, "status": r.status_code, "detail": detail}

    async def start_print(self, storage: str, path: str) -> bool:
        async with self._client() as c:
            r = await c.post(f"/api/v1/files/{storage}/{quote(path, safe='')}")
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
        async with self._client(timeout=5.0) as c:
            try:
                r = await c.get("/api/v1/cameras/snap")
                if r.status_code == 200:
                    return r.content
            except Exception:
                pass
            return None

    async def get_thumbnail(self, thumb_path: str) -> Optional[bytes]:
        async with self._client() as c:
            r = await c.get(thumb_path)
            if r.status_code == 200:
                return r.content
            return None

# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------
telemetry_store: dict[str, dict] = {}
camera_store: dict[str, bytes] = {}  # latest snapshot per printer
printer_clients: dict[str, PrusaLinkClient] = {}
printer_camera_urls: dict[str, str] = {}  # external camera URLs
printer_webhooks: dict[str, str] = {}  # discord webhook URLs per printer
printer_names: dict[str, str] = {}  # printer names for notifications
discord_last_progress: dict[str, int] = {}  # last notified 5% bracket per printer
discord_last_state: dict[str, str] = {}  # last notified state per printer
ws_clients: list[WebSocket] = []

async def broadcast_telemetry():
    """Push telemetry to all connected WebSocket clients."""
    # Include camera availability in telemetry
    cam_available = {pid: pid in camera_store for pid in telemetry_store}
    data = json.dumps({
        "type": "telemetry",
        "printers": telemetry_store,
        "cameras": cam_available,
    })
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

async def poll_camera(printer_id: str, client: PrusaLinkClient):
    """Grab a camera snapshot from the printer or external URL."""
    try:
        # Try external camera URL first
        ext_url = printer_camera_urls.get(printer_id, "")
        if ext_url:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(ext_url)
                if r.status_code == 200:
                    camera_store[printer_id] = r.content
                    return
        # Fall back to PrusaLink camera API
        snap = await client.get_camera_snap()
        if snap:
            camera_store[printer_id] = snap
    except Exception:
        pass

async def send_discord_notification(printer_id: str, printer_name: str,
                                     webhook_url: str, title: str,
                                     description: str, color: int,
                                     fields: list = None):
    """Send a Discord webhook embed, optionally with a camera snapshot."""
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"PrusaDisconnect \u2022 {printer_name}"},
    }
    if fields:
        embed["fields"] = fields

    snap = camera_store.get(printer_id)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            if snap:
                # Multipart: embed JSON + image file
                embed["image"] = {"url": "attachment://snapshot.jpg"}
                payload = {"payload_json": json.dumps({"embeds": [embed]})}
                ext = "jpg" if snap[:2] == b'\xff\xd8' else "png"
                ct = "image/jpeg" if ext == "jpg" else "image/png"
                files = {"file": (f"snapshot.{ext}", snap, ct)}
                await c.post(webhook_url, data=payload, files=files)
            else:
                await c.post(webhook_url, json={"embeds": [embed]})
    except Exception:
        pass  # Don't let webhook failures break polling

async def check_discord_notifications(printer_id: str):
    """Check if we need to send a Discord notification for this printer."""
    webhook = printer_webhooks.get(printer_id, "")
    if not webhook:
        return
    name = printer_names.get(printer_id, printer_id)
    t = telemetry_store.get(printer_id, {})
    if not t.get("online"):
        return

    state = (t.get("status", {}).get("printer", {}).get("state", "")).lower()
    job = t.get("job") or {}
    progress = job.get("progress", 0)
    file_name = (job.get("file", {}) or {}).get("display_name") or (job.get("file", {}) or {}).get("name", "")
    time_remaining = job.get("time_remaining")
    time_printing = job.get("time_printing")

    prev_state = discord_last_state.get(printer_id, "")

    # Print started
    if state == "printing" and prev_state not in ("printing", "paused"):
        discord_last_progress[printer_id] = 0
        discord_last_state[printer_id] = state
        await send_discord_notification(
            printer_id, name, webhook,
            "\U0001F7E2 Print Started",
            f"**{file_name}**" if file_name else "New print job started",
            0x4ade80,  # green
        )
        return

    # Print finished
    if state in ("finished", "idle", "ready") and prev_state == "printing":
        discord_last_state[printer_id] = state
        discord_last_progress.pop(printer_id, None)
        fields = []
        if time_printing:
            h = int(time_printing) // 3600
            m = (int(time_printing) % 3600) // 60
            fields.append({"name": "Print Time", "value": f"{h}h {m}m", "inline": True})
        await send_discord_notification(
            printer_id, name, webhook,
            "\u2705 Print Complete",
            f"**{file_name}**" if file_name else "Print job finished",
            0x4ade80,  # green
            fields=fields,
        )
        return

    # Print failed / error
    if state in ("error", "attention") and prev_state != state:
        discord_last_state[printer_id] = state
        await send_discord_notification(
            printer_id, name, webhook,
            "\u26A0\uFE0F Printer Error",
            f"Printer entered **{state}** state" + (f"\nFile: **{file_name}**" if file_name else ""),
            0xf87171,  # red
        )
        return

    discord_last_state[printer_id] = state

    # Progress update every 5%
    if state == "printing" and progress > 0:
        current_bracket = int(progress // 5) * 5
        last_bracket = discord_last_progress.get(printer_id, 0)
        if current_bracket > last_bracket:
            discord_last_progress[printer_id] = current_bracket
            fields = [
                {"name": "Progress", "value": f"{progress:.1f}%", "inline": True},
            ]
            if time_remaining:
                h = int(time_remaining) // 3600
                m = (int(time_remaining) % 3600) // 60
                fields.append({"name": "Remaining", "value": f"{h}h {m}m", "inline": True})
            if time_printing:
                h = int(time_printing) // 3600
                m = (int(time_printing) % 3600) // 60
                fields.append({"name": "Elapsed", "value": f"{h}h {m}m", "inline": True})
            # Progress bar visual
            filled = int(current_bracket / 5)
            bar = "\u2588" * filled + "\u2591" * (20 - filled)
            await send_discord_notification(
                printer_id, name, webhook,
                f"\U0001F5A8\uFE0F Printing \u2014 {current_bracket}%",
                f"**{file_name}**\n`{bar}` {progress:.1f}%" if file_name else f"`{bar}` {progress:.1f}%",
                0xe8793a,  # accent orange
                fields=fields,
            )

_poll_counter = 0

async def polling_loop():
    """Background task: poll all printers every 3 seconds, cameras every 5 cycles (15s)."""
    global _poll_counter
    while True:
        tasks = []
        for pid, client in list(printer_clients.items()):
            tasks.append(poll_printer(pid, client, str(DB_PATH)))
            # Poll cameras less frequently
            if _poll_counter % 5 == 0:
                tasks.append(poll_camera(pid, client))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await broadcast_telemetry()
            # Check Discord notifications after telemetry is updated
            for pid in list(printer_clients.keys()):
                await check_discord_notifications(pid)
        # Process print queue
        await process_queue()
        _poll_counter += 1
        await asyncio.sleep(3)

_queue_processing: set = set()  # printers currently being sent a queue job

async def process_queue():
    """Check each printer's queue — if idle/ready and queue has items, start next print."""
    conn = get_db()
    for pid, client in list(printer_clients.items()):
        if pid in _queue_processing:
            continue
        t = telemetry_store.get(pid, {})
        if not t.get("online"):
            continue
        state = (t.get("status", {}).get("printer", {}).get("state", "")).lower()
        # Only start queue jobs when printer is truly idle/ready — not "finished"
        # (finished means the last print plate hasn't been cleared yet)
        if state not in ("idle", "ready"):
            continue
        # Skip if printer already has an active queue item
        active = conn.execute(
            "SELECT 1 FROM print_queue WHERE printer_id = ? AND status = 'printing' LIMIT 1",
            (pid,)
        ).fetchone()
        if active:
            continue
        # Get next queued item
        row = conn.execute(
            "SELECT q.*, g.filename FROM print_queue q JOIN gcodes g ON q.gcode_id = g.id "
            "WHERE q.printer_id = ? AND q.status = 'queued' ORDER BY q.position LIMIT 1",
            (pid,)
        ).fetchone()
        if not row:
            continue
        qid = row["id"]
        gcode_id = row["gcode_id"]
        filename = row["filename"]
        gcode_path = GCODE_DIR / gcode_id
        if not gcode_path.exists():
            conn.execute("UPDATE print_queue SET status = 'error' WHERE id = ?", (qid,))
            conn.commit()
            continue
        _queue_processing.add(pid)
        try:
            data = gcode_path.read_bytes()
            storage = await client.get_default_storage()
            result = await client.upload_file(storage, filename, data, print_after=True)
            if result["ok"]:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute("UPDATE print_queue SET status = 'printing', started_at = ? WHERE id = ?",
                             (now, qid))
                conn.execute(
                    "INSERT INTO print_history (printer_id, file_name, started_at, status) VALUES (?,?,?,?)",
                    (pid, filename, now, "PRINTING")
                )
                conn.commit()
            else:
                log.error("Queue upload failed for %s: HTTP %d — %s",
                          filename, result["status"], result["detail"])
                conn.execute("UPDATE print_queue SET status = 'error' WHERE id = ?", (qid,))
                conn.commit()
        except Exception as e:
            log.error("Queue error for printer %s: %s", pid, e)
            conn.execute("UPDATE print_queue SET status = 'error' WHERE id = ?", (qid,))
            conn.commit()
        finally:
            _queue_processing.discard(pid)
    conn.close()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    conn = get_db()
    for row in conn.execute("SELECT * FROM printers WHERE enabled = 1"):
        pid = row["id"]
        printer_clients[pid] = PrusaLinkClient(
            host=row["host"], username=row["username"],
            password=row["password"], api_key=row["api_key"]
        )
        if row["camera_url"]:
            printer_camera_urls[pid] = row["camera_url"]
        if row["discord_webhook"]:
            printer_webhooks[pid] = row["discord_webhook"]
        printer_names[pid] = row["name"]
    conn.close()
    task = asyncio.create_task(polling_loop())
    yield
    task.cancel()

app = FastAPI(title="PrusaDisconnect", version="0.2.0", lifespan=lifespan)
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
    camera_url: str = ""
    discord_webhook: str = ""

class PrinterUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None
    camera_url: Optional[str] = None
    discord_webhook: Optional[str] = None
    enabled: Optional[bool] = None

class QueueAdd(BaseModel):
    gcode_id: str
    printer_id: str

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
        p["has_camera"] = r["id"] in camera_store
        printers.append(p)
    return printers

@app.post("/api/printers")
async def add_printer(body: PrinterAdd):
    pid = str(uuid.uuid4())[:8]
    client = PrusaLinkClient(host=body.host, username=body.username,
                             password=body.password, api_key=body.api_key)
    try:
        version = await client.get_version()
    except httpx.ConnectError:
        raise HTTPException(400, f"Connection refused — is the printer at {body.host} powered on and connected to the network?")
    except httpx.TimeoutException:
        raise HTTPException(400, f"Timed out connecting to {body.host} — check the IP address and ensure the printer is on your network")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(400, f"Authentication failed for {body.host} — check username/password or API key")
        raise HTTPException(400, f"Printer at {body.host} returned HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(400, f"Cannot reach printer at {body.host}: {e}")

    conn = get_db()
    conn.execute(
        "INSERT INTO printers (id, name, host, api_key, username, password, printer_type, camera_url, discord_webhook, added_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, body.name, body.host, body.api_key, body.username, body.password,
         version.get("text", ""), body.camera_url, body.discord_webhook,
         datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    printer_clients[pid] = client
    printer_names[pid] = body.name
    if body.camera_url:
        printer_camera_urls[pid] = body.camera_url
    if body.discord_webhook:
        printer_webhooks[pid] = body.discord_webhook
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
    row = conn.execute("SELECT * FROM printers WHERE id = ?", (printer_id,)).fetchone()
    conn.close()
    if row["enabled"]:
        printer_clients[printer_id] = PrusaLinkClient(
            host=row["host"], username=row["username"],
            password=row["password"], api_key=row["api_key"]
        )
        printer_names[printer_id] = row["name"]
        if row["camera_url"]:
            printer_camera_urls[printer_id] = row["camera_url"]
        else:
            printer_camera_urls.pop(printer_id, None)
        if row["discord_webhook"]:
            printer_webhooks[printer_id] = row["discord_webhook"]
        else:
            printer_webhooks.pop(printer_id, None)
    else:
        printer_clients.pop(printer_id, None)
    return {"ok": True}

@app.delete("/api/printers/{printer_id}")
async def delete_printer(printer_id: str):
    conn = get_db()
    conn.execute("DELETE FROM printers WHERE id = ?", (printer_id,))
    conn.execute("DELETE FROM print_queue WHERE printer_id = ?", (printer_id,))
    conn.commit()
    conn.close()
    printer_clients.pop(printer_id, None)
    telemetry_store.pop(printer_id, None)
    camera_store.pop(printer_id, None)
    printer_camera_urls.pop(printer_id, None)
    printer_webhooks.pop(printer_id, None)
    printer_names.pop(printer_id, None)
    discord_last_progress.pop(printer_id, None)
    discord_last_state.pop(printer_id, None)
    return {"ok": True}

# ---------------------------------------------------------------------------
# API: Camera
# ---------------------------------------------------------------------------
@app.get("/api/printers/{printer_id}/camera/snap")
async def camera_snap(printer_id: str):
    """Return latest cached snapshot, or fetch live."""
    data = camera_store.get(printer_id)
    if not data:
        client = printer_clients.get(printer_id)
        if client:
            data = await client.get_camera_snap()
            if data:
                camera_store[printer_id] = data
    if data:
        ct = "image/jpeg" if data[:2] == b'\xff\xd8' else "image/png"
        return Response(content=data, media_type=ct,
                        headers={"Cache-Control": "no-cache"})
    raise HTTPException(204, "No snapshot available")

@app.get("/api/printers/{printer_id}/camera/stream")
async def camera_stream(printer_id: str):
    """MJPEG stream from cached snapshots (refreshes every 2s)."""
    async def generate():
        while True:
            data = camera_store.get(printer_id)
            if data:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + data + b"\r\n")
            await asyncio.sleep(2)
    return StreamingResponse(generate(),
                             media_type="multipart/x-mixed-replace; boundary=frame")

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
async def upload_file_to_printer(printer_id: str, storage: str, path: str,
                      file: UploadFile = File(...),
                      print_after: bool = Form(False)):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    data = await file.read()
    result = await client.upload_file(storage, path or file.filename, data, print_after)
    if not result["ok"]:
        raise HTTPException(502, f"Printer rejected upload: HTTP {result['status']} — {result['detail']}")
    return {"ok": True}

@app.post("/api/printers/{printer_id}/print/{storage}/{path:path}")
async def start_print(printer_id: str, storage: str, path: str):
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404)
    ok = await client.start_print(storage, path)
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

# ---------------------------------------------------------------------------
# API: G-code cloud storage
# ---------------------------------------------------------------------------
@app.get("/api/gcodes")
async def list_gcodes():
    conn = get_db()
    rows = conn.execute("SELECT * FROM gcodes ORDER BY uploaded_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/gcodes")
async def upload_gcode(file: UploadFile = File(...)):
    gid = str(uuid.uuid4())[:8]
    data = await file.read()
    dest = GCODE_DIR / gid
    dest.write_bytes(data)
    checksum = hashlib.md5(data).hexdigest()
    conn = get_db()
    conn.execute(
        "INSERT INTO gcodes (id, filename, display_name, size, uploaded_at, checksum) VALUES (?,?,?,?,?,?)",
        (gid, file.filename, file.filename, len(data),
         datetime.now(timezone.utc).isoformat(), checksum)
    )
    conn.commit()
    conn.close()
    return {"id": gid, "filename": file.filename, "size": len(data)}

@app.delete("/api/gcodes/{gcode_id}")
async def delete_gcode(gcode_id: str):
    gcode_path = GCODE_DIR / gcode_id
    if gcode_path.exists():
        gcode_path.unlink()
    conn = get_db()
    conn.execute("DELETE FROM gcodes WHERE id = ?", (gcode_id,))
    conn.execute("DELETE FROM print_queue WHERE gcode_id = ? AND status = 'queued'", (gcode_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/gcodes/{gcode_id}/download")
async def download_gcode(gcode_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM gcodes WHERE id = ?", (gcode_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    gcode_path = GCODE_DIR / gcode_id
    if not gcode_path.exists():
        raise HTTPException(404, "File missing from storage")
    return Response(
        content=gcode_path.read_bytes(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{row["filename"]}"'}
    )

@app.post("/api/gcodes/{gcode_id}/send/{printer_id}")
async def send_gcode_to_printer(gcode_id: str, printer_id: str,
                                print_after: bool = Query(False)):
    """Upload a stored gcode file directly to a printer."""
    client = printer_clients.get(printer_id)
    if not client:
        raise HTTPException(404, "Printer not found")
    conn = get_db()
    row = conn.execute("SELECT * FROM gcodes WHERE id = ?", (gcode_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "G-code not found")
    gcode_path = GCODE_DIR / gcode_id
    if not gcode_path.exists():
        raise HTTPException(404, "File missing from storage")
    data = gcode_path.read_bytes()
    storage = await client.get_default_storage()
    result = await client.upload_file(storage, row["filename"], data, print_after)
    if not result["ok"]:
        raise HTTPException(502, f"Printer rejected upload: HTTP {result['status']} — {result['detail']}")
    if print_after:
        conn = get_db()
        conn.execute(
            "INSERT INTO print_history (printer_id, file_name, started_at, status) VALUES (?,?,?,?)",
            (printer_id, row["filename"], datetime.now(timezone.utc).isoformat(), "PRINTING")
        )
        conn.commit()
        conn.close()
    return {"ok": True}

# ---------------------------------------------------------------------------
# API: Print queue
# ---------------------------------------------------------------------------
@app.get("/api/queue")
async def list_queue(printer_id: Optional[str] = None):
    conn = get_db()
    if printer_id:
        rows = conn.execute(
            "SELECT q.*, g.filename, g.display_name, g.size, p.name as printer_name "
            "FROM print_queue q JOIN gcodes g ON q.gcode_id = g.id "
            "JOIN printers p ON q.printer_id = p.id "
            "WHERE q.printer_id = ? ORDER BY q.position",
            (printer_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT q.*, g.filename, g.display_name, g.size, p.name as printer_name "
            "FROM print_queue q JOIN gcodes g ON q.gcode_id = g.id "
            "JOIN printers p ON q.printer_id = p.id "
            "ORDER BY q.printer_id, q.position"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/queue")
async def add_to_queue(body: QueueAdd):
    conn = get_db()
    # Verify both exist
    if not conn.execute("SELECT 1 FROM printers WHERE id = ?", (body.printer_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "Printer not found")
    if not conn.execute("SELECT 1 FROM gcodes WHERE id = ?", (body.gcode_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "G-code not found")
    # Get next position
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 as next_pos FROM print_queue WHERE printer_id = ?",
        (body.printer_id,)
    ).fetchone()
    pos = row["next_pos"]
    qid = str(uuid.uuid4())[:8]
    conn.execute(
        "INSERT INTO print_queue (id, printer_id, gcode_id, position, status, added_at) VALUES (?,?,?,?,?,?)",
        (qid, body.printer_id, body.gcode_id, pos, "queued",
         datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    return {"id": qid, "position": pos}

@app.delete("/api/queue/{queue_id}")
async def remove_from_queue(queue_id: str):
    conn = get_db()
    conn.execute("DELETE FROM print_queue WHERE id = ?", (queue_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/queue")
async def clear_queue(printer_id: str = Query(...)):
    conn = get_db()
    conn.execute("DELETE FROM print_queue WHERE printer_id = ? AND status = 'queued'",
                 (printer_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

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
# API: Printer metrics
# ---------------------------------------------------------------------------
@app.get("/api/printers/{printer_id}/metrics")
async def printer_metrics(printer_id: str):
    conn = get_db()
    row = conn.execute("SELECT name FROM printers WHERE id = ?", (printer_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Printer not found")
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM print_history WHERE printer_id = ?", (printer_id,)
    ).fetchone()["cnt"]
    total_time = conn.execute(
        "SELECT COALESCE(SUM(time_printing), 0) as t FROM print_history WHERE printer_id = ?", (printer_id,)
    ).fetchone()["t"]
    completed = conn.execute(
        "SELECT COUNT(*) as cnt FROM print_history WHERE printer_id = ? AND status IN ('FINISHED', 'COMPLETED')",
        (printer_id,)
    ).fetchone()["cnt"]
    failed = conn.execute(
        "SELECT COUNT(*) as cnt FROM print_history WHERE printer_id = ? AND status IN ('ERROR', 'FAILED', 'STOPPED')",
        (printer_id,)
    ).fetchone()["cnt"]
    last_print = conn.execute(
        "SELECT file_name, started_at, status FROM print_history WHERE printer_id = ? ORDER BY started_at DESC LIMIT 1",
        (printer_id,)
    ).fetchone()
    conn.close()
    return {
        "printer_id": printer_id,
        "printer_name": row["name"],
        "total_prints": total,
        "completed": completed,
        "failed": failed,
        "success_rate": round(completed / total * 100, 1) if total > 0 else 0,
        "total_print_time_s": total_time,
        "last_print": dict(last_print) if last_print else None,
    }

@app.get("/api/metrics")
async def all_metrics():
    conn = get_db()
    printers = conn.execute("SELECT id, name FROM printers ORDER BY name").fetchall()
    results = []
    for p in printers:
        pid = p["id"]
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM print_history WHERE printer_id = ?", (pid,)
        ).fetchone()["cnt"]
        total_time = conn.execute(
            "SELECT COALESCE(SUM(time_printing), 0) as t FROM print_history WHERE printer_id = ?", (pid,)
        ).fetchone()["t"]
        completed = conn.execute(
            "SELECT COUNT(*) as cnt FROM print_history WHERE printer_id = ? AND status IN ('FINISHED', 'COMPLETED')",
            (pid,)
        ).fetchone()["cnt"]
        failed = conn.execute(
            "SELECT COUNT(*) as cnt FROM print_history WHERE printer_id = ? AND status IN ('ERROR', 'FAILED', 'STOPPED')",
            (pid,)
        ).fetchone()["cnt"]
        results.append({
            "printer_id": pid,
            "printer_name": p["name"],
            "total_prints": total,
            "completed": completed,
            "failed": failed,
            "success_rate": round(completed / total * 100, 1) if total > 0 else 0,
            "total_print_time_s": total_time,
        })
    conn.close()
    return results

@app.get("/api/printers/{printer_id}/metrics/history")
async def printer_metrics_history(printer_id: str, days: int = Query(30)):
    """Return daily print counts and print time for charting."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DATE(started_at) as day, COUNT(*) as prints, "
        "COALESCE(SUM(time_printing), 0) as print_time, "
        "SUM(CASE WHEN status IN ('FINISHED','COMPLETED') THEN 1 ELSE 0 END) as completed "
        "FROM print_history WHERE printer_id = ? AND started_at >= DATE('now', ?) "
        "GROUP BY DATE(started_at) ORDER BY day",
        (printer_id, f"-{days} days")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# PrusaSlicer compatibility (OctoPrint API)
# ---------------------------------------------------------------------------
@app.get("/api/version")
async def octoprint_version():
    """OctoPrint-compatible version endpoint for PrusaSlicer."""
    return {"api": "0.1", "server": "0.2.0", "text": "PrusaDisconnect 0.2.0"}

@app.post("/api/files/local")
async def octoprint_upload(file: UploadFile = File(...),
                           print: str = Form("false"),
                           select: str = Form("false")):
    """OctoPrint-compatible upload endpoint for PrusaSlicer.
    Stores the file in cloud storage. If print=true and there's exactly one printer, sends to it."""
    gid = str(uuid.uuid4())[:8]
    data = await file.read()
    dest = GCODE_DIR / gid
    dest.write_bytes(data)
    checksum = hashlib.md5(data).hexdigest()
    conn = get_db()
    conn.execute(
        "INSERT INTO gcodes (id, filename, display_name, size, uploaded_at, checksum) VALUES (?,?,?,?,?,?)",
        (gid, file.filename, file.filename, len(data),
         datetime.now(timezone.utc).isoformat(), checksum)
    )
    conn.commit()

    should_print = print.lower() in ("true", "1")
    result = {
        "files": {"local": {"name": file.filename, "origin": "local"}},
        "done": True
    }

    if should_print:
        # Auto-send to first available printer
        rows = conn.execute("SELECT id FROM printers WHERE enabled = 1 LIMIT 1").fetchall()
        if rows:
            pid = rows[0]["id"]
            client = printer_clients.get(pid)
            if client:
                try:
                    storage = await client.get_default_storage()
                    result = await client.upload_file(storage, file.filename, data, print_after=True)
                    if result["ok"]:
                        conn.execute(
                            "INSERT INTO print_history (printer_id, file_name, started_at, status) VALUES (?,?,?,?)",
                            (pid, file.filename, datetime.now(timezone.utc).isoformat(), "PRINTING")
                        )
                        conn.commit()
                except Exception:
                    pass

    conn.close()
    return result

# ---------------------------------------------------------------------------
# WebSocket: live telemetry
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        cam_available = {pid: pid in camera_store for pid in telemetry_store}
        await websocket.send_text(json.dumps({
            "type": "telemetry",
            "printers": telemetry_store,
            "cameras": cam_available,
        }))
        while True:
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
