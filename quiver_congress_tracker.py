#!/usr/bin/env python3
"""
Quiver Congress Tracker — Raspberry Pi Headless Edition
Runs as a systemd service automatically on boot.
Stores everything in QQCT_Data/ next to this script.

pip install requests

No display or GUI required. Credentials are read from QQCT_Data/config.env.
See SETUP.md for full Raspberry Pi setup instructions.
"""
from __future__ import annotations
import hashlib, logging, os, queue, re, signal, sqlite3, sys, threading, time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Sequence

try:
    import requests
except ImportError:
    sys.exit("FATAL: pip install requests")

# ============================================================
#  PATHS & CONSTANTS
# ============================================================
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(SCRIPT_DIR, "QQCT_Data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.env")
DB_PATH     = os.path.join(DATA_DIR, "trades.db")
os.makedirs(DATA_DIR, exist_ok=True)

SCHEMA_VERSION     = 1
USER_AGENT         = "QuiverCongressTracker/4.0 (Python)"
DEFAULT_POLL_TIMES = ("09:30", "13:00", "16:05")
_ET_STD            = timedelta(hours=-5)
_ET_DST            = timedelta(hours=-4)
logger             = logging.getLogger("congress_tracker")
_shutdown          = False

def _sig(s, f):
    global _shutdown
    _shutdown = True

# ============================================================
#  CONFIG FILE HELPERS
# ============================================================
def _read_env(path: str) -> dict:
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            if k:
                out[k] = v
    return out

def _write_env(path: str, updates: dict) -> None:
    existing = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as fh:
            existing = fh.readlines()
    written = set()
    new_lines = []
    for line in existing:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                written.add(k)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in written:
            new_lines.append(f"{k}={v}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(new_lines)

def load_config_env() -> dict:
    data = _read_env(CONFIG_PATH)
    for k, v in data.items():
        if k not in os.environ:
            os.environ[k] = v
    return data

# ============================================================
#  CONFIGURATION DATACLASS
# ============================================================
@dataclass
class Config:
    """
    Mutable so poll_times can be updated live without restarting.
    All other fields stay fixed after startup.
    """
    quiver_api_key: str
    discord_webhook_url: str
    poll_times: list           # list of "HH:MM" strings, US Eastern
    discord_send_delay: float   = 1.25
    max_discord_batch: int      = 25
    quiver_request_timeout: int = 30
    initial_notify_days: int    = 7   # on empty DB: only notify trades this many days old or newer
    log_level: str              = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        raw = os.getenv("POLL_TIMES", "")
        poll_times = [t.strip() for t in raw.split(",") if t.strip()] if raw.strip() else list(DEFAULT_POLL_TIMES)
        return cls(
            quiver_api_key=os.environ["QUIVER_API_KEY"],
            discord_webhook_url=os.environ["DISCORD_WEBHOOK_URL"],
            poll_times=poll_times,
            discord_send_delay=float(os.getenv("DISCORD_SEND_DELAY", "1.25")),
            max_discord_batch=int(os.getenv("MAX_DISCORD_BATCH", "25")),
            quiver_request_timeout=int(os.getenv("QUIVER_REQUEST_TIMEOUT", "30")),
            initial_notify_days=int(os.getenv("INITIAL_NOTIFY_DAYS", "7")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def set_poll_times(self, times: list) -> None:
        """Update poll_times in memory and persist to config.env."""
        self.poll_times = times
        _write_env(CONFIG_PATH, {"POLL_TIMES": ",".join(times)})
        os.environ["POLL_TIMES"] = ",".join(times)

# ============================================================
#  DATE / TIME HELPERS
# ============================================================
_DATE_FMTS = ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")

def parse_date(value: Any) -> Optional[date]:
    if value is None: return None
    if isinstance(value, date) and not isinstance(value, datetime): return value
    if isinstance(value, datetime): return value.date()
    text = str(value).strip()
    for fmt in _DATE_FMTS:
        try: return datetime.strptime(text, fmt).date()
        except ValueError: pass
    try: return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except: return None

def norm_type(raw: Optional[str]) -> str:
    v = (raw or "").strip().upper()
    if any(k in v for k in ("BUY", "PURCHASE")): return "BUY"
    if any(k in v for k in ("SELL", "SALE")):    return "SELL"
    return "OTHER"

def _now_utc() -> datetime: return datetime.now(timezone.utc)

def _is_dst(dt: datetime) -> bool:
    y = dt.year
    mar1 = datetime(y, 3, 1, tzinfo=timezone.utc)
    ds = (mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)).replace(hour=7, minute=0, second=0, microsecond=0)
    nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
    de = (nov1 + timedelta(days=(6 - nov1.weekday()) % 7)).replace(hour=6, minute=0, second=0, microsecond=0)
    return ds <= dt < de

def _to_et(dt: datetime) -> datetime: return dt + (_ET_DST if _is_dst(dt) else _ET_STD)
def _et_now() -> datetime: return _to_et(_now_utc())

def next_poll_utc(poll_times: list) -> Optional[datetime]:
    if not poll_times: return None
    now = _now_utc(); now_et = _to_et(now)
    off = _ET_DST if _is_dst(now) else _ET_STD
    cands = []
    for t in poll_times:
        try: hh, mm = map(int, t.split(":"))
        except: continue
        c = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0) - off
        if c <= now: c += timedelta(days=1)
        cands.append(c)
    return min(cands) if cands else None

def fmt_cd(s: int) -> str:
    h, r = divmod(max(0, s), 3600); m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def valid_hhmm(s: str) -> bool:
    """Return True if s is a valid HH:MM time string."""
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", s.strip())
    return m is not None

# ============================================================
#  TRADE MODEL
# ============================================================
@dataclass
class Trade:
    ticker: str; politician: str; transaction_type: str; amount: str
    transaction_date: date; report_date: Optional[date] = None; chamber: str = "Unknown"

    @property
    def dedupe_key(self) -> str:
        raw = "|".join([self.ticker.strip().upper(), self.politician.strip().upper(),
                        self.transaction_date.isoformat(), self.amount.strip().upper()])
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def from_api_row(cls, row: dict) -> Optional["Trade"]:
        ticker = str(row.get("Ticker") or row.get("Ticker Symbol") or "").strip().upper()
        pol    = str(row.get("Politician") or row.get("Representative") or row.get("Senator") or "").strip()
        tx_date = parse_date(row.get("TransactionDate") or row.get("Date"))
        if not ticker or not pol or tx_date is None: return None
        return cls(
            ticker=ticker, politician=pol,
            transaction_type=norm_type(str(row.get("Transaction") or row.get("Type") or "")),
            amount=str(row.get("Range") or row.get("Amount") or "").strip(),
            transaction_date=tx_date,
            report_date=parse_date(row.get("DateRecieved") or row.get("DisclosureDate") or row.get("last_modified")),
            chamber=str(row.get("Chamber") or row.get("Office") or "Unknown"),
        )

# ============================================================
#  QUIVER API CLIENT
# ============================================================
class QuiverClient:
    BASE = "https://api.quiverquant.com/beta"
    EPS  = ["/live/congresstrading", "/bulk/congresstrading"]
    CSRF = "TyTJwjuEC7VV7mOqZ622haRaaUr0x0Ng4nrwSRFKQs7vdoBcJlK9qjAS69ghzhFu"

    def __init__(self, api_key: str, timeout: int = 30):
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers.update({"Authorization": f"Token {api_key}", "Accept": "application/json",
                                 "X-CSRFToken": self.CSRF, "User-Agent": USER_AGENT})
        self._etag = self._lm = self._ep = None

    def _try(self, ep: str) -> Optional[requests.Response]:
        hdrs = {}
        if self._etag: hdrs["If-None-Match"] = self._etag
        if self._lm:   hdrs["If-Modified-Since"] = self._lm
        try:
            r = self._s.get(f"{self.BASE}{ep}", headers=hdrs, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout):
            return None
        if r.status_code in (200, 304): return r
        if r.status_code == 429: raise requests.HTTPError(response=r)
        return None

    def fetch(self) -> list:
        eps  = [self._ep] if self._ep else list(self.EPS)
        resp = None
        for ep in eps:
            resp = self._try(ep)
            if resp is not None:
                self._ep = ep; break
        if resp is None or resp.status_code == 304: return []
        self._etag = resp.headers.get("ETag"); self._lm = resp.headers.get("Last-Modified")
        try:    data = resp.json()
        except: return []
        return data if isinstance(data, list) else []

# ============================================================
#  SQLITE STORE
# ============================================================
class TradeStore:
    DDL = """
    CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE,
        ticker TEXT NOT NULL, politician TEXT NOT NULL, transaction_type TEXT NOT NULL,
        amount TEXT, transaction_date TEXT NOT NULL, report_date TEXT, chamber TEXT, fetched_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tx  ON trades (transaction_date DESC);
    CREATE INDEX IF NOT EXISTS idx_fat ON trades (fetched_at DESC);
    """

    def __init__(self, path: str):
        self._path = path
        self._c = sqlite3.connect(self._path, check_same_thread=False)
        self._c.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        return self._c

    def close(self):
        self._c.close()

    def init(self):
        with self._lock:
            c = self._conn(); c.executescript(self.DDL)
            c.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)); c.commit()

    def insert_new(self, trades: Sequence[Trade]) -> list:
        if not trades: return []
        with self._lock:
            conn = self._conn(); now = _now_utc().isoformat()
            keys = [t.dedupe_key for t in trades]
            existing = set()
            for i in range(0, len(keys), 900):
                b = keys[i:i+900]
                existing.update(r[0] for r in conn.execute(
                    f"SELECT dedupe_key FROM trades WHERE dedupe_key IN ({','.join('?'*len(b))})", b))
            new, rows = [], []
            for t in trades:
                if t.dedupe_key in existing: continue
                existing.add(t.dedupe_key); new.append(t)
                rows.append((t.dedupe_key, t.ticker, t.politician, t.transaction_type, t.amount,
                             t.transaction_date.isoformat(),
                             t.report_date.isoformat() if t.report_date else None,
                             t.chamber, now))
            if rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO trades (dedupe_key,ticker,politician,transaction_type,"
                    "amount,transaction_date,report_date,chamber,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)", rows)
                conn.commit()
            return new

    def count(self) -> int:
        with self._lock:
            r = self._conn().execute("SELECT COUNT(*) FROM trades").fetchone()
            return r[0] if r else 0

    def recent(self, n: int = 3) -> list:
        with self._lock:
            return self._conn().execute(
                "SELECT ticker,transaction_type,politician,amount,transaction_date,chamber "
                "FROM trades ORDER BY fetched_at DESC, transaction_date DESC LIMIT ?", (n,)
            ).fetchall()

# ============================================================
#  DISCORD NOTIFIER
# ============================================================
class Discord:
    BUY = 0x2ECC71; SELL = 0xE74C3C; OTHER = 0x95A5A6

    def __init__(self, url: str, delay: float = 1.25, retries: int = 4, timeout: int = 20):
        self.url = url; self.delay = delay; self.retries = retries; self.timeout = timeout
        self._s = requests.Session(); self._s.headers["User-Agent"] = USER_AGENT

    def _embed(self, t: Trade) -> dict:
        if   t.transaction_type == "BUY":  act, col = "bought", self.BUY
        elif t.transaction_type == "SELL": act, col = "sold",   self.SELL
        else:                              act, col = "traded",  self.OTHER
        fields = [{"name": "Type",   "value": t.transaction_type,             "inline": True},
                  {"name": "Amount", "value": t.amount or "N/A",              "inline": True},
                  {"name": "Traded", "value": t.transaction_date.isoformat(), "inline": True}]
        if t.report_date:
            fields.append({"name": "Reported", "value": t.report_date.isoformat(), "inline": True})
        return {"title":     f"🏛️ {t.politician} {act} ${t.ticker}",
                "color":     col, "fields": fields,
                "footer":    {"text": t.chamber},
                "timestamp": _now_utc().isoformat()}

    def send(self, trade: Trade) -> bool:
        pl = {"embeds": [self._embed(trade)]}
        for _ in range(self.retries):
            try:    r = self._s.post(self.url, json=pl, timeout=self.timeout)
            except: return False
            if r.status_code in (200, 204): return True
            if r.status_code == 429:
                time.sleep(max(1.0, float(r.headers.get("Retry-After", "2")))); continue
            return False
        return False

    def send_batch(self, trades: Sequence[Trade], limit: int = 25) -> int:
        sent = 0
        for t in sorted(trades, key=lambda x: (x.report_date or date.min, x.transaction_date))[:limit]:
            if _shutdown: break
            if self.send(t): sent += 1
            if self.delay > 0: time.sleep(self.delay)
        return sent

# ============================================================
#  POLL WORKER  (runs in a background thread)
# ============================================================
@dataclass
class PollResult:
    api_rows: int   = 0;  parsed: int    = 0
    new: int        = 0;  notified: int  = 0
    duration_s: float = 0.0;  error: str = ""
    new_trades: list  = field(default_factory=list)

def _poll_worker(cfg: Config, client: QuiverClient, store: TradeStore,
                 discord: Discord, dry_run: bool, q: queue.Queue) -> None:
    r = PollResult(); t0 = time.monotonic()

    # Check if the DB was empty before this poll — used to suppress old notifications
    db_was_empty = store.count() == 0

    try:    raw = client.fetch()
    except Exception as e:
        r.error = str(e); r.duration_s = time.monotonic() - t0; q.put(r); return
    r.api_rows = len(raw)
    trades = [t for row in raw if (t := Trade.from_api_row(row))]
    r.parsed = len(trades)
    try:    new = store.insert_new(trades)
    except Exception as e:
        r.error = f"DB: {e}"; r.duration_s = time.monotonic() - t0; q.put(r); return
    r.new = len(new); r.new_trades = new

    if new and not dry_run:
        if db_was_empty:
            # First-ever poll: only notify trades within INITIAL_NOTIFY_DAYS
            cutoff = _now_utc().date() - timedelta(days=cfg.initial_notify_days)
            notify = [t for t in new if t.transaction_date >= cutoff]
            logger.info(
                "Initial populate: stored %d trades, notifying %d within last %d days.",
                len(new), len(notify), cfg.initial_notify_days,
            )
        else:
            notify = new
        r.notified = discord.send_batch(notify, cfg.max_discord_batch)

    r.duration_s = time.monotonic() - t0; q.put(r)

# ============================================================
#  HEADLESS ENTRY POINT  (no GUI — runs forever in terminal)
#  Uncomment this block and call run_headless() from main()
#  instead of run_gui() to operate without a GUI.
# ============================================================
def run_headless(cfg: Config, dry_run: bool = False) -> int:
    store   = TradeStore(DB_PATH); store.init()
    client  = QuiverClient(cfg.quiver_api_key, cfg.quiver_request_timeout)
    discord = Discord(cfg.discord_webhook_url, cfg.discord_send_delay)
    q: queue.Queue[PollResult] = queue.Queue()

    logger.info("Headless mode started. Poll times (ET): %s", ", ".join(cfg.poll_times))
    try:
        while not _shutdown:
            nxt = next_poll_utc(cfg.poll_times)
            if nxt:
                secs = max(0, int((nxt - _now_utc()).total_seconds()))
                logger.info("Next poll in %s at %s ET", fmt_cd(secs), _to_et(nxt).strftime("%H:%M"))
                # Sleep in 1-second increments so shutdown flag is checked
                for _ in range(secs):
                    if _shutdown: break
                    time.sleep(1)
            if _shutdown: break
            _poll_worker(cfg, client, store, discord, dry_run, q)
            try:
                r = q.get_nowait()
                logger.info("Poll done — rows:%d parsed:%d new:%d notified:%d (%.1fs)%s",
                            r.api_rows, r.parsed, r.new, r.notified, r.duration_s,
                            f"  ERROR: {r.error}" if r.error else "")
            except queue.Empty:
                pass
    finally:
        store.close()
        logger.info("Headless shutdown complete.")
    return 0

# ============================================================
#  MAIN
# ============================================================
def main() -> int:
    # Log to stdout — systemd/journald captures this automatically.
    # Also log to a rotating file in QQCT_Data/ for easy on-device review.
    log_file = os.path.join(DATA_DIR, "tracker.log")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"))
    except Exception:
        pass  # If file logging fails, stdout is still fine

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)

    load_config_env()

    # Validate credentials exist before starting
    if not os.getenv("QUIVER_API_KEY", "").strip():
        logger.error("FATAL: QUIVER_API_KEY is not set in %s", CONFIG_PATH)
        logger.error("Edit the file and add: QUIVER_API_KEY=your_key_here")
        return 1
    if not os.getenv("DISCORD_WEBHOOK_URL", "").strip():
        logger.error("FATAL: DISCORD_WEBHOOK_URL is not set in %s", CONFIG_PATH)
        logger.error("Edit the file and add: DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...")
        return 1

    cfg = Config.from_env()
    logging.getLogger("congress_tracker").setLevel(cfg.log_level)
    logger.info("Starting Quiver Congress Tracker (headless / Raspberry Pi mode)")
    logger.info("Data directory : %s", DATA_DIR)
    logger.info("Database       : %s", DB_PATH)
    logger.info("Poll times (ET): %s", ", ".join(cfg.poll_times))
    return run_headless(cfg)


if __name__ == "__main__":
    sys.exit(main())
