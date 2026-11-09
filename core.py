"""
core.py - Logic layer of the Network Traffic Congestion Monitoring and
Optimization Visualizer.

Everything here is plain Python (no Streamlit), so it can be unit-tested
with pytest. The Streamlit user interface lives in app.py.

Modules (see Chapter Three, Figure 3.2):
    * Traffic Counter Sampler        -> read_counters(), read_per_interface()
    * Simulated Traffic Generator    -> simulated_counters()
    * Rate Computation Module        -> compute_rates()
    * Congestion Detection Module    -> utilisation_percent(), classify()
    * Optimization Recommendation    -> recommend()
    * Traffic Log Store              -> TrafficLog (SQLite + CSV export)
"""

import csv
import io
import math
import random
import sqlite3
import time
from datetime import datetime

try:
    import psutil
    PSUTIL_AVAILABLE = True
    PSUTIL_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on environment
    psutil = None
    PSUTIL_AVAILABLE = False
    PSUTIL_IMPORT_ERROR = str(exc)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
MAX_POINTS = 60               # samples kept in the rolling history
BYTES_PER_KB = 1024.0
BYTES_PER_MB = 1024.0 * 1024.0

NORMAL = "Normal"
MODERATE = "Moderate"
CONGESTED = "Congested"

DEFAULT_MODERATE_THRESHOLD = 50.0    # percent of link capacity
DEFAULT_CONGESTED_THRESHOLD = 80.0   # percent of link capacity


# --------------------------------------------------------------------------- #
# Traffic Counter Sampler and Simulated Traffic Generator
# --------------------------------------------------------------------------- #
def simulated_counters(state, heavy_load=False):
    """Return a plausible, monotonically increasing simulated reading.

    `state` is a dict that keeps the simulator's running totals between
    calls. With heavy_load=True the traffic is roughly tripled so that the
    congestion levels can be demonstrated on a small link capacity.
    """
    sim = state.setdefault(
        "_sim_state",
        {"bytes_sent": 5_000_000, "bytes_recv": 20_000_000,
         "packets_sent": 40_000, "packets_recv": 90_000,
         "t": 0.0, "last": time.time()},
    )
    # Advance by real elapsed time so simulated rates (per second) do not
    # depend on the refresh interval.
    t_now = time.time()
    dt = min(max(t_now - sim.get("last", t_now), 0.5), 10.0)
    sim["last"] = t_now
    sim["t"] += dt
    wave = (math.sin(sim["t"] / 8.0) + 1.0) / 2.0          # 0..1, period ~50 s
    # Normal: up to ~3 Mbps download. Heavy load: up to ~9 Mbps.
    factor = 3.0 if heavy_load else 1.0
    down = int((20_000 + wave * 300_000 + random.uniform(0, 60_000)) * factor * dt)
    up = int((8_000 + wave * 90_000 + random.uniform(0, 20_000)) * factor * dt)
    pkt_down = int((60 + wave * 400 + random.uniform(0, 80)) * factor * dt)
    pkt_up = int((30 + wave * 160 + random.uniform(0, 40)) * factor * dt)
    sim["bytes_recv"] += down
    sim["bytes_sent"] += up
    sim["packets_recv"] += pkt_down
    sim["packets_sent"] += pkt_up
    return {
        "bytes_sent": sim["bytes_sent"], "bytes_recv": sim["bytes_recv"],
        "packets_sent": sim["packets_sent"], "packets_recv": sim["packets_recv"],
        "live": False,
    }


def read_counters(state, force_simulated=False, heavy_load=False):
    """Return cumulative network counters.

    Uses psutil for live counters. Never raises: on any failure (or when
    force_simulated is True) a simulated reading is returned instead.
    """
    if PSUTIL_AVAILABLE and not force_simulated:
        try:
            c = psutil.net_io_counters()
            if c is not None:
                return {
                    "bytes_sent": int(c.bytes_sent), "bytes_recv": int(c.bytes_recv),
                    "packets_sent": int(c.packets_sent), "packets_recv": int(c.packets_recv),
                    "live": True,
                }
        except Exception:
            pass
    return simulated_counters(state, heavy_load=heavy_load)


def read_per_interface(state, force_simulated=False):
    """Return ([(interface, bytes_sent, bytes_recv), ...], is_live)."""
    if PSUTIL_AVAILABLE and not force_simulated:
        try:
            per = psutil.net_io_counters(pernic=True)
            rows = [(name, int(c.bytes_sent), int(c.bytes_recv)) for name, c in per.items()]
            if rows:
                return rows, True
        except Exception:
            pass
    sim = state.get("_sim_state", {})
    sent = sim.get("bytes_sent", 5_000_000)
    recv = sim.get("bytes_recv", 20_000_000)
    return [
        ("eth0 (sim)", int(sent * 0.70), int(recv * 0.75)),
        ("wlan0 (sim)", int(sent * 0.25), int(recv * 0.20)),
        ("lo (sim)", int(sent * 0.05), int(recv * 0.05)),
    ], False


def busiest_interface(rows):
    """Name of the interface with the highest total traffic, or None."""
    if not rows:
        return None
    return max(rows, key=lambda r: r[1] + r[2])[0]


# --------------------------------------------------------------------------- #
# Rate Computation Module
# --------------------------------------------------------------------------- #
def compute_rates(prev, cur, dt):
    """Per-second rates between two readings.

    Returns a dict with down_kb, up_kb (KB/s) and pkt_recv, pkt_sent
    (packets/s). Negative differences (counter reset) are clamped to 0.
    """
    dt = max(float(dt), 1e-3)
    d_recv = max(cur["bytes_recv"] - prev["bytes_recv"], 0)
    d_sent = max(cur["bytes_sent"] - prev["bytes_sent"], 0)
    d_precv = max(cur["packets_recv"] - prev["packets_recv"], 0)
    d_psent = max(cur["packets_sent"] - prev["packets_sent"], 0)
    return {
        "down_kb": (d_recv / dt) / BYTES_PER_KB,
        "up_kb": (d_sent / dt) / BYTES_PER_KB,
        "pkt_recv": d_precv / dt,
        "pkt_sent": d_psent / dt,
    }


def human_bytes(n):
    """Format a byte count as a human-readable string. Safe on bad input."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0 B"
    if n < BYTES_PER_KB:
        return f"{n:.0f} B"
    if n < BYTES_PER_MB:
        return f"{n / BYTES_PER_KB:.1f} KB"
    if n < BYTES_PER_MB * BYTES_PER_KB:
        return f"{n / BYTES_PER_MB:.2f} MB"
    return f"{n / (BYTES_PER_MB * BYTES_PER_KB):.2f} GB"


# --------------------------------------------------------------------------- #
# Congestion Detection Module
# --------------------------------------------------------------------------- #
def kbps_to_mbps(rate_kb_per_s):
    """Convert KB/s (kilobytes per second) to Mbps (megabits per second)."""
    return rate_kb_per_s * BYTES_PER_KB * 8.0 / 1_000_000.0


def utilisation_percent(rate_kb_per_s, capacity_mbps):
    """Link utilisation (%) = traffic rate / link capacity x 100."""
    if not capacity_mbps or capacity_mbps <= 0:
        return 0.0
    return kbps_to_mbps(max(rate_kb_per_s, 0.0)) / capacity_mbps * 100.0


def classify(util_percent,
             moderate=DEFAULT_MODERATE_THRESHOLD,
             congested=DEFAULT_CONGESTED_THRESHOLD):
    """Classify utilisation into Normal, Moderate or Congested."""
    if util_percent > congested:
        return CONGESTED
    if util_percent >= moderate:
        return MODERATE
    return NORMAL


# --------------------------------------------------------------------------- #
# Optimization Recommendation Engine
# --------------------------------------------------------------------------- #
def recommend(level, busiest=None, direction="download"):
    """Return a list of (action, priority) recommendations for a level."""
    iface = f" on interface '{busiest}'" if busiest else ""
    if level == CONGESTED:
        return [
            (f"Identify and rate-limit the heaviest {direction} users{iface} (traffic shaping).", "High"),
            ("Enable QoS priority for real-time traffic (VoIP, video conferencing, e-learning).", "High"),
            ("Postpone large downloads, updates and backups to off-peak hours.", "High"),
            ("Balance load across an alternative link or access point if available.", "Medium"),
            ("If congestion persists daily, plan a bandwidth upgrade for this link.", "Medium"),
        ]
    if level == MODERATE:
        return [
            (f"Watch the trend{iface}: utilisation is approaching the congestion threshold.", "Medium"),
            ("Schedule bandwidth-intensive tasks (updates, backups) for off-peak hours.", "Medium"),
            ("Check for unexpected heavy users or applications (streaming, file sharing).", "Low"),
        ]
    return [("No action required: traffic is within normal limits.", "Low")]


# --------------------------------------------------------------------------- #
# Traffic Log Store (SQLite) - Tables 3.3 and 3.4
# --------------------------------------------------------------------------- #
class TrafficLog:
    """SQLite store for traffic samples and recommendations."""

    def __init__(self, path="traffic_log.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS TrafficLog (
                LogID INTEGER PRIMARY KEY AUTOINCREMENT,
                Timestamp TEXT NOT NULL,
                Interface TEXT,
                DownloadKBps REAL,
                UploadKBps REAL,
                PacketsRecvPerSec REAL,
                PacketsSentPerSec REAL,
                Utilisation REAL,
                CongestionLevel TEXT,
                DataSource TEXT
            );
            CREATE TABLE IF NOT EXISTS Recommendation (
                RecID INTEGER PRIMARY KEY AUTOINCREMENT,
                LogID INTEGER REFERENCES TrafficLog(LogID),
                Action TEXT,
                Priority TEXT
            );
            """
        )
        self.conn.commit()

    def save(self, rates, util, level, live, interface="All", recommendations=None):
        """Store one sample (and its recommendations). Returns the LogID."""
        cur = self.conn.execute(
            "INSERT INTO TrafficLog (Timestamp, Interface, DownloadKBps, UploadKBps,"
            " PacketsRecvPerSec, PacketsSentPerSec, Utilisation, CongestionLevel, DataSource)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), interface,
             round(rates["down_kb"], 2), round(rates["up_kb"], 2),
             round(rates["pkt_recv"], 2), round(rates["pkt_sent"], 2),
             round(util, 2), level, "Live" if live else "Simulated"),
        )
        log_id = cur.lastrowid
        if recommendations and level != NORMAL:
            self.conn.executemany(
                "INSERT INTO Recommendation (LogID, Action, Priority) VALUES (?,?,?)",
                [(log_id, a, p) for a, p in recommendations],
            )
        self.conn.commit()
        return log_id

    def recent_events(self, limit=20):
        """Most recent Moderate/Congested samples, newest first."""
        rows = self.conn.execute(
            "SELECT Timestamp, DownloadKBps, UploadKBps, Utilisation, CongestionLevel, DataSource"
            " FROM TrafficLog WHERE CongestionLevel != ? ORDER BY LogID DESC LIMIT ?",
            (NORMAL, limit),
        ).fetchall()
        keys = ["Time", "Download (KB/s)", "Upload (KB/s)", "Utilisation (%)", "Level", "Source"]
        return [dict(zip(keys, r)) for r in rows]

    def counts(self):
        """Number of logged samples per congestion level."""
        rows = self.conn.execute(
            "SELECT CongestionLevel, COUNT(*) FROM TrafficLog GROUP BY CongestionLevel"
        ).fetchall()
        out = {NORMAL: 0, MODERATE: 0, CONGESTED: 0}
        out.update(dict(rows))
        return out

    def export_csv(self):
        """Whole TrafficLog table as CSV text."""
        cur = self.conn.execute("SELECT * FROM TrafficLog ORDER BY LogID")
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([d[0] for d in cur.description])
        writer.writerows(cur.fetchall())
        return buf.getvalue()

    def clear(self):
        self.conn.execute("DELETE FROM Recommendation")
        self.conn.execute("DELETE FROM TrafficLog")
        self.conn.commit()


def now():
    return time.time()
