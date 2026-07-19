#!/usr/bin/env python3
"""Journal → Ledger correspondence: write each new michaeljoelhall.com journal entry
(the star-enrichment "the Ledger wrote back") into the synced calendar day tree as a
correspondence ReadingEvent.

This is Michael's PERSONAL server automation — the app carries no knowledge of it. The
app renders any readingEvent whose `source` starts with "↩" as a correspondence reply
(never a feed, so it can't be re-starred) and feeds it to Ask/synthesis. Anyone else's
app simply has no such events.

TWO THINGS TO CONFIRM BEFORE ENABLING (--commit):
  1. JOURNAL_FEED_URL must resolve to ONLY the star-enrichment entries. The default
     ?post_type=social_archive is too broad (it also returns Pickings/Field Ledger/etc.).
     Point it at the star-enrichment category/tag feed, e.g. a category feed.
  2. LEDGER_CALENDAR_ROOT must be the exact tree the devices sync (both toolsboox and
     toolsforboox calendar trees are live) — verify a test write reaches a device's Log
     before wiring the cron.

Design:
- Idempotent: a state file records which journal-entry ids we've already written.
- Merge-safe: we only APPEND a readingEvent with a unique id ("reply-<guid>") to the day
  file; the device-side union merge folds it in without disturbing device edits. Never
  removes or rewrites existing events. Atomic temp+rename write.
- Non-destructive by default: pass --commit to actually write; otherwise dry-run.

Env:
  DIGEST_API_KEY        (optional) auth for the WP journal endpoint, if used
  LEDGER_CALENDAR_ROOT  the synced tree whose <root>/calendar/YYYY/MM/day-*.json the
                        devices read. MUST be confirmed to be the folder iOS+Boox sync,
                        or the events won't reach devices. No safe default — required.
  JOURNAL_FEED_URL      default https://michaeljoelhall.com/feed/?post_type=social_archive
  JOURNAL_STATE_FILE    default <this dir>/journal_to_ledger_state.json
"""
import os
import sys
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
import urllib.request
from datetime import date
from pathlib import Path

FEED_URL = os.environ.get("JOURNAL_FEED_URL",
                          "https://michaeljoelhall.com/feed/?post_type=social_archive")
STATE_FILE = os.environ.get("JOURNAL_STATE_FILE",
                            str(Path(__file__).with_name("journal_to_ledger_state.json")))
CALENDAR_ROOT = os.environ.get("LEDGER_CALENDAR_ROOT", "")

CORRESPONDENCE_SOURCE = "↩ the Ledger wrote back"


def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&[a-z]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_state():
    try:
        return set(json.loads(Path(STATE_FILE).read_text()).get("written", []))
    except Exception:
        return set()


def save_state(ids):
    tmp = STATE_FILE + ".tmp"
    Path(tmp).write_text(json.dumps({"written": sorted(ids)}, indent=2))
    os.replace(tmp, STATE_FILE)


def fetch_entries():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "LedgerJournal/1.0"})
    raw = urllib.request.urlopen(req, timeout=40).read()
    root = ET.fromstring(raw)
    out = []
    for item in root.iter("item"):
        def t(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""
        guid = t("guid") or t("link")
        if not guid:
            continue
        out.append({
            "id": guid,
            "title": t("title"),
            "link": t("link"),
            "desc": strip_html(t("description"))[:1200],
        })
    return out


def day_file(root, d):
    return Path(root) / "calendar" / f"{d.year:04d}" / f"{d.month:02d}" / f"day-{d.isoformat()}-v2.json"


def append_event(root, d, event, commit):
    fp = day_file(root, d)
    if fp.exists():
        try:
            day = json.loads(fp.read_text())
        except Exception as e:
            print(f"  ! {fp} unreadable ({e}); skipping to avoid clobber")
            return False
    else:
        day = {"year": d.year, "month": d.month, "day": d.day, "readingEvents": []}
    events = day.setdefault("readingEvents", [])
    if any(ev.get("id") == event["id"] for ev in events):
        return True   # already present
    events.append(event)
    if commit:
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(fp) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(day, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, fp)
    return True


def main():
    commit = "--commit" in sys.argv
    if commit and not CALENDAR_ROOT:
        sys.exit("LEDGER_CALENDAR_ROOT must be set (the confirmed synced calendar tree) to --commit.")
    written = load_state()
    entries = fetch_entries()
    today = date.today()
    new = 0
    for e in entries:
        if e["id"] in written:
            continue
        event = {
            "id": f"reply-{uuid.uuid5(uuid.NAMESPACE_URL, e['id']).hex[:16]}",
            "kind": "article",                 # wire kind — both apps decode it
            "date": int(time.time() * 1000),
            "title": e["title"],
            "source": CORRESPONDENCE_SOURCE,   # "↩" flags it as correspondence in the Log
            "url": e["link"] or None,
            "excerpt": e["desc"] or None,
        }
        ok = append_event(CALENDAR_ROOT, today, event, commit) if CALENDAR_ROOT else True
        print(f"  {'✓ wrote' if (commit and ok) else 'would write'}: {e['title'][:56]}")
        if ok:
            written.add(e["id"])
            new += 1
    if commit and new:
        save_state(written)
    print(f"{'committed' if commit else 'dry-run'}: {new} new correspondence event(s); "
          f"root={CALENDAR_ROOT or '(unset — dry-run only)'}")


if __name__ == "__main__":
    main()
