# Quiver Congress Tracker — Raspberry Pi Headless Edition

A standalone Python service that monitors US Congress stock trades via the Quiver Quantitative API and sends real-time notifications to a Discord channel. Designed to run 24/7 on a Raspberry Pi (or any Linux server) with no display, no GUI, and minimal dependencies.

> ⚠️ **Disclaimer**: This tool relies on third‑party APIs and unofficial data. Always verify trading information from official sources. The authors are not responsible for any financial decisions made based on this data.

---

## Overview

The tracker periodically polls the Quiver Quantitative API for new congressional trades, stores them in a local SQLite database, and dispatches rich embed notifications to a Discord webhook. It runs as a headless background process – perfect for a Raspberry Pi powered by a USB charger.

Key features:

- **Fully headless** – no X11, no desktop environment required.
- **Systemd integration** – starts on boot, restarts on failure.
- **Persistent SQLite database** – keeps a deduplicated history of all trades.
- **Smart initial sync** – when the database is empty, only the last N days of trades are notified (avoids spam on first run).
- **Configurable polling schedule** – set multiple daily poll times (US Eastern Time).
- **Automatic timezone handling** – correctly converts between UTC and US Eastern (DST‑aware).
- **Graceful shutdown** – responds to SIGINT/SIGTERM.
- **Rotating log files** – logs to both stdout (for journald) and a rotating file inside `QQCT_Data/`.

---

## Requirements

| Item                         | Notes                                        |
|------------------------------|----------------------------------------------|
| Raspberry Pi (any model)     | Tested on Pi 3A+                             |
| Raspberry Pi OS Lite (64‑bit)| Debian Bullseye/Bookworm recommended        |
| Python 3.9+                  | Pre‑installed on Raspberry Pi OS             |
| Internet connection          | To reach Quiver API and Discord              |
| Quiver Quantitative API key  | Free tier available at [quiverquant.com](https://quiverquant.com)|
| Discord webhook URL          | Create in your Discord server’s channel settings|

---

## File Structure

The entire application lives in a single directory. All data and configuration are stored inside a subfolder `QQCT_Data/` next to the script.

```
/home/pi/congress_tracker/
├── tracker.py                # The main script (name it as you wish)
└── QQCT_Data/                # Automatically created
    ├── config.env            # Configuration file (you create this)
    ├── trades.db             # SQLite database (auto‑created)
    └── tracker.log           # Rotating log file (auto‑created)
```

No other files are required. The script creates `QQCT_Data/` on first run if it does not exist.

---

## Installation

### 1. Install Python dependencies

Only one external library is needed:

```bash
sudo apt update
sudo apt install python3-pip   # if not already present
pip3 install requests
```

### 2. Download the script

Copy the `tracker.py` script to a directory of your choice, e.g.:

```bash
mkdir -p /home/pi/congress_tracker
nano /home/pi/congress_tracker/tracker.py
# Paste the full script content, save, and exit
```

Make it executable:

```bash
chmod +x /home/pi/congress_tracker/tracker.py
```

### 3. Create the configuration file

Inside `QQCT_Data/` create `config.env`:

```bash
cd /home/pi/congress_tracker
mkdir -p QQCT_Data
nano QQCT_Data/config.env
```

Add the following lines (replace with your actual keys):

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

Save and exit.

---

## Configuration Options

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

## Running Manually (for testing)

Before setting up the service, test that everything works:

```bash
cd /home/pi/congress_tracker
python3 tracker.py
```

You should see log output showing the first poll and any new trades. Press `Ctrl+C` to stop.

---

## Setting Up as a Systemd Service

Create a systemd unit file so the tracker starts automatically on boot and restarts if it crashes.

### 1. Create the service file

```bash
sudo nano /etc/systemd/system/congress-tracker.service
```

Paste the following (adjust the `WorkingDirectory` and `ExecStart` paths if needed):

```ini
[Unit]
Description=Quiver Congress Tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/congress_tracker
ExecStart=/usr/bin/python3 /home/pi/congress_tracker/tracker.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 2. Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable congress-tracker.service
sudo systemctl start congress-tracker.service
```

### 3. Check status and logs

```bash
sudo systemctl status congress-tracker.service
journalctl -u congress-tracker.service -f
```

---

## Logging

Two logging destinations are used simultaneously:

- **stdout** → captured by `journalctl` when running as a systemd service.
- **Rotating file** → `QQCT_Data/tracker.log` (max 5 MB, 3 backups).

Log entries include timestamp, level, and a human‑readable message. Example:

```
2025-01-15 13:05:01 [INFO] congress_tracker: Next poll in 02:30:00 at 16:05 ET
2025-01-15 16:05:15 [INFO] congress_tracker: Poll done — rows:342 parsed:12 new:3 notified:3 (1.2s)
```

---

## How It Works

1. **Startup** – reads `config.env`, initialises SQLite, creates tables if missing.
2. **Scheduling** – calculates the next poll time based on `POLL_TIMES` (US Eastern). Sleeps until that moment, checking for shutdown every second.
3. **Polling** – calls the Quiver `/bulk/congresstrading` (or `/live/…`) endpoint with `If-None-Match` / `If-Modified-Since` headers to avoid redundant data.
4. **Parsing** – extracts trades from JSON, normalises transaction types (`BUY`/`SELL`/`OTHER`), and creates `Trade` objects.
5. **Deduplication** – inserts only new trades using a SHA‑256 key based on ticker, politician, date, and amount.
6. **Notification** – for each new trade, sends a Discord embed. On first run (empty DB), only trades within `INITIAL_NOTIFY_DAYS` are sent.
7. **Loop** – repeats forever.

---

## Database Schema

The file `trades.db` contains two tables:

### `trades`

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

### `schema_version`

Stores the current schema version (integer).

---

## Troubleshooting

| Symptom                            | Likely fix                                                                 |
|------------------------------------|----------------------------------------------------------------------------|
| `FATAL: QUIVER_API_KEY is not set` | Check that `config.env` exists in `QQCT_Data/` and contains the key.      |
| `ModuleNotFoundError: No module named 'requests'` | Run `pip3 install requests` (use `pip3`, not `pip`).         |
| No Discord notifications            | Verify webhook URL, check Discord server settings, look for HTTP errors in logs. |
| Script stops after a few hours      | Increase `RestartSec` in service file or add `TimeoutStopSec=60`.          |
| `Poll done — error: 429`            | Rate limit from Quiver API – reduce poll frequency or contact Quiver.      |
| Timezone mismatch                   | Ensure the Pi’s timezone is set correctly (`sudo raspi-config` → Localisation Options). |

---

## Updating the Script

To update to a newer version:

```bash
cd /home/pi/congress_tracker
# Stop the service
sudo systemctl stop congress-tracker.service
# Replace tracker.py with the new version
# Restart
sudo systemctl start congress-tracker.service
```

The database and `config.env` remain untouched.

---

## Uninstalling

```bash
sudo systemctl stop congress-tracker.service
sudo systemctl disable congress-tracker.service
sudo rm /etc/systemd/system/congress-tracker.service
sudo systemctl daemon-reload
rm -rf /home/pi/congress_tracker   # removes all data
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
- To debug interactively, run `python3 tracker.py` from the terminal – log output will appear directly.

**Happy tracking!** 🏛️📈
