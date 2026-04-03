# Quiver Congress Tracker – Universal Python Service

A standalone Python daemon that monitors U.S. Congress stock trades via the [Quiver Quantitative API](https://quiverquant.com) and sends real‑time notifications to a Discord channel.  
It runs **headless** – no GUI, no display – and works on any platform where Python 3.9+ is available: Windows, macOS, Linux, Raspberry Pi, cloud VMs, etc.

> ⚠️ **Disclaimer**  
> This tool relies on third‑party APIs and unofficial data. Always verify trading information from official sources. The authors are not responsible for any financial decisions made based on this data.

---

## Overview

The tracker periodically polls the Quiver Quantitative API, stores new trades in a local SQLite database (deduplicated), and dispatches rich embed notifications to a Discord webhook.  
It is designed to run continuously – as a background service, a systemd unit, a Windows scheduled task, or simply inside a `screen`/`tmux` session.

### Key Features

- **Fully headless** – no GUI, no X11, no desktop environment required.
- **Cross‑platform** – Python 3.9+ with only `requests` as an external dependency.
- **Persistent SQLite database** – keeps a complete, deduplicated history of all trades.
- **Smart initial sync** – on first run (empty DB), only trades from the last N days are notified (avoids spam).
- **Configurable polling schedule** – set multiple daily poll times in **US Eastern Time** (DST‑aware).
- **Automatic timezone handling** – converts between UTC and US Eastern correctly.
- **Graceful shutdown** – responds to `SIGINT`/`SIGTERM`.
- **Rotating log files** – logs to both stdout and a rotating file inside the data directory.

---

## Requirements

| Item                         | Notes                                           |
|------------------------------|-------------------------------------------------|
| Python 3.9+                  | Any OS (Windows, macOS, Linux, Raspberry Pi)   |
| `requests` library           | Install via `pip install requests`             |
| Internet connection          | To reach Quiver API and Discord                |
| Quiver Quantitative API key  | Free tier available at [quiverquant.com](https://quiverquant.com) |
| Discord webhook URL          | Create in your Discord server’s channel settings |

---

## File Structure

The entire application lives in a single directory. All data and configuration are stored inside a subfolder `QQCT_Data/` next to the script.

```
/path/to/your/project/
├── quiver_congress_tracker.py   # The main script (name can be anything)
└── QQCT_Data/                   # Automatically created on first run
    ├── config.env               # Configuration file (you create this)
    ├── trades.db                # SQLite database (auto‑created)
    └── tracker.log              # Rotating log file (auto‑created)
```

No other files are required. The script creates `QQCT_Data/` if it does not exist.

---

## Configuration

Create a file named `config.env` inside the `QQCT_Data/` folder.  
Use the following template (replace the placeholder values):

```ini
QUIVER_API_KEY=your_quiver_api_key_here
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxxxxxxx/yyyyyyyyyy
POLL_TIMES=09:30,13:00,16:05
INITIAL_NOTIFY_DAYS=7
LOG_LEVEL=INFO
DISCORD_SEND_DELAY=1.25
MAX_DISCORD_BATCH=25
QUIVER_REQUEST_TIMEOUT=30
```

### Configuration Options

| Variable                 | Default     | Description                                                                 |
|--------------------------|-------------|-----------------------------------------------------------------------------|
| `QUIVER_API_KEY`         | *(required)*| Your Quiver Quantitative API token.                                        |
| `DISCORD_WEBHOOK_URL`    | *(required)*| Full Discord webhook URL.                                                  |
| `POLL_TIMES`             | `09:30,13:00,16:05` | Comma‑separated list of poll times in **US Eastern Time** (24h format). |
| `INITIAL_NOTIFY_DAYS`    | `7`         | When the database is empty, only notify trades from the last N days.       |
| `LOG_LEVEL`              | `INFO`      | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`.                        |
| `DISCORD_SEND_DELAY`     | `1.25`      | Seconds to wait between sending multiple Discord embeds (rate‑limit safety). |
| `MAX_DISCORD_BATCH`      | `25`        | Maximum number of new trades to send in one poll cycle.                    |
| `QUIVER_REQUEST_TIMEOUT` | `30`        | HTTP timeout in seconds for Quiver API calls.                              |

> **Note**: After changing `POLL_TIMES`, the new schedule takes effect immediately without restarting the script. The value is persisted in `config.env`.

---

## Running the Script

### Basic Execution (for testing)

```bash
cd /path/to/your/project
python3 quiver_congress_tracker.py
```

Press `Ctrl+C` to stop. The script will log everything to the console and also to `QQCT_Data/tracker.log`.

### Running as a Background Process

- **Linux / macOS** – use `screen`, `tmux`, or a systemd service (see Raspberry Pi section below).
- **Windows** – use Task Scheduler or run as a background Python process with `pythonw.exe`.

---

## How It Works

1. **Startup** – reads `config.env`, initialises SQLite, creates tables if missing.
2. **Scheduling** – calculates the next poll time based on `POLL_TIMES` (US Eastern). Sleeps until that moment, checking for shutdown every second.
3. **Polling** – calls the Quiver `/bulk/congresstrading` endpoint with `If-None-Match` / `If-Modified-Since` headers to avoid redundant data.
4. **Parsing** – extracts trades from JSON, normalises transaction types (`BUY`/`SELL`/`OTHER`), and creates `Trade` objects.
5. **Deduplication** – inserts only new trades using a SHA‑256 key based on ticker, politician, date, and amount.
6. **Notification** – for each new trade, sends a Discord embed. On first run (empty DB), only trades within `INITIAL_NOTIFY_DAYS` are sent.
7. **Loop** – repeats forever.

---

## Raspberry Pi / Systemd Setup (Recommended for 24/7 Operation)

If you want the tracker to start automatically on boot and restart if it crashes, set it up as a **systemd service**.  
These instructions assume you are using a Raspberry Pi (or any Linux distribution with systemd) and that you have already placed the script in `/home/tradely/congress_tracker/`.

### 1. Prepare the environment

```bash
# Go to your project folder
mkdir -p /home/tradely/congress_tracker
cd /home/tradely/congress_tracker

# Make sure your script is here (if not, copy/move it)
# mv ~/quiver_congress_tracker.py .

# Install system packages (Python virtual environment support)
sudo apt update
sudo apt install python3-venv -y

# Create a virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install requests
deactivate   # exit the venv for now

# Create the data/config folder
mkdir -p QQCT_Data

# Create your config.env file (edit with your actual keys)
cat > QQCT_Data/config.env << 'EOF'
QUIVER_API_KEY=PUT_YOUR_API_KEY_HERE
DISCORD_WEBHOOK_URL=PUT_YOUR_WEBHOOK_URL_HERE
POLL_TIMES=09:30,13:00,16:05
LOG_LEVEL=INFO
EOF

# Make the script executable
chmod +x quiver_congress_tracker.py

# Test run (Ctrl+C to stop after it starts)
source venv/bin/activate
python quiver_congress_tracker.py
```

### 2. Create the systemd service

Create a service file (as root) with the following content.  
Replace `tradely` with your actual username and adjust paths if necessary.

```bash
sudo bash -c 'cat > /etc/systemd/system/quiver_tracker.service << EOF
[Unit]
Description=Quiver Congress Tracker
After=network.target

[Service]
User=tradely
WorkingDirectory=/home/tradely/congress_tracker
ExecStart=/home/tradely/congress_tracker/venv/bin/python /home/tradely/congress_tracker/quiver_congress_tracker.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF'
```

### 3. Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable quiver_tracker
sudo systemctl start quiver_tracker
```

### 4. Check status and logs

```bash
sudo systemctl status quiver_tracker
journalctl -u quiver_tracker -f
```

To see the rotating log file inside the data directory:

```bash
tail -f /home/tradely/congress_tracker/QQCT_Data/tracker.log
```

### 5. Stopping / restarting

```bash
sudo systemctl stop quiver_tracker
sudo systemctl restart quiver_tracker
```

---

## Logging

Two logging destinations are used simultaneously:

- **stdout** → captured by `journalctl` when running as a systemd service.
- **Rotating file** → `QQCT_Data/tracker.log` (max 5 MB, 3 backups).

Log entries include timestamp, level, and a human‑readable message. Example:

```
2025-01-15 13:05:01 [INFO] congress_tracker: Next poll in 02:30:00 at 16:05 ET
2025-01-15 16:05:15 [INFO] congress_tracker: Poll done — rows:342 parsed:12 new:3 notified:3 (1.2s)
```

---

## Database Schema

The file `trades.db` (inside `QQCT_Data/`) contains a table `trades`:

| Column              | Type    | Description                                   |
|---------------------|---------|-----------------------------------------------|
| `id`                | INTEGER | Auto‑increment primary key                   |
| `dedupe_key`        | TEXT    | SHA‑256 hash (unique)                        |
| `ticker`            | TEXT    | Stock symbol                                 |
| `politician`        | TEXT    | Name of the representative/senator           |
| `transaction_type`  | TEXT    | `BUY`, `SELL`, or `OTHER`                    |
| `amount`            | TEXT    | Dollar range (e.g. `$15,001 - $50,000`)      |
| `transaction_date`  | TEXT    | ISO date (YYYY-MM-DD)                        |
| `report_date`       | TEXT    | Disclosure date (may be NULL)                |
| `chamber`           | TEXT    | `House`, `Senate`, or `Unknown`              |
| `fetched_at`        | TEXT    | UTC timestamp when this record was inserted  |

There is also a `schema_version` table storing the current schema version (integer).

---

## Troubleshooting

| Symptom                            | Likely fix                                                                 |
|------------------------------------|----------------------------------------------------------------------------|
| `FATAL: QUIVER_API_KEY is not set` | Check that `config.env` exists in `QQCT_Data/` and contains the key.      |
| `ModuleNotFoundError: No module named 'requests'` | Run `pip install requests` (inside your virtual environment if using one). |
| No Discord notifications           | Verify webhook URL, check Discord server settings, look for HTTP errors in logs. |
| Service stops after a few hours    | Increase `RestartSec` in service file or add `TimeoutStopSec=60`.          |
| `Poll done — error: 429`           | Rate limit from Quiver API – reduce poll frequency or contact Quiver.      |
| Timezone mismatch                  | Ensure your system timezone is set correctly (on Linux: `timedatectl`).    |

---

## Updating the Script

To update to a newer version:

```bash
cd /home/tradely/congress_tracker
sudo systemctl stop quiver_tracker
# Replace quiver_congress_tracker.py with the new version
sudo systemctl start quiver_tracker
```

The database and `config.env` remain untouched.

---

## Uninstalling

```bash
sudo systemctl stop quiver_tracker
sudo systemctl disable quiver_tracker
sudo rm /etc/systemd/system/quiver_tracker.service
sudo systemctl daemon-reload
rm -rf /home/tradely/congress_tracker   # removes all data
```

---

## License

© TRADELY.DEV. All rights reserved. Refer to the repository licence file for terms.  
*(If you obtained this script from a public source, please check the accompanying license.)*

---

## Final Notes

- The script never calls home except to Quiver Quantitative and Discord.
- No telemetry, no auto‑updater.
- For security, consider running the service under a dedicated user with limited permissions.
- To debug interactively, run `python quiver_congress_tracker.py` from the terminal – log output will appear directly.

**Happy tracking!** 🏛️📈
