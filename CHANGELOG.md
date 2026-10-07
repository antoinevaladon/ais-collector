# Changelog

Each release bumps `VERSION` in `collector.py`. Releases that change `schema.sql` also bump `SCHEMA_VERSION` (in `collector.py`) and the `schema_version` value at the bottom of `schema.sql`, and the collector won't start until the new schema is applied.

## 0.3.0 (schema 2)

- New: status display for a LilyGO T-Dongle-S3 plugged into the Pi (`display/`). Firmware for the dongle (compile-tested on ESP32 Arduino core 3.3.12) and a separate service on the Pi that reads the collector's health from the database every 5 seconds. Shows OK / feed down / no messages / buffer full / feed error / collector off or frozen / database down, plus a watchdog that turns the screen red if the Pi stops sending. Every message is checked to fit the 160×80 screen. See `display/README.md`.
- `display/screens.png`: an exact render of the display's screens, shown in both READMEs.
- `requirements.txt` adds `pyserial`. After pulling, run `venv/bin/pip install -r requirements.txt`.
- `.env.example` adds `DISPLAY_DISK_PATH`.
- No collector or schema changes.

## 0.2.2 (schema 2)

- Default collection area widened from Haiti and its approaches (`17,-81,27,-68`) to the Caribbean basin (`10,-83,31,-59`): the Florida peninsula and Keys, the Bahamas, Cuba, Jamaica, Hispaniola, Puerto Rico, the Lesser Antilles out to Barbados and Trinidad, and the Caribbean coast of Colombia and Venezuela.

## 0.2.1 (schema 2)

- Ready for GitHub: README covers cloning and pushing (`gh repo create`).
- The systemd service installs from any folder, not only `~/ais-collector`, so a clone under any name works.

## 0.2.0 (schema 2)

- Every log line shows the exact version that wrote it, such as `[0.2.0+git.1a2b3c4]`. From a git checkout it includes the commit, plus `.modified` if files changed since; without git it includes a fingerprint of `collector.py`, so local edits still show up.
- Each run records its full version and runtime (Python, library, PostgreSQL and OS versions) in `collector_runs`.
- Each message records the run that stored it (`ais_raw.run_id`), and so the version. Upgrading tags messages stored by 0.1.0 automatically.
- Gaps show the version that was running when they began.
- The collector checks the schema version at startup and says what to run if it's out of date.
- `schema.sql` upgrades an existing database in place and applies all-or-nothing.

Upgrading from 0.1.0: replace the files, run `psql -h localhost -U ais -d ais -f schema.sql`, then `sudo systemctl restart ais-collector`.

## 0.1.0 (schema 1)

- First release: aisstream.io to PostgreSQL with monthly partitions, automatic reconnects, stale-feed detection, in-memory buffering during database outages, single-instance lock, and the gap log (`collector_runs`, `feed_events`, `feed_coverage`, `collection_gaps`).
