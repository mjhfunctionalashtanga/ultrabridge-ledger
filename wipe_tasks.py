#!/usr/bin/env python3
"""One-shot wipe of all Ultrabridge tasks via CalDAV.

Lists every task .ics in the tasks collection and DELETEs it, then clears
state.json's tasks_sent + recent-day processed hashes so the next
process-ledger.py run regenerates a clean (deduplicated) set.

Reads credentials from /opt/ultrabridge-ledger/.env (or env vars).
"""

import os
import sys
import json
import re
from pathlib import Path
import requests
from xml.etree import ElementTree as ET

ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

UB_TASKS_URL = os.environ.get("UB_TASKS_URL", "https://ultrabridge.mjh.yoga/tasks")
UB_TASKS_USER = os.environ.get("UB_TASKS_USER", "admin")
UB_TASKS_PASS = os.environ.get("UB_TASKS_PASS", "")
STATE_FILE = os.environ.get("LEDGER_STATE_FILE", "/opt/ultrabridge-ledger/state.json")

if not UB_TASKS_PASS:
    sys.exit("UB_TASKS_PASS not set")

collection = UB_TASKS_URL.replace("/tasks", "/caldav/user/calendars/tasks/")
print(f"Collection: {collection}")

propfind_body = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/></d:prop></d:propfind>'
)

r = requests.request(
    "PROPFIND",
    collection,
    data=propfind_body,
    headers={"Depth": "1", "Content-Type": "application/xml"},
    auth=(UB_TASKS_USER, UB_TASKS_PASS),
    timeout=30,
)
r.raise_for_status()

# Parse out the href elements
root = ET.fromstring(r.text)
ns = "{DAV:}"
hrefs = [el.text for el in root.iter(f"{ns}href") if el.text]
ics_hrefs = [h for h in hrefs if h.endswith(".ics")]
print(f"Found {len(ics_hrefs)} task .ics files to delete")

deleted = 0
failed = 0
base = re.match(r"https?://[^/]+", collection).group(0)
for h in ics_hrefs:
    url = h if h.startswith("http") else base + h
    resp = requests.delete(url, auth=(UB_TASKS_USER, UB_TASKS_PASS), timeout=15)
    if resp.ok:
        deleted += 1
    else:
        failed += 1
        print(f"  delete {url} → {resp.status_code}")

print(f"Deleted: {deleted}; failed: {failed}")

# Clear the state ONLY when every delete succeeded: clearing tasks_sent while some
# tasks survived on the server would recreate them as duplicates on the next pass —
# and a crash after deletion but before this point loses nothing now, because the
# next run of this script simply resumes (deletes are idempotent).
if failed:
    sys.exit(f"{failed} deletes failed — state left untouched; fix and re-run.")

state_path = Path(STATE_FILE)
if state_path.exists():
    import datetime
    lookback = int(os.environ.get("TASK_CREATE_LOOKBACK_DAYS", "7"))
    cutoff = (datetime.date.today() - datetime.timedelta(days=lookback)).isoformat()
    state = json.loads(state_path.read_text())
    state.pop("tasks_sent", None)
    processed = state.get("processed", {})
    # Only reset RECENT day hashes — the processor only creates tasks for days inside
    # the lookback window, so wiping ALL hashes just re-OCR'd (and re-billed) the
    # whole history for nothing.
    def keep(key):
        m = re.match(r"day-(\d{4}-\d{2}-\d{2})", key)
        return not m or m.group(1) < cutoff
    state["processed"] = {k: v for k, v in processed.items() if keep(k)}
    # Atomic (the state.json lesson).
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_path)
    print(f"State cleared: removed tasks_sent and reset {len(processed) - len(state['processed'])} recent day hashes")
