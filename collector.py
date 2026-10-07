#!/usr/bin/env python3
"""
AIS collector: streams live AIS messages from aisstream.io into PostgreSQL.

Built to run unattended for months on a Raspberry Pi:
  * reconnects automatically, with backoff, when the feed drops
  * forces a reconnect if the feed goes silent for too long
  * timestamps every message the moment it arrives, then writes in batches
  * holds messages in memory while the database is briefly unavailable
  * records every run, disconnect and silent period in the gap log, so a
    missing hour can always be explained ("we were down" vs. "no ships")
  * refuses to start if another copy is already running

Settings come from environment variables, or from a .env file next to this
script (see .env.example). Run it directly to test; let systemd run it
permanently (see ais-collector.service).
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import platform
import random
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
import websockets
from websockets.asyncio.client import connect

# Bump VERSION with every change you deploy, and note it in CHANGELOG.md.
# Bump SCHEMA_VERSION whenever schema.sql changes (it must match the value
# schema.sql writes into schema_info).
VERSION = "0.2.2"
SCHEMA_VERSION = 2

DEFAULT_URL = "wss://stream.aisstream.io/v0/stream"
DEFAULT_BBOXES = "10,-83,31,-59"   # Caribbean basin: Florida peninsula and Keys to Trinidad, Bahamas to Colombia
CLEAN_STOP = "clean shutdown"       # schema.sql treats any other stop_reason as a crash
UNCLEAN_STOP = "no clean shutdown recorded (crash, kill, power loss, or stopped while the database was down)"

log = logging.getLogger("ais-collector")

COPY_SQL = ("COPY ais_raw (received_at, run_id, source, message_type, mmsi, ship_name, lat, lon, payload) "
            "FROM STDIN")
INSERT_SQL = ("INSERT INTO ais_raw (received_at, run_id, source, message_type, mmsi, ship_name, lat, lon, payload) "
              "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)")
EVENT_SQL = ("INSERT INTO feed_events (run_id, event_time, event_type, since, detail) "
             "VALUES (%s, %s, %s, %s, %s)")
HEARTBEAT_SQL = ("UPDATE collector_runs SET last_heartbeat = now(), messages_received = %s, "
                 "messages_written = %s, messages_dropped = %s WHERE run_id = %s")


class StaleFeed(Exception):
    """Connected, but no messages arrived for too long."""


class FeedError(Exception):
    """The server sent an error message (for example, an invalid API key)."""


class AlreadyRunning(Exception):
    """Another collector instance holds the lock."""


class SchemaOutdated(Exception):
    """The database schema is older than this collector needs."""


# ---------------------------------------------------------------- version

def build_version():
    """VERSION plus exactly which code is running.

    In a git checkout:  0.2.0+git.1a2b3c4  (".modified" if files changed since that commit)
    Otherwise:          0.2.0+file.9f8e7d6c (fingerprint of this file, so local edits still show)
    No network needed, so it works offline and at boot.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        def git(*args):
            return subprocess.run(["git", *args], cwd=here, capture_output=True, text=True,
                                  timeout=5, check=True).stdout.strip()
        git("ls-files", "--error-unmatch", os.path.basename(__file__))   # is this file in the repo?
        commit = git("rev-parse", "--short", "HEAD")
        modified = git("status", "--porcelain", "--untracked-files=no", "--", ".")
        return f"{VERSION}+git.{commit}" + (".modified" if modified else "")
    except (OSError, subprocess.SubprocessError):
        pass                            # no git, not a checkout, or this file isn't committed
    with open(os.path.abspath(__file__), "rb") as f:
        return f"{VERSION}+file.{hashlib.sha256(f.read()).hexdigest()[:8]}"


def runtime_info(conn):
    """Versions of everything around the collector, recorded with each run."""
    pg = conn.info.server_version       # e.g. 150008 for PostgreSQL 15.8
    return (f"python {platform.python_version()} | websockets {websockets.__version__} | "
            f"psycopg {psycopg.__version__} | postgres {pg // 10000}.{pg % 100} | "
            f"{platform.system()} {platform.release()} {platform.machine()}")


# ---------------------------------------------------------------- settings

def load_env_file(path):
    """Minimal .env reader so the script also works outside systemd (e.g. in Thonny)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_bboxes(spec):
    """'lat_min,lon_min,lat_max,lon_max', several boxes separated by ';'."""
    boxes = []
    for part in spec.split(";"):
        if not part.strip():
            continue
        try:
            lat1, lon1, lat2, lon2 = (float(x) for x in part.split(","))
        except ValueError:
            raise ValueError(f"bad bounding box {part!r}; expected lat_min,lon_min,lat_max,lon_max")
        if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90 and -180 <= lon1 <= 180 and -180 <= lon2 <= 180):
            raise ValueError(f"bounding box {part!r} is outside valid latitude/longitude ranges")
        boxes.append([[lat1, lon1], [lat2, lon2]])   # aisstream wants [lat, lon] corners
    if not boxes:
        raise ValueError("AIS_BBOXES is empty")
    return boxes


@dataclass
class Config:
    api_key: str
    url: str
    source: str
    boxes: list
    message_types: list
    stale_seconds: float
    batch_size: int
    flush_seconds: float
    queue_max: int
    heartbeat_seconds: float

    @classmethod
    def from_env(cls):
        env = os.environ.get
        api_key = env("AISSTREAM_API_KEY", "").strip()
        if not api_key or api_key == "your-api-key-here":
            raise ValueError("AISSTREAM_API_KEY is not set (put it in .env)")
        return cls(
            api_key=api_key,
            url=env("AISSTREAM_URL", DEFAULT_URL),
            source=env("AIS_SOURCE", "aisstream"),
            boxes=parse_bboxes(env("AIS_BBOXES", DEFAULT_BBOXES)),
            message_types=[t.strip() for t in env("AIS_MESSAGE_TYPES", "").split(",") if t.strip()],
            stale_seconds=float(env("STALE_SECONDS", "300")),
            batch_size=int(env("BATCH_SIZE", "500")),
            flush_seconds=float(env("FLUSH_SECONDS", "1.0")),
            queue_max=int(env("QUEUE_MAX", "100000")),
            heartbeat_seconds=float(env("HEARTBEAT_SECONDS", "10")),
        )


# ---------------------------------------------------------------- helpers

def utcnow():
    return datetime.now(timezone.utc)


def to_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def to_mmsi(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 2_147_483_647 else None


def clean_name(value):
    if not isinstance(value, str):
        return None
    # AIS pads names with spaces or '@'; PostgreSQL text cannot hold NUL characters
    return value.replace("\x00", "").strip().rstrip("@").strip() or None


def describe(exc):
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# ---------------------------------------------------------------- collector

class Collector:
    def __init__(self, cfg, version):
        self.cfg = cfg
        self.version = version
        self.queue = asyncio.Queue(maxsize=cfg.queue_max)
        self.pending_events = []        # (event_time, event_type, since, detail), waiting to be written
        self.lock_name = f"ais-collector:{cfg.source}"
        self.control = None             # connection holding the single-instance lock
        self.run_id = None
        self.received = self.written = self.dropped = self.rejected = self.malformed = 0
        self.dropped_unreported = 0
        self.first_drop_at = None
        self.connected = False
        self.last_message_at = None
        self.draining = False
        self.stop_requested = asyncio.Event()

    # ---- events and incoming messages

    def event(self, kind, detail=None, since=None):
        self.pending_events.append((utcnow(), kind, since, detail))
        level = logging.INFO if kind == "connected" else logging.WARNING
        log.log(level, "feed %s%s", kind, f": {detail}" if detail else "")

    def handle_raw(self, raw, now):
        """Queue one message for the database. Returns True if it was an AIS message."""
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if "\\u0000" in raw:            # jsonb cannot store NUL characters
            raw = raw.replace("\\u0000", "")
        try:
            msg = json.loads(raw)
        except ValueError:
            self.malformed += 1
            log.warning("skipped malformed message: %.120r", raw)
            return False
        if not isinstance(msg, dict):
            self.malformed += 1
            return False
        if "error" in msg:
            raise FeedError(str(msg["error"]))
        mtype = msg.get("MessageType")
        if mtype == "SubscriptionConfirmation":
            log.info("subscription confirmed by server")
            return False
        if not isinstance(mtype, str) or not mtype:
            self.malformed += 1
            log.warning("skipped message without a MessageType: %.120r", raw)
            return False

        meta = msg.get("MetaData") if isinstance(msg.get("MetaData"), dict) else {}
        mmsi = meta.get("MMSI")
        if mmsi is None:                # fall back to the AIS UserID inside the message body
            body = msg.get("Message")
            inner = body.get(mtype) if isinstance(body, dict) else None
            if isinstance(inner, dict):
                mmsi = inner.get("UserID")
        lat = meta.get("latitude", meta.get("Latitude"))
        lon = meta.get("longitude", meta.get("Longitude"))
        row = (now, self.run_id, self.cfg.source, mtype, to_mmsi(mmsi), clean_name(meta.get("ShipName")),
               to_float(lat), to_float(lon), raw)

        self.received += 1
        self.last_message_at = now
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            if self.dropped_unreported == 0:
                self.first_drop_at = now
            self.dropped += 1
            self.dropped_unreported += 1
        return True

    # ---- receiving

    def subscription(self):
        sub = {"APIKey": self.cfg.api_key, "BoundingBoxes": self.cfg.boxes}
        if self.cfg.message_types:
            sub["FilterMessageTypes"] = self.cfg.message_types
        return json.dumps(sub)

    async def receive_forever(self):
        delay = 1.0
        subscription = self.subscription()
        while True:
            conn_started = last_in_conn = None
            try:
                log.info("connecting to %s", self.cfg.url)
                # aisstream needs compression to serve the full message rate
                async with connect(self.cfg.url, compression="deflate", open_timeout=20,
                                   ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    await ws.send(subscription)   # must arrive within 3 s of connecting
                    conn_started = utcnow()
                    self.connected = True
                    self.event("connected", f"subscribed to {len(self.cfg.boxes)} bounding box(es)")
                    try:
                        while True:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=self.cfg.stale_seconds)
                            except asyncio.TimeoutError:
                                raise StaleFeed(f"no messages for {self.cfg.stale_seconds:.0f} s") from None
                            now = utcnow()
                            if self.handle_raw(raw, now):
                                last_in_conn = now
                                delay = 1.0   # data is flowing again, so reset the backoff
                    except asyncio.CancelledError:
                        await ws.close(reason="collector shutting down")   # polite close on shutdown
                        raise
            except asyncio.CancelledError:
                raise
            except StaleFeed as e:
                self.event("stale", f"{e}; reconnecting", since=last_in_conn or conn_started)
            except FeedError as e:
                self.event("error", f"server says: {e}", since=last_in_conn or conn_started)
                delay = max(delay, 60.0)      # don't hammer the server with, say, a bad API key
            except Exception as e:
                if conn_started:
                    self.event("disconnected", describe(e), since=last_in_conn or conn_started)
                else:
                    log.warning("connection attempt failed: %s", describe(e))
            finally:
                self.connected = False
            wait = delay + random.uniform(0, delay / 2)
            log.info("reconnecting in %.0f s", wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, 120.0)

    # ---- writing

    async def next_batch(self):
        """Wait for messages, then gather more for up to FLUSH_SECONDS or BATCH_SIZE."""
        try:
            first = await asyncio.wait_for(self.queue.get(), timeout=self.cfg.flush_seconds)
        except asyncio.TimeoutError:
            return []
        batch = [first]
        deadline = time.monotonic() + self.cfg.flush_seconds
        while len(batch) < self.cfg.batch_size:
            remaining = 0 if self.draining else deadline - time.monotonic()
            try:
                if remaining <= 0:
                    batch.append(self.queue.get_nowait())
                else:
                    batch.append(await asyncio.wait_for(self.queue.get(), timeout=remaining))
            except (asyncio.QueueEmpty, asyncio.TimeoutError):
                break
        return batch

    async def flush_events(self, conn):
        if not self.pending_events or self.run_id is None:
            return
        events = list(self.pending_events)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.executemany(EVENT_SQL, [(self.run_id, *e) for e in events])
        del self.pending_events[:len(events)]

    async def write_rows(self, conn, rows):
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    async with cur.copy(COPY_SQL) as copy:
                        for row in rows:
                            await copy.write_row(row)
            return len(rows)
        except psycopg.DataError:
            # One bad row fails the whole COPY. Retry row by row so a single
            # malformed message can't block everything behind it.
            written = 0
            for row in rows:
                try:
                    async with conn.transaction():
                        await conn.execute(INSERT_SQL, row)
                    written += 1
                except psycopg.DataError as e:
                    self.rejected += 1
                    log.warning("rejected a message the database could not store (%s): %.200s",
                                str(e).splitlines()[0], row[-1])
            return written

    async def write_forever(self):
        conn = None
        batch = []
        delay = 1.0
        try:
            while True:
                if not batch:
                    batch = await self.next_batch()
                if not batch and not self.pending_events:
                    if self.draining and self.queue.empty():
                        return
                    continue
                try:
                    if conn is None or conn.closed:
                        conn = await psycopg.AsyncConnection.connect(autocommit=True, connect_timeout=10)
                    await self.flush_events(conn)
                    if batch:
                        self.written += await self.write_rows(conn, batch)
                        batch = []
                    delay = 1.0
                except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                    log.warning("database write failed (%s); retrying in %.0f s with %d messages waiting",
                                describe(e).splitlines()[0], delay, len(batch) + self.queue.qsize())
                    if conn is not None:
                        await conn.close()
                    conn = None
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5.0)   # retry often: every second of delay risks a full buffer
        finally:
            if conn is not None:
                await conn.close()

    # ---- control connection: lock, run bookkeeping, heartbeat, maintenance

    async def open_control(self):
        conn = await psycopg.AsyncConnection.connect(autocommit=True, connect_timeout=10)
        cur = await conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (self.lock_name,))
        if not (await cur.fetchone())[0]:
            await conn.close()
            raise AlreadyRunning()
        return conn

    async def wait_for_database(self):
        """At boot PostgreSQL may not be up yet, so keep trying until it is."""
        delay = 2.0
        while True:
            try:
                return await self.open_control()
            except psycopg.OperationalError as e:
                log.warning("waiting for database (%s)", describe(e).splitlines()[0])
            try:
                await asyncio.wait_for(self.stop_requested.wait(), timeout=delay)
                return None
            except asyncio.TimeoutError:
                delay = min(delay * 2, 30.0)

    async def check_schema(self):
        """Stop early, with instructions, if schema.sql hasn't been applied or updated."""
        try:
            cur = await self.control.execute("SELECT value FROM schema_info WHERE key = 'schema_version'")
            row = await cur.fetchone()
            found = int(row[0]) if row else 0
        except psycopg.errors.UndefinedTable:
            found = 0
        if found < SCHEMA_VERSION:
            raise SchemaOutdated(found)
        if found > SCHEMA_VERSION:
            log.warning("database schema is version %d, newer than this collector expects (%d); "
                        "consider updating the collector", found, SCHEMA_VERSION)

    async def start_run(self):
        conn = self.control
        runtime = runtime_info(conn)
        async with conn.transaction():
            await conn.execute("SELECT ensure_ais_partitions(2)")
            # Close out runs that never recorded a clean stop. The end time is the
            # last message they wrote (found just after their last heartbeat).
            cur = await conn.execute(
                """UPDATE collector_runs r
                   SET stop_reason = %s,
                       stopped_at = GREATEST(r.last_heartbeat, (
                           SELECT max(a.received_at) FROM ais_raw a
                           WHERE a.source = r.source
                             AND a.received_at BETWEEN r.last_heartbeat
                                                   AND r.last_heartbeat + interval '5 minutes'))
                   WHERE r.stopped_at IS NULL AND r.source = %s
                   RETURNING r.run_id""",
                (UNCLEAN_STOP, self.cfg.source))
            unclean = [r[0] for r in await cur.fetchall()]
            cur = await conn.execute(
                "INSERT INTO collector_runs (source, hostname, version, runtime) "
                "VALUES (%s, %s, %s, %s) RETURNING run_id",
                (self.cfg.source, socket.gethostname(), self.version, runtime))
            self.run_id = (await cur.fetchone())[0]
        if unclean:
            log.warning("previous run(s) %s ended without a clean shutdown; recorded as a gap",
                        ", ".join(map(str, unclean)))
        log.info("run %s started on %s (%d bounding box(es)) | %s",
                 self.run_id, socket.gethostname(), len(self.cfg.boxes), runtime)

    async def control_forever(self):
        last_maintenance = last_stats = time.monotonic()
        count_at_last_stats = 0
        while True:
            await asyncio.sleep(self.cfg.heartbeat_seconds)
            if self.dropped_unreported:
                n, self.dropped_unreported = self.dropped_unreported, 0
                self.event("dropped", f"{n} messages lost because the buffer was full",
                           since=self.first_drop_at)
            try:
                if self.control is None or self.control.closed:
                    self.control = await self.open_control()   # AlreadyRunning ends this run
                    log.info("control connection restored")
                await self.control.execute(
                    HEARTBEAT_SQL, (self.received, self.written, self.dropped, self.run_id))
                if time.monotonic() - last_maintenance > 6 * 3600:
                    await self.control.execute("SELECT ensure_ais_partitions(2)")
                    last_maintenance = time.monotonic()
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                log.warning("heartbeat failed (%s)", describe(e).splitlines()[0])
                if self.control is not None:
                    await self.control.close()
                self.control = None
            except psycopg.Error as e:
                log.error("maintenance error: %s", describe(e))

            elapsed = time.monotonic() - last_stats
            if elapsed >= 60:
                rate = (self.received - count_at_last_stats) / elapsed
                log.info("%.1f msg/s | received %d, written %d, waiting %d, dropped %d, rejected %d | feed %s",
                         rate, self.received, self.written, self.queue.qsize(), self.dropped,
                         self.rejected, "connected" if self.connected else "DOWN")
                last_stats, count_at_last_stats = time.monotonic(), self.received

    async def finish_run(self, reason):
        if self.run_id is None:
            return
        try:
            async with await psycopg.AsyncConnection.connect(autocommit=True, connect_timeout=5) as conn:
                await self.flush_events(conn)
                await conn.execute(
                    "UPDATE collector_runs SET stopped_at = now(), last_heartbeat = now(), stop_reason = %s, "
                    "messages_received = %s, messages_written = %s, messages_dropped = %s WHERE run_id = %s",
                    (reason, self.received, self.written, self.dropped, self.run_id))
        except psycopg.Error as e:
            log.error("could not record the shutdown in the database: %s", describe(e).splitlines()[0])

    # ---- main

    def request_stop(self, signame):
        if not self.stop_requested.is_set():
            log.info("%s received, shutting down", signame)
            self.stop_requested.set()

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop, sig.name)

        self.control = await self.wait_for_database()
        if self.control is None:
            return 0
        try:
            await self.check_schema()
            await self.start_run()
        except SchemaOutdated as e:
            found = e.args[0]
            log.error("database schema is %s, but this collector needs version %d. Apply it with: "
                      "psql -h localhost -U ais -d ais -f schema.sql",
                      f"version {found}" if found else "missing or from before version tracking",
                      SCHEMA_VERSION)
            await self.control.close()
            return 2

        writer = asyncio.create_task(self.write_forever(), name="writer")
        receiver = asyncio.create_task(self.receive_forever(), name="receiver")
        control = asyncio.create_task(self.control_forever(), name="control")
        stop_wait = asyncio.create_task(self.stop_requested.wait())
        done, _ = await asyncio.wait({writer, receiver, control, stop_wait},
                                     return_when=asyncio.FIRST_COMPLETED)

        reason, exit_code = CLEAN_STOP, 0
        for task in done - {stop_wait}:
            exc = task.exception()
            if isinstance(exc, AlreadyRunning):
                reason, exit_code = "another collector took over the lock", 3
                log.error("lost the single-instance lock to another collector; stopping")
            else:
                reason, exit_code = f"crashed: {describe(exc)}"[:500], 1
                log.error("%s task crashed", task.get_name(), exc_info=exc)

        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        self.draining = True
        try:
            await asyncio.wait_for(writer, timeout=15)
        except asyncio.TimeoutError:
            log.error("database unavailable at shutdown; %d buffered messages were not saved",
                      self.queue.qsize())
        except Exception as e:
            log.error("writer failed during shutdown: %s", describe(e))
        for task in (control, stop_wait):
            task.cancel()
        await asyncio.gather(control, stop_wait, return_exceptions=True)

        await self.finish_run(reason)
        if self.control is not None:
            await self.control.close()    # releases the single-instance lock
        log.info("stopped: received %d, written %d, dropped %d, rejected %d",
                 self.received, self.written, self.dropped, self.rejected)
        return exit_code


def main():
    version = build_version()
    # Every log line carries the version, so old logs always say which code wrote them.
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format=f"%(asctime)s [{version}] %(levelname)-7s %(message)s", stream=sys.stdout)
    log.info("AIS collector %s starting", version)
    load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    try:
        cfg = Config.from_env()
    except ValueError as e:
        log.error("configuration error: %s", e)
        return 2
    try:
        return asyncio.run(Collector(cfg, version).run())
    except AlreadyRunning:
        log.error("another collector is already running (if it's the service: sudo systemctl stop ais-collector)")
        return 3


if __name__ == "__main__":
    sys.exit(main())
