# Deployment Guide — Docker & Home Server (NUC)

This guide explains how to run the Expense Tracker as a containerized service on a home server (e.g. an Intel NUC), using Docker and docker-compose. The setup gives you:

- **Always-on GUI** — accessible from any device on your home network
- **Scheduled daily sync** — fetches bank statements from email, ingests, categorizes, and regenerates reports automatically
- **Shared reports** — ODS and PDF files synced to a cloud folder (iCloud, Dropbox, Google Drive, etc.)

---

## Prerequisites

- A Linux-based home server (Ubuntu/Debian recommended)
- [Docker](https://docs.docker.com/engine/install/) and [Docker Compose](https://docs.docker.com/compose/install/) installed
- Git installed on the server

---

## 1. Project Structure on the Server

Clone the repo to your server:

```bash
# Clone a specific branch (e.g. integration, main)
git clone -b integration https://github.com/<your-user>/personal-expense-tracker.git
cd personal-expense-tracker
```

The Docker setup uses **bind mounts** so your data lives on the host filesystem (not inside the container). This means data persists across container rebuilds and is easy to back up or sync.

```
expense-tracking/
├── docker-compose.yml      # Orchestrates both services
├── Dockerfile              # App image definition
├── cron/
│   └── daily-sync.sh       # Automated daily sync script
├── data/                   # ← persisted on host (bind mount)
├── raw/                    # ← persisted on host (bind mount)
├── reports/                # ← persisted on host (bind mount)
├── backups/                # ← persisted on host (bind mount)
└── expense-report.ods      # ← persisted on host (bind mount)
```

---

## 2. Dockerfile

Create a `Dockerfile` in the project root:

```dockerfile
FROM python:3.12-slim

# System deps for PDF parsing (pdfplumber uses pdftotext under the hood)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY bank_ingest.py .
COPY expense_tracker/ expense_tracker/

# Create runtime directories (will be overridden by bind mounts)
RUN mkdir -p raw data backups reports

# Default: run the GUI
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "expense_tracker/gui.py", \
     "--server.port", "8501", \
     "--server.headless", "true", \
     "--server.address", "0.0.0.0", \
     "--browser.gatherUsageStats", "false"]
```

### Building the image

```bash
docker build -t expense-tracker .
```

---

## 3. docker-compose.yml

Create a `docker-compose.yml` in the project root. This defines two services:

1. **gui** — the always-on Streamlit web interface
2. **cron** — a lightweight container that runs the daily sync on a schedule

```yaml
services:
  # ── Always-on GUI ───────────────────────────────────────────────
  gui:
    build: .
    container_name: expense-tracker-gui
    restart: unless-stopped
    ports:
      - "8501:8501"
    volumes:
      - ./data:/app/data
      - ./raw:/app/raw
      - ./reports:/app/reports
      - ./backups:/app/backups
      - ./expense-report.ods:/app/expense-report.ods
    environment:
      - TZ=Europe/Lisbon

  # ── Scheduled daily sync ────────────────────────────────────────
  cron:
    build: .
    container_name: expense-tracker-cron
    restart: unless-stopped
    entrypoint: ["/bin/bash", "/app/cron/daily-sync.sh"]
    volumes:
      - ./data:/app/data
      - ./raw:/app/raw
      - ./reports:/app/reports
      - ./backups:/app/backups
      - ./expense-report.ods:/app/expense-report.ods
      - ./cron:/app/cron:ro
      # Optional: mount a shared cloud sync folder for reports
      # - /path/to/cloud-sync/folder:/app/shared
    environment:
      - TZ=Europe/Lisbon
      - SYNC_HOUR=8
      - SYNC_MINUTE=0
      # Optional: copy reports to a shared folder after sync
      # - SHARED_FOLDER=/app/shared
```

### Volume mapping explained

| Host path | Container path | Purpose |
|-----------|----------------|---------|
| `./data/` | `/app/data/` | SQLite DB, rules, email config, all settings |
| `./raw/` | `/app/raw/` | Raw bank statement files (CSV/PDF) |
| `./reports/` | `/app/reports/` | Generated monthly PDF reports |
| `./backups/` | `/app/backups/` | Backup archives |
| `./expense-report.ods` | `/app/expense-report.ods` | The main ODS report file |
| `./cron/` | `/app/cron/` | Sync script (read-only) |

### Port mapping

| Host port | Container port | Service |
|-----------|----------------|---------|
| 8501 | 8501 | Streamlit GUI |

> **Network access**: The cron container needs outbound internet access to connect to your email server (IMAP). Docker's default bridge network allows outbound connections, so this works out of the box.

---

## 4. Daily Sync Script

Create the file `cron/daily-sync.sh`:

```bash
#!/bin/bash
#
# daily-sync.sh — runs inside the cron container.
# Sleeps until the configured time, then runs the sync loop daily.
#
# Environment variables:
#   SYNC_HOUR   — hour to run (0-23, default: 8)
#   SYNC_MINUTE — minute to run (0-59, default: 0)
#   SHARED_FOLDER — optional path to copy reports to after sync
#

set -e
cd /app

HOUR="${SYNC_HOUR:-8}"
MINUTE="${SYNC_MINUTE:-0}"

echo "Expense Tracker daily sync"
echo "Schedule: every day at ${HOUR}:$(printf '%02d' ${MINUTE})"
echo "Timezone: ${TZ:-UTC}"
echo ""

sync_once() {
    echo "=== Sync started at $(date) ==="

    # 1. Fetch new statements from email (if configured)
    if [ -f "data/email-config.json" ]; then
        echo "[1/4] Fetching statements from email..."
        python bank_ingest.py fetch --quiet || echo "  Warning: email fetch failed (will retry tomorrow)"
    else
        echo "[1/4] Skipping email fetch (no email-config.json found)"
    fi

    # 2. Ingest, categorize, and regenerate ODS report
    echo "[2/4] Running auto ingest + report..."
    python bank_ingest.py auto --quiet

    # 3. Generate monthly PDF
    echo "[3/4] Generating PDF report..."
    python bank_ingest.py pdf --quiet || echo "  Warning: PDF generation failed (possibly no data for this month)"

    # 4. Copy to shared folder (if configured)
    if [ -n "${SHARED_FOLDER}" ] && [ -d "${SHARED_FOLDER}" ]; then
        echo "[4/4] Copying reports to shared folder..."
        cp -f expense-report.ods "${SHARED_FOLDER}/" 2>/dev/null || true
        mkdir -p "${SHARED_FOLDER}/reports"
        cp -f reports/*.pdf "${SHARED_FOLDER}/reports/" 2>/dev/null || true
        echo "  Reports copied to ${SHARED_FOLDER}"
    else
        echo "[4/4] No shared folder configured, skipping copy"
    fi

    echo "=== Sync complete at $(date) ==="
    echo ""
}

# Run once immediately on container start
sync_once

# Then loop: sleep until next scheduled time, run, repeat
while true; do
    # Calculate seconds until next run
    now=$(date +%s)
    target=$(date -d "today ${HOUR}:$(printf '%02d' ${MINUTE})" +%s 2>/dev/null || \
             date -j -f "%H:%M" "${HOUR}:$(printf '%02d' ${MINUTE})" +%s 2>/dev/null)

    if [ "$target" -le "$now" ]; then
        # Already passed today, schedule for tomorrow
        target=$((target + 86400))
    fi

    sleep_secs=$((target - now))
    echo "Next sync in $(( sleep_secs / 3600 ))h $(( (sleep_secs % 3600) / 60 ))m (at $(date -d @${target} 2>/dev/null || date -r ${target}))"
    sleep "$sleep_secs"

    sync_once
done
```

Make it executable:

```bash
mkdir -p cron
chmod +x cron/daily-sync.sh
```

---

## 5. Initial Setup

### 5.1 First-time initialization

Before starting the containers, run the install script on the host to create the directory structure and starter config files:

```bash
./install.sh
```

### 5.2 Configure email (optional)

If you want the daily sync to fetch statements from email, set up the email config **before** starting the containers:

```bash
source .venv/bin/activate
python bank_ingest.py fetch --setup
```

This creates `data/email-config.json` which will be available inside the containers via the bind mount.

### 5.3 Create the ODS file

The ODS file needs to exist before Docker can bind-mount it (otherwise Docker creates a directory):

```bash
touch expense-report.ods
```

Or better, run the pipeline once with some data so it generates a real ODS:

```bash
source .venv/bin/activate
python bank_ingest.py auto
```

---

## 6. Starting the Services

```bash
# Build and start both services
docker compose up -d --build

# Check they're running
docker compose ps

# View GUI logs
docker compose logs -f gui

# View cron/sync logs
docker compose logs -f cron
```

The GUI is now available at: **http://\<nuc-ip\>:8501**

Any device on your home network can open this URL in a browser.

---

## 7. Shared Cloud Folder (Optional)

To automatically copy reports to a cloud-synced folder, uncomment and configure the shared folder volume and environment variable in `docker-compose.yml`:

```yaml
# In the cron service:
volumes:
  - /path/to/cloud-sync/folder:/app/shared
environment:
  - SHARED_FOLDER=/app/shared
```

Common cloud sync folder locations:

| Service | Typical Linux path |
|---------|--------------------|
| Dropbox | `~/Dropbox/Expenses` |
| Google Drive (via rclone) | `~/gdrive/Expenses` |
| Nextcloud | `~/Nextcloud/Expenses` |
| Syncthing | `~/Sync/Expenses` |

If you're using **Syncthing** on your NUC (which is common for NUC setups), just point the shared folder to a Syncthing-managed directory and it'll propagate to all connected devices automatically.

---

## 8. Adding to an Existing docker-compose

If you already have a `docker-compose.yml` on your NUC with other services, you can add the expense tracker services to it. Here's what to add:

```yaml
# Add to your existing docker-compose.yml
services:
  # ... your existing services ...

  expense-tracker-gui:
    build: ./expense-tracking
    container_name: expense-tracker-gui
    restart: unless-stopped
    ports:
      - "8501:8501"
    volumes:
      - ./expense-tracking/data:/app/data
      - ./expense-tracking/raw:/app/raw
      - ./expense-tracking/reports:/app/reports
      - ./expense-tracking/backups:/app/backups
      - ./expense-tracking/expense-report.ods:/app/expense-report.ods
    environment:
      - TZ=Europe/Lisbon

  expense-tracker-cron:
    build: ./expense-tracking
    container_name: expense-tracker-cron
    restart: unless-stopped
    entrypoint: ["/bin/bash", "/app/cron/daily-sync.sh"]
    volumes:
      - ./expense-tracking/data:/app/data
      - ./expense-tracking/raw:/app/raw
      - ./expense-tracking/reports:/app/reports
      - ./expense-tracking/backups:/app/backups
      - ./expense-tracking/expense-report.ods:/app/expense-report.ods
      - ./expense-tracking/cron:/app/cron:ro
    environment:
      - TZ=Europe/Lisbon
      - SYNC_HOUR=8
      - SYNC_MINUTE=0
```

Make sure the `build` context points to where you cloned the expense-tracking repo.

---

## 9. Alternative: systemd (No Docker)

If you prefer to run natively without Docker (e.g. you already have Python on the NUC):

### 9.1 GUI service

Create `/etc/systemd/system/expense-tracker-gui.service`:

```ini
[Unit]
Description=Expense Tracker GUI
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/expense-tracking
ExecStart=/home/YOUR_USERNAME/expense-tracking/.venv/bin/python -m streamlit run \
    expense_tracker/gui.py \
    --server.port 8501 \
    --server.headless true \
    --server.address 0.0.0.0 \
    --browser.gatherUsageStats false
Restart=on-failure
RestartSec=10
Environment=HOME=/home/YOUR_USERNAME

[Install]
WantedBy=multi-user.target
```

### 9.2 Daily sync timer

Create `/etc/systemd/system/expense-tracker-sync.service`:

```ini
[Unit]
Description=Expense Tracker Daily Sync
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/expense-tracking
ExecStart=/home/YOUR_USERNAME/expense-tracking/cron/daily-sync-native.sh
Environment=HOME=/home/YOUR_USERNAME
```

Create `/etc/systemd/system/expense-tracker-sync.timer`:

```ini
[Unit]
Description=Expense Tracker daily sync timer

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### 9.3 Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now expense-tracker-gui.service
sudo systemctl enable --now expense-tracker-sync.timer

# Check status
systemctl status expense-tracker-gui
systemctl status expense-tracker-sync.timer
systemctl list-timers expense-tracker-sync.timer
```

---

## 10. Updating

To update the expense tracker on the server:

```bash
cd /path/to/expense-tracking

# Pull latest code
git pull

# Rebuild and restart containers
docker compose up -d --build
```

Or use the built-in update script:

```bash
./update.sh
docker compose up -d --build
```

---

## 11. Useful Commands

```bash
# View real-time sync logs
docker compose logs -f cron

# Trigger a manual sync (outside the schedule)
docker compose exec cron python bank_ingest.py fetch --quiet
docker compose exec cron python bank_ingest.py auto --quiet

# Run any CLI command inside the container
docker compose exec gui python bank_ingest.py rules list
docker compose exec gui python bank_ingest.py suggest

# Restart just the GUI
docker compose restart gui

# Stop everything
docker compose down

# Stop and remove volumes (careful — this removes data!)
# docker compose down -v
```

---

## 12. Troubleshooting

**Container can't connect to email server**
Make sure the cron container has internet access. Test with:
```bash
docker compose exec cron python -c "import socket; socket.create_connection(('imap.gmail.com', 993)); print('OK')"
```

**ODS file mount creates a directory instead of a file**
Make sure `expense-report.ods` exists as a file on the host before starting:
```bash
touch expense-report.ods
```

**GUI not accessible from other devices**
Check your NUC's firewall allows port 8501:
```bash
sudo ufw allow 8501/tcp
```

**Timezone is wrong in logs**
Set the `TZ` environment variable in `docker-compose.yml` to your timezone (e.g. `Europe/Lisbon`).

**Permission errors on mounted volumes**
The container runs as root by default. If you need to match host user IDs:
```yaml
services:
  gui:
    user: "1000:1000"  # Match your host UID:GID
```

Find your UID/GID with: `id -u` and `id -g`
