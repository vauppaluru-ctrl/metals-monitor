# Deployment Guide

Two supported modes. Choose one — do not run both simultaneously on the same machine unless they target different ports.

---

## Mode A — macOS LaunchAgent (local, no Docker)

Runs `metals_live_monitor.py` directly on the host. `metals_web_server.py` is started separately when you need the dashboard.

### Install

```bash
cd metals_monitor
bash install_launch_agent.sh
```

The script:
1. Detects a working Python 3 (falls back to `/usr/bin/python3` 3.9.6 if Homebrew Python has broken `pyexpat.so`)
2. Creates `.venv` and installs `requirements.txt`
3. Generates `com.local.metalsmonitor.plist` from the template and copies it to `~/Library/LaunchAgents/`
4. Bootstraps, enables, and kick-starts the agent
5. Runs one immediate test cycle

### Verify

```bash
launchctl print gui/$(id -u)/com.local.metalsmonitor
# Expect: state = waiting  (or running during a cycle)
```

### Start the web dashboard (separate step)

```bash
.venv/bin/uvicorn metals_web_server:app --host 0.0.0.0 --port 8747
```

Set `SCHEDULER_ENABLED=false` (the default) so the dashboard does not also run a scheduler loop — the LaunchAgent handles scheduling.

### Reload after config change

```bash
launchctl bootout gui/$(id -u)/com.local.metalsmonitor
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.metalsmonitor.plist
```

### Uninstall

```bash
bash uninstall_launch_agent.sh
```

---

## Mode B — Docker / container (local or cloud)

Single container runs both the web server and the built-in asyncio scheduler. No LaunchAgent needed.

### Prerequisites
- Docker Desktop (local) or Docker Engine (server)
- Port 8747 available

### Build and run

```bash
cd metals_monitor
docker compose up --build          # foreground
docker compose up --build -d       # background (detached)
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | Run hourly monitor loop inside the container |
| `SCHEDULER_INTERVAL_SECS` | `3600` | Seconds between monitor runs |
| `PORT` | `8747` | Web server port (must match `ports:` in compose) |

Override in `docker-compose.yml` or with `-e` flags.

### Volumes

Named volumes provide persistence across container restarts:

| Volume | Container path | Contents |
|---|---|---|
| `metals-state` | `/app/metals_monitor/metals_monitor_state` | `state.json` (cooldowns, alert history) |
| `metals-logs` | `/app/metals_monitor/metals_monitor_logs` | `metals_monitor.log` and rotation backups |

To view logs from outside the container:
```bash
docker compose exec metals-monitor tail -f metals_monitor/metals_monitor_logs/metals_monitor.log
```

### Health check

The container exposes a health check that GETs `/api/status`. Docker Compose and orchestrators (ECS, Cloud Run) use this automatically.

```bash
docker ps                                # check STATUS column
docker inspect metals-monitor | grep -A5 Health
```

### Useful commands

```bash
docker compose logs -f                  # stream all container output
docker compose restart metals-monitor   # restart without rebuild
docker compose down                     # stop, remove container
docker compose down -v                  # also remove volumes (deletes state + logs)
```

---

## Cloud deployment

The Docker image runs identically on any cloud that accepts containers. Notes:

### Fly.io (simplest)
```bash
fly launch --name metals-monitor --port 8747
fly deploy
fly logs
```

### Railway / Render
Point to the repository; set `PORT=8747`, `SCHEDULER_ENABLED=true`. Both auto-detect `Dockerfile`.

### AWS ECS / GCP Cloud Run
Push image to ECR or Artifact Registry, create a task/service with port 8747 and the two volumes (EFS on ECS, Cloud Filestore on GCP for persistence).

### Notifications on cloud
`osascript` (macOS only) silently fails on Linux containers. Use the **web notifications** button in the dashboard header instead. The SSE stream delivers alert events to any open browser tab regardless of platform.

---

## Python version constraint

On this machine, Homebrew Python 3.12 and 3.14 fail to create a venv because `pyexpat.cpython-*.so` references `_XML_SetAllocTrackerActivationThreshold`, which is absent in the system `libexpat.1.dylib`. The install script's `find_python()` function probes each Python and falls back to `/usr/bin/python3` (Apple system Python 3.9.6).

Docker uses Python 3.11-slim from Docker Hub — this constraint does not apply there.

Do not hardcode a Python interpreter path in any script. Always let `find_python()` or the shebang (`#!/usr/bin/env python3`) resolve it.
