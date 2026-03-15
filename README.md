# ⚡ PrusaDisconnect

**Self-hosted Prusa Connect alternative. No cloud. No internet. Your LAN, your printers.**

PrusaDisconnect talks directly to your Prusa printers via the PrusaLink local REST API. It provides a multi-printer dashboard, print controls, file management, and print history — all running on your local network with zero dependency on Prusa's cloud infrastructure.

## Why?

- **No cloud dependency** — works even if Prusa shuts down connect.prusa3d.com tomorrow
- **Privacy** — your print files and telemetry never leave your network
- **Speed** — local polling means instant response, no round-trip through the internet
- **Control** — you own the server, the data, and the code

## Supported Printers

Any Prusa printer running PrusaLink firmware:
- **Native PrusaLink**: MK4/S, MK3.9, MK3.5, XL, MINI/MINI+, Core One/+/L
- **RPi-based PrusaLink**: MK3/S/+, MK2.5/S (via Raspberry Pi)

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/yourname/prusa-disconnect.git
cd prusa-disconnect
docker compose up -d
```

Open `http://localhost:8484` in your browser.

### Manual

```bash
pip install -r requirements.txt
python server.py
```

Open `http://localhost:8484`.

## Features

### Phase 1 (Current — v0.1.0)
- [x] Multi-printer dashboard with live telemetry via WebSocket
- [x] Real-time temperatures (nozzle + bed, current + target)
- [x] Print progress, time remaining, time elapsed
- [x] Pause / Resume / Stop controls
- [x] File listing and upload to printer storage
- [x] Start prints remotely
- [x] Camera snapshot proxy
- [x] Print history logging
- [x] SQLite persistence (printers + history)
- [x] Docker deployment with single `docker compose up`
- [x] Zero internet required

### Phase 2 (Planned)
- [ ] Print queue management per printer
- [ ] PrusaSlicer integration (OctoPrint-compatible upload endpoint)
- [ ] Camera snapshot aggregation + timelapse
- [ ] Notification webhooks (Telegram, Discord, MQTT)
- [ ] File manager with thumbnails
- [ ] Drag-and-drop upload from browser

### Phase 3 (Future)
- [ ] Multi-user with RBAC
- [ ] Print cost / filament tracking
- [ ] Remote access via Tailscale/WireGuard
- [ ] Firmware update management
- [ ] Mobile-responsive PWA

## Architecture

```
┌──────────────────────────────────────────────────┐
│           PrusaDisconnect Server                  │
│           (Python / FastAPI)                      │
│                                                   │
│  ┌──────────┐  ┌──────────┐  ┌────────────────┐ │
│  │ Poller   │  │ SQLite   │  │ Frontend SPA   │ │
│  │ (async)  │──│ DB       │──│ (HTML/CSS/JS)  │ │
│  │ 3s loop  │  │          │  │                │ │
│  └────┬─────┘  └──────────┘  └────────────────┘ │
│       │                            │ WebSocket    │
└───────┼────────────────────────────┼─────────────┘
        │ HTTP Digest Auth           │
        │ PrusaLink v1 API           │
   ┌────▼────┐  ┌─────────┐    Browser
   │ MK4     │  │ MINI+   │
   │ :80     │  │ :80     │
   └─────────┘  └─────────┘
```

## API Endpoints

### Printer Management
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/printers` | List all printers with telemetry |
| POST | `/api/printers` | Add a new printer |
| PUT | `/api/printers/:id` | Update printer config |
| DELETE | `/api/printers/:id` | Remove a printer |

### Printer Actions (proxied to PrusaLink)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/printers/:id/status` | Full status |
| GET | `/api/printers/:id/job` | Current job info |
| POST | `/api/printers/:id/job/:jobId/pause` | Pause print |
| POST | `/api/printers/:id/job/:jobId/resume` | Resume print |
| DELETE | `/api/printers/:id/job/:jobId` | Stop print |
| GET | `/api/printers/:id/files/:storage` | List files |
| POST | `/api/printers/:id/files/:storage/:path` | Upload file |
| POST | `/api/printers/:id/print/:storage/:path` | Start print |
| GET | `/api/printers/:id/camera/snap` | Camera snapshot |

### Other
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/history` | Print history |
| WS | `/ws` | Live telemetry stream |

## Configuration

Printers are configured via the web UI. Each printer needs:
- **Name**: Display name
- **Host**: IP address or hostname (e.g., `192.168.1.50` or `prusa-mk4.local`)
- **Username/Password**: PrusaLink digest auth credentials
- **API Key** (optional): Some firmware versions use API key auth instead

### Network Mode

The Docker container uses `network_mode: host` by default so it can reach printers on your LAN. If your setup requires bridge networking, ensure the container can route to your printer IPs.

## How It Works

1. **Polling**: A background async task polls each printer's PrusaLink API every 3 seconds
2. **Storage**: Telemetry is held in memory for real-time access; printer configs and print history persist in SQLite
3. **WebSocket**: The frontend connects via WebSocket for live updates — no page refreshes needed
4. **Proxy**: Print actions (pause/resume/stop/upload) are proxied through to the printer's PrusaLink API with proper digest auth

## Based On

- **PrusaLink OpenAPI v1 Spec**: [github.com/prusa3d/Prusa-Link-Web/spec/openapi.yaml](https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml)
- **Prusa Connect SDK**: [github.com/prusa3d/Prusa-Connect-SDK-Printer](https://github.com/prusa3d/Prusa-Connect-SDK-Printer)

## License

MIT — do whatever you want with it.
