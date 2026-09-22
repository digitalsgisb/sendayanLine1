# Sendayan Line 1 Production Tracker

`sdy1.py` keeps the existing Sendayan Line 1 sensor-counting and timer rules,
while adding the same production lifecycle safeguards used by the ABB trackers.

## Added lifecycle features

- Restart recovery through local SQLite snapshots.
- Durable SQLite event outbox for a future central API.
- Automatic shift end calculated from `workingTime` and overtime.
- Final partial-hour record queued before shift PDF generation.
- Production-day boundary reset at 08:00.
- A 45-minute shift-entry window at 08:00, using machine status `7`.
- Products made inside that window are adopted by the submitted shift without a
  second reset; if no shift arrives, the buffered production is discarded.
- A different shift ID cannot replace an active shift merely because the form
  was submitted again.
- Retained MQTT status and live recovery metadata.

The SDY1 two-proximity/limit-switch counting flow, two-second count debounce,
10-second loading threshold, and cycle-time formula remain unchanged.

## Runtime files

By default SQLite is stored at `runtime/sdy1_state.db`. Override it with:

```env
SDY1_SQLITE_PATH=/var/lib/sdy1/sdy1_state.db
```

Optional API outbox delivery settings are:

```env
SDY1_API_ENABLED=true
SDY1_API_EVENTS_URL=http://172.19.3.72:8088/api/v1/events
SDY1_API_KEY=<private-key>
```

Do not commit `.env`, API keys, or runtime database files.

## Install and verify

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python sdy1_sqlite_status.py
mosquitto_sub -h localhost -t 'sdy1/status' -v
```

The systemd template expects this project at `/home/<user>/sdy1`:

```bash
sudo install -m 0644 deploy/sdy1@.service /etc/systemd/system/sdy1@.service
sudo systemctl daemon-reload
sudo systemctl enable --now "sdy1@$USER.service"
```

See `SQLITE_RECOVERY_GUIDE.md` for restart, buffer, and outbox behavior.

## Pull future updates on the Raspberry Pi

Check for local edits before pulling. If `git status --short` prints unexpected
files, stop and review them first. Do not discard Raspberry Pi changes or its
runtime database.

```bash
cd "$HOME/sdy1"
git status --short
git pull --ff-only origin main
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
sudo install -m 0644 deploy/sdy1@.service /etc/systemd/system/sdy1@.service
sudo systemctl daemon-reload
sudo systemctl restart "sdy1@$USER.service"
sudo systemctl status "sdy1@$USER.service" --no-pager
```

If the service does not start, inspect its latest logs:

```bash
sudo journalctl -u "sdy1@$USER.service" -n 100 --no-pager
```

## Google Apps Script deployment

`appScriptSdy1.js` is the matching backend for the Line 1 spreadsheet. Copy it
into the spreadsheet's Apps Script project and deploy a new web-app version.
Keep the deployed `/exec` URL equal to `WEB_APP_URL` in `sdy1.py`.

The backend combines repeated records in the same hour, selects the latest
same-shift details, validates allowed data tabs, and deduplicates Python HTTP
retries by `request_id`. Run `node tests/test_sdy1_appscript.js` before deployment.

### Duplicate-delivery protection

- Each data tab receives a trailing `Request_ID` column automatically. Keep this
  column in place: it is the durable idempotency record used after cache expiry,
  network timeouts, and process restarts.
- Hourly rows and shift PDFs use deterministic request IDs, so the same logical
  event is rejected even if it is queued twice or sent by a restarted client.
  MQTT reject, downtime, shift, and parameter events reuse their supplied
  `event_id` when one is available.
- Exact legacy duplicate rows are ignored when the PDF aggregates hourly,
  rejection, and downtime totals.
- `GENERATE_PDF` reuses the latest existing PDF for the same shift. Send
  `"force_regenerate": true` with a new `request_id` only when an intentional
  replacement report is required after correcting source data.
- The Python sender allows up to 120 seconds for PDF export and retries failures
  with exponential backoff while preserving the original `request_id`.

After changing `appScriptSdy1.js`, create a new Apps Script web-app deployment
version; saving the editor is not enough. Confirm the deployed `/exec` URL still
matches `WEB_APP_URL`, then update and restart the Raspberry Pi service.
