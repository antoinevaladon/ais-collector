#!/usr/bin/env python3
"""
Feeds the T-Dongle-S3 status screen. Every few seconds it checks the
collector's health in PostgreSQL and sends one status line over USB serial
(protocol: see ais_status_display.ino).

    venv/bin/python display/status_display.py           # find the dongle and run
    venv/bin/python display/status_display.py --print   # print the lines instead
    venv/bin/python display/status_display.py --demo    # sample screens, no database

It runs as its own service (ais-display.service), separate from the
collector, so a display problem can never interrupt data collection. It only
reads from the database, using the same .env as the collector.
"""

import argparse
import logging
import os
import shutil
import signal
import sys
import time
from datetime import datetime

import serial
from serial.tools import list_ports

try:
    import psycopg
except ImportError:           # fine for --demo, which needs only pyserial
    psycopg = None

VERSION = "1.0.0"
ESPRESSIF_VID = 0x303A        # USB vendor ID of the ESP32-S3's built-in USB port
INTERVAL = 5                  # seconds between updates (the dongle flags 30 s of silence)
HEARTBEAT_LATE = 60           # the collector heartbeats every 10 s

log = logging.getLogger("ais-display")


# ---------------------------------------------------------------- helpers

def load_env_file(path):
    """Same minimal .env reader as the collector, for runs outside systemd."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def span(seconds):
    """Uptime style: 12m, 5h 12m, 3d 4h."""
    m = max(0, int(seconds)) // 60
    if m < 60:
        return f"{m}m"
    if m < 1440:
        return f"{m // 60}h {m % 60}m"
    return f"{m // 1440}d {m % 1440 // 60}h"


def size(nbytes):
    for unit, factor in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if nbytes >= factor:
            return f"{nbytes / factor:.1f} {unit}"
    return f"{nbytes / 1e3:.0f} KB"


def clean(text, limit=40):
    """The screen's fonts are plain ASCII, and '|' separates fields."""
    text = str(text).replace("|", "/")
    return "".join(c for c in text if 32 <= ord(c) <= 126)[:limit]


def status_line(level, title, l1="", l2="", l3=""):
    corner = datetime.now().strftime("%H:%M")
    return "S|" + "|".join(clean(x) for x in (level, title, corner, l1, l2, l3)) + "\n"


# ---------------------------------------------------------------- health

class Health:
    """Reads the collector's state from the database. Read-only."""

    def __init__(self, source, disk_path):
        self.source = source
        self.disk_path = disk_path
        self.conn = None
        self.last_db_error = None

    def _one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def status(self):
        try:
            if self.conn is None or self.conn.closed:
                self.conn = psycopg.connect(autocommit=True, connect_timeout=5,
                                            options="-c statement_timeout=4000")
            return self._status()
        except psycopg.OperationalError as e:
            if self.conn is not None:
                self.conn.close()
            self.conn = None
            detail = " ".join(str(e).split()) or type(e).__name__
            if detail != self.last_db_error:              # full error in the log, once
                log.warning("database: %s", detail)
                self.last_db_error = detail
            if "password" in detail:
                hint = "Wrong DB password?"
            elif "does not exist" in detail:
                hint = "Database missing?"
            else:
                hint = "Is PostgreSQL running?"
            return status_line("ERR", "DB DOWN", "Can't reach the DB", hint, "journalctl -u ais-display")
        except psycopg.errors.UndefinedTable:
            return status_line("ERR", "NO SCHEMA", "Database tables", "missing: apply", "schema.sql")

    def _status(self):
        run = self._one(
            """SELECT run_id, version,
                      extract(epoch FROM now() - started_at),
                      extract(epoch FROM now() - last_heartbeat),
                      stopped_at IS NOT NULL,
                      extract(epoch FROM now() - stopped_at),
                      coalesce(stop_reason, '')
               FROM collector_runs WHERE source = %s
               ORDER BY run_id DESC LIMIT 1""", (self.source,))
        if run is None:
            return status_line("WARN", "NOT STARTED", "The collector has", "never run", "")
        run_id, version, uptime, hb_age, stopped, stopped_ago, reason = run
        version = (version or "?").split("+")[0]

        if stopped:
            why = "cleanly" if reason == "clean shutdown" else "unexpectedly"
            return status_line("ERR", "COLLECTOR OFF", f"Stopped {why}", f"{ago(stopped_ago)} ago",
                               "and not restarted")
        if hb_age > HEARTBEAT_LATE:
            return status_line("ERR", "COLLECTOR DOWN", "No heartbeat for", ago(hb_age),
                               "check its logs")

        feed = self._one(
            """SELECT event_type, coalesce(detail, ''), extract(epoch FROM now() - event_time)
               FROM feed_events
               WHERE run_id = %s AND event_type IN ('connected', 'disconnected', 'stale', 'error')
               ORDER BY event_id DESC LIMIT 1""", (run_id,))
        if feed is None:
            return status_line("WARN", "CONNECTING", "Collector started", f"{ago(uptime)} ago", "")
        state, detail, state_age = feed
        if state == "error":
            return status_line("ERR", "FEED ERROR", detail.replace("server says: ", ""),
                               f"since {ago(state_age)} ago", "")
        if state in ("disconnected", "stale"):
            return status_line("WARN", "FEED DOWN", "Reconnecting...", f"down {ago(state_age)}",
                               f"collector {version}")

        msgs = self._one("SELECT count(*) FROM ais_raw WHERE source = %s "
                         "AND received_at > now() - interval '60 seconds'", (self.source,))[0]
        if msgs == 0:
            return status_line("WARN", "NO MESSAGES", "Connected, but", "nothing in 60s",
                               f"connected {ago(state_age)}")
        dropped = self._one("SELECT count(*) FROM feed_events WHERE run_id = %s "
                            "AND event_type = 'dropped' AND event_time > now() - interval '10 minutes'",
                            (run_id,))[0]
        if dropped:
            return status_line("WARN", "BUFFER FULL", "Database too slow:", "messages lost",
                               "in the last 10 min")

        # Healthy: alternate between two pages every 10 seconds.
        if int(time.time() // 10) % 2 == 0:
            ships = self._one("SELECT count(DISTINCT mmsi) FROM ais_raw WHERE source = %s "
                              "AND received_at > now() - interval '10 minutes'", (self.source,))[0]
            gaps, gap_secs = self._one(
                "SELECT count(*), coalesce(sum(extract(epoch FROM duration)), 0) FROM collection_gaps "
                "WHERE source = %s AND gap_end > now() - interval '24 hours'", (self.source,))
            gap_text = f"gaps 24h: {gaps}" + (f" ({ago(gap_secs)})" if gaps else "")
            return status_line("OK", "AIS OK", f"{msgs / 60:.1f} msg/s", f"{ships} ships / 10 min",
                               gap_text)
        db_size = self._one("SELECT pg_database_size(current_database())")[0]
        try:
            free = f"disk {size(shutil.disk_usage(self.disk_path).free)} free"
        except OSError:
            free = "disk: unknown"
        return status_line("OK", "AIS OK", f"DB {size(db_size)}", free,
                           f"{version} up {span(uptime)}")


# ---------------------------------------------------------------- dongle

def find_port(explicit):
    if explicit:
        return explicit
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        if p.vid == ESPRESSIF_VID:
            return p.device
    return None


class Dongle:
    def __init__(self, port):
        self.port_arg = port
        self.ser = None
        self.last_problem = None      # log each problem once, not every 5 seconds

    def problem(self, message):
        if message != self.last_problem:
            log.warning("%s (will keep trying every %d s)", message, INTERVAL)
            self.last_problem = message

    def send(self, line):
        if self.ser is None:
            port = find_port(self.port_arg)
            if port is None:
                self.problem("display not found; is the T-Dongle-S3 plugged in?")
                return False
            try:
                self.ser = serial.Serial(port, 115200, timeout=1, write_timeout=2)
            except (serial.SerialException, OSError) as e:
                hint = " (add your user to the dialout group)" if "ermission" in str(e) else ""
                self.problem(f"could not open {port}: {e}{hint}")
                return False
            self.last_problem = None
            log.info("display connected on %s", port)
        try:
            self.ser.write(line.encode("ascii"))
            self.ser.reset_input_buffer()     # discard anything the dongle printed
            return True
        except (serial.SerialException, OSError) as e:
            self.problem(f"display disconnected ({e})")
            self.close()
            return False

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except (serial.SerialException, OSError):
                pass
        self.ser = None


DEMO = [
    lambda: status_line("OK", "AIS OK", "7.2 msg/s", "143 ships / 10 min", "gaps 24h: 0"),
    lambda: status_line("OK", "AIS OK", "DB 3.2 GB", "disk 88.4 GB free", "0.3.0 up 3d 4h"),
    lambda: status_line("WARN", "FEED DOWN", "Reconnecting...", "down 45s", "collector 0.3.0"),
    lambda: status_line("ERR", "COLLECTOR DOWN", "No heartbeat for", "4m", "check its logs"),
    lambda: status_line("ERR", "FEED ERROR", "Api Key Is Not Valid", "since 2m ago", ""),
]


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="serial port of the dongle (default: find it automatically)")
    p.add_argument("--print", action="store_true", help="print status lines instead of sending them")
    p.add_argument("--demo", action="store_true", help="cycle through sample screens, no database needed")
    args = p.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format=f"%(asctime)s [display {VERSION}] %(levelname)-7s %(message)s",
                        stream=sys.stdout)
    load_env_file(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if psycopg is None and not args.demo:
        log.error("psycopg is not installed; run: venv/bin/pip install -r requirements.txt")
        return 2

    health = Health(os.environ.get("AIS_SOURCE", "aisstream"), os.environ.get("DISPLAY_DISK_PATH", "/"))
    dongle = None if args.print else Dongle(args.port)
    last_title = None
    step = 0
    log.info("status display %s starting", VERSION)
    try:
        while True:
            started = time.monotonic()
            if args.demo:
                line = DEMO[step % len(DEMO)]()
                step += 1
            else:
                line = health.status()
            if args.print:
                print(line, end="", flush=True)
            else:
                dongle.send(line)
            title = line.split("|")[2]
            if title != last_title:                 # log changes, not every update
                log.info("status: %s", " | ".join(line.strip().split("|")[1:]))
                last_title = title
            time.sleep(max(0.0, (3 if args.demo else INTERVAL) - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        if dongle is not None:
            if dongle.ser is not None:
                dongle.send(status_line("WARN", "SCRIPT STOPPED", "The display service", "was stopped", ""))
            dongle.close()
        log.info("stopped")


if __name__ == "__main__":
    sys.exit(main())
