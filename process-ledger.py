#!/usr/bin/env python3
"""
Ultrabridge Ledger Processor

Watches for Tools for Boox day JSON files uploaded via WebDAV, renders the
full planner page to PNG with template grid overlay, OCRs everything with
Claude Sonnet, creates CalDAV tasks from unchecked items (with due date
parsing), and optionally forwards structured data to a webhook.

https://github.com/mjhfunctionalashtanga/ultrabridge-ledger

Dependencies: requests Pillow anthropic
"""

import json
import hashlib
import os
import time
from pathlib import Path
from io import BytesIO
import base64
import logging

import requests
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ledger] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ledger")

# --- Configuration (from env or .env file) ---
WEBDAV_JSON_DIR = os.environ.get(
    "LEDGER_JSON_DIR",
    "/docker/ultrabridge/ultrabridge-data/tab8/toolsboox/ToolsForBoox/json",
)
# Parent directory containing per-device subfolders. If set, the processor will
# merge JSON files for the same date across all devices before OCR'ing.
LEDGER_DATA_ROOT = os.environ.get(
    "LEDGER_DATA_ROOT",
    "/docker/ultrabridge/ultrabridge-data",
)
STATE_FILE = os.environ.get(
    "LEDGER_STATE_FILE",
    "/opt/ultrabridge-ledger/state.json",
)
WEBHOOK_URL = os.environ.get(
    "LEDGER_WEBHOOK_URL",
    "",
)
WEBHOOK_SECRET = os.environ.get("LEDGER_WEBHOOK_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
UB_TASKS_URL = os.environ.get("UB_TASKS_URL", "https://ultrabridge.mjh.yoga/tasks")
UB_TASKS_USER = os.environ.get("UB_TASKS_USER", "admin")
UB_TASKS_PASS = os.environ.get("UB_TASKS_PASS", "")
TASK_CREATE_LOOKBACK_DAYS = int(os.environ.get("TASK_CREATE_LOOKBACK_DAYS", "7"))

# Pickings → journal (michaeljoelhall.com social_archive) + mjh.yoga /notes/ mirror.
SOCIAL_ARCHIVE_URL = os.environ.get("SOCIAL_ARCHIVE_URL", "https://michaeljoelhall.com/wp-json/mjh/v1/social-archive")
SOCIAL_ARCHIVE_SECRET = os.environ.get("SOCIAL_ARCHIVE_SECRET", "")
NOTES_CREATE_TAGGED_URL = os.environ.get("NOTES_CREATE_TAGGED_URL", "https://mjh.yoga/wp-json/mjh/v1/notes/create-tagged")
NOTES_INGEST_SECRET = os.environ.get("NOTES_INGEST_SECRET", "")

# --- Grid constants (from CalendarDayPage.kt) ---
PAGE_W, PAGE_H = 1404, 1872
CEW, CEH = 600, 50
LO, TO = 20, 61

# Section boundaries
SCHED_X1, SCHED_X2 = LO, LO + CEW  # 20, 620
SCHED_LABEL_X = LO + 120  # divider between hour labels and content
SCHED_Y1 = TO + CEH  # 111
TASKS_X1 = LO + CEW + 50  # 670
TASKS_X2 = LO + 2 * CEW + 50  # 1270
TASKS_Y1 = TO + CEH  # 111
TASKS_Y2 = TO + 17 * CEH  # 911
TASK_ROWS = 16
CB_X1, CB_X2 = LO + CEW + 60, LO + CEW + 90
CONTENT_DIVIDER_X = LO + CEW + 100  # 720
NOTES_Y1 = TO + 19 * CEH  # 1011


def load_state():
    # A corrupt/truncated state file (the disk-full incident) must not kill the cron:
    # sideline it and start clean — the content hashes rebuild on the next pass.
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            quarantine = f"{STATE_FILE}.corrupt-{int(time.time())}"
            log.error(f"state file unreadable ({e}); sidelining to {quarantine}")
            try:
                os.replace(STATE_FILE, quarantine)
            except OSError:
                pass
    return {}


def save_state(state):
    # Atomic: a crash or full disk mid-write must never truncate the live state file.
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def android_color_to_rgb(color_int):
    unsigned = color_int & 0xFFFFFFFF
    r = (unsigned >> 16) & 0xFF
    g = (unsigned >> 8) & 0xFF
    b = unsigned & 0xFF
    return (r, g, b)


def render_full_page(cal_strokes, note_strokes, start_hour, scale=1):
    """Render all strokes onto the template grid as a PNG."""
    img_w, img_h = PAGE_W * scale, PAGE_H * scale
    img = Image.new("RGB", (img_w, img_h), (255, 255, 253))
    draw = ImageDraw.Draw(img)

    s = scale  # shorthand

    # --- Draw template grid ---
    grey80 = (80, 80, 80)
    grey50 = (180, 180, 180)
    grey20 = (235, 235, 230)
    white = (255, 255, 255)

    # Schedules title bar
    draw.rectangle([LO*s, TO*s, (LO+CEW)*s, (TO+CEH)*s], fill=grey80)
    # Schedules grid
    for i in range(1, 35):
        y = (TO + i * CEH) * s
        if i % 2 == 0:
            draw.rectangle([(LO+120)*s, y, (LO+CEW)*s, y + CEH*s], fill=grey20)
        draw.line([(LO*s, y), ((LO+CEW)*s, y)], fill=grey50, width=1)
    # Hour labels
    if start_hour is not None and start_hour >= 0:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14 * s)
        except (IOError, OSError):
            font = ImageFont.load_default()
        for i in range(0, 17):
            hour = start_hour + i
            label = f"{hour}:00"
            y_pos = (TO + (1 + i * 2) * CEH + 15) * s
            draw.text((30 * s, y_pos), label, fill=(60, 60, 60), font=font)
    # Schedules vertical divider
    draw.line([((LO+120)*s, (TO+CEH)*s), ((LO+120)*s, (TO+35*CEH)*s)], fill=(30,30,30), width=1)

    # Tasks title bar
    draw.rectangle([TASKS_X1*s, TO*s, TASKS_X2*s, (TO+CEH)*s], fill=grey80)
    # Tasks grid + checkboxes
    for i in range(1, TASK_ROWS + 1):
        y = (TO + i * CEH) * s
        if i % 2 == 0:
            draw.rectangle([TASKS_X1*s, y, TASKS_X2*s, y + CEH*s], fill=grey20)
        draw.line([(TASKS_X1*s, y), (TASKS_X2*s, y)], fill=grey50, width=1)
        # Checkbox square
        cb_y1 = (TO + i * CEH + 10) * s
        cb_y2 = (TO + i * CEH + 40) * s
        draw.rectangle([CB_X1*s, cb_y1, CB_X2*s, cb_y2], outline=grey50, width=1)
    # Tasks vertical divider
    draw.line([(CONTENT_DIVIDER_X*s, (TO+CEH)*s), (CONTENT_DIVIDER_X*s, TASKS_Y2*s)], fill=(30,30,30), width=1)

    # Notes title bar
    notes_title_y = (TO + 18 * CEH) * s
    draw.rectangle([TASKS_X1*s, notes_title_y, TASKS_X2*s, notes_title_y + CEH*s], fill=grey80)
    # Notes grid
    for i in range(19, 36):
        y = (TO + i * CEH) * s
        if i % 2 == 0:
            draw.rectangle([TASKS_X1*s, y, TASKS_X2*s, y + CEH*s], fill=grey20)
        draw.line([(TASKS_X1*s, y), (TASKS_X2*s, y)], fill=grey50, width=1)

    # --- Draw strokes ---
    def draw_strokes(strokes):
        for stroke in strokes:
            pts = stroke.get("strokePoints", [])
            if len(pts) < 2:
                continue
            color = android_color_to_rgb(stroke.get("color", -16777216))
            width = max(1, round(stroke.get("strokeWidth", 3.0) * s * 0.8))
            coords = [(p["x"] * s, p["y"] * s) for p in pts]
            draw.line(coords, fill=color, width=width, joint="curve")

    draw_strokes(cal_strokes)
    draw_strokes(note_strokes)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_note_page(strokes, scale=1):
    """Render a single note page's strokes onto a blank lined background as PNG."""
    img_w, img_h = PAGE_W * scale, PAGE_H * scale
    img = Image.new("RGB", (img_w, img_h), (255, 255, 253))
    draw = ImageDraw.Draw(img)
    s = scale

    # Light horizontal rules every 50px for OCR context
    rule_grey = (220, 220, 215)
    for y in range(int(TO * s), img_h, int(CEH * s)):
        draw.line([(LO * s, y), ((LO + 2 * CEW + 50) * s, y)], fill=rule_grey, width=1)

    for stroke in strokes:
        pts = stroke.get("strokePoints", [])
        if len(pts) < 2:
            continue
        color = android_color_to_rgb(stroke.get("color", -16777216))
        width = max(1, round(stroke.get("strokeWidth", 3.0) * s * 0.8))
        coords = [(p["x"] * s, p["y"] * s) for p in pts]
        draw.line(coords, fill=color, width=width, joint="curve")

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# Doodle area on the "gratitude" note page (in canvas coordinates).
# Strokes whose centroid lands in this box are treated as a sketch
# and uploaded to mjh.yoga as a tagged Sketch note.
DOODLE_BOX = (60, 1005, 1344, 1820)  # left, top, right, bottom


def extract_doodle_strokes(gratitude_strokes):
    """Return strokes whose centroid falls inside the gratitude page's doodle box."""
    if not gratitude_strokes:
        return []
    left, top, right, bottom = DOODLE_BOX
    out = []
    for stroke in gratitude_strokes:
        pts = stroke.get("strokePoints") or []
        if not pts:
            continue
        avg_x = sum(p["x"] for p in pts) / len(pts)
        avg_y = sum(p["y"] for p in pts) / len(pts)
        if left <= avg_x <= right and top <= avg_y <= bottom:
            out.append(stroke)
    return out


def render_doodle_png(strokes, scale=2):
    """Render doodle strokes as a tightly cropped PNG. Returns bytes or None."""
    if not strokes:
        return None
    all_x = []
    all_y = []
    for s in strokes:
        for p in s.get("strokePoints") or []:
            all_x.append(p["x"])
            all_y.append(p["y"])
    if not all_x:
        return None
    # Render the FULL landscape doodle box (like the capture card on the gratitude page)
    # rather than a tight crop to the strokes — a tight crop makes the aspect ratio follow
    # whatever was drawn, so a roughly square sketch reads as square/portrait. Using the box
    # keeps the doodle in its true landscape frame and position.
    x_min, y_min, x_max, y_max = DOODLE_BOX

    w = max(1, (x_max - x_min) * scale)
    h = max(1, (y_max - y_min) * scale)
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    for stroke in strokes:
        pts = stroke.get("strokePoints") or []
        if len(pts) < 2:
            continue
        color = android_color_to_rgb(stroke.get("color", -16777216))
        width = max(1, round(stroke.get("strokeWidth", 3.0) * scale * 0.8))
        coords = [((p["x"] - x_min) * scale, (p["y"] - y_min) * scale) for p in pts]
        draw.line(coords, fill=color, width=width, joint="curve")

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class TransientOcrError(Exception):
    """OCR failed for a reason that should retry next pass (API outage, rate limit).

    Distinct from the deliberate config skips (no API key / no package), which still
    return None: a transient failure must NOT let the day be marked processed, or a
    blip in the API becomes a permanent gap in the corpus.
    """


def ocr_note_page(png_data):
    """Send a single note page to Sonnet for freeform OCR."""
    if not ANTHROPIC_API_KEY:
        log.warning("No ANTHROPIC_API_KEY set, skipping note-page OCR")
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except ImportError:
        log.warning("anthropic package not installed, skipping note-page OCR")
        return None

    prompt = (
        "This is a scanned handwritten note page from a planner journal. "
        "Transcribe ALL handwritten text exactly as written, preserving line breaks "
        "between distinct thoughts and paragraphs.\n\n"
        "Rules:\n"
        "- Return ONLY the transcribed text, no commentary, no formatting markers\n"
        "- Use blank lines between paragraphs\n"
        "- If text is illegible, write [illegible] inline — do not guess\n"
        "- If the page is entirely blank, return an empty string"
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(png_data).decode(),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.error(f"Note page OCR failed: {e}")
        raise TransientOcrError(str(e))


def ocr_full_page(png_data, start_hour):
    """Send the full rendered page to Sonnet for complete OCR."""
    if not ANTHROPIC_API_KEY:
        log.warning("No ANTHROPIC_API_KEY set, skipping OCR")
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except ImportError:
        log.warning("anthropic package not installed, skipping OCR")
        return None

    hour_hint = ""
    if start_hour is not None and start_hour >= 0:
        hours = [f"{start_hour + i}:00/{start_hour + i}:30" for i in range(0, 17)]
        hour_hint = f" The schedule runs from {start_hour}:00 to {start_hour + 17}:00."

    prompt = (
        "This is a scanned daily planner page with three sections of handwriting:\n\n"
        "1. **SCHEDULE** (left column) — hourly time slots with handwritten appointments/notes.{hour_hint}\n"
        "2. **TASKS** (right column, top) — 16 checkbox rows. Each has a small square checkbox on the left "
        "and handwritten text to the right. A checkbox with marks through it means checked/done.\n"
        "3. **NOTES** (right column, bottom) — freeform handwritten notes.\n\n"
        "Transcribe ALL handwritten content. Return ONLY valid JSON in this exact format:\n"
        '{{\n'
        '  "schedule": [{{"time": "HH:MM", "text": "transcribed text"}}],\n'
        '  "tasks": [{{"row": 1, "checked": false, "text": "transcribed text"}}],\n'
        '  "notes": "transcribed freeform text"\n'
        '}}\n\n'
        "Rules:\n"
        "- Only include entries that have handwriting (skip empty rows/slots)\n"
        "- For tasks, set checked=true if the checkbox has marks through it\n"
        "- For schedule, use the time label from the left margin\n"
        "- If text is illegible, write \"[illegible]\" — do not guess\n"
        "- Return ONLY the JSON, no other text"
    ).format(hour_hint=hour_hint)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(png_data).decode(),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Bad JSON from the model is NOT transient — retrying re-bills the same page
        # every pass. Skip it like before; a stroke edit will re-hash and retry it.
        log.error(f"OCR returned invalid JSON: {e}")
        log.error(f"Raw response: {text[:500]}")
        return None
    except Exception as e:
        log.error(f"OCR API call failed: {e}")
        raise TransientOcrError(str(e))


def parse_due_date(text, fallback_date):
    """Extract a due date from task text. Returns (cleaned_title, date_str).

    Matches patterns like:
      due 1/1/2027, due: 01/01/2027, due 2027-01-01,
      due jan 1, due january 1 2027, due 1/1
    An optional connector word is accepted after "due", so the natural
    phrasings "due by 6/8/2026" and "due on Jan 15" also parse.
    Strips the matched portion from the title.
    Falls back to the planner page date if no due date found.
    """
    import re
    from datetime import date

    # due MM/DD/YYYY or M/D/YYYY or MM/DD/YY
    m = re.search(r'\bdue(?:\s+(?:by|on))?[:\s]+(\d{1,2})/(\d{1,2})/(\d{2,4})\b', text, re.IGNORECASE)
    if m:
        month, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        title = (text[:m.start()] + text[m.end():]).strip(' ,;-')
        return title, f"{yr:04d}-{month:02d}-{day:02d}"

    # due YYYY-MM-DD
    m = re.search(r'\bdue(?:\s+(?:by|on))?[:\s]+(\d{4})-(\d{1,2})-(\d{1,2})\b', text, re.IGNORECASE)
    if m:
        title = (text[:m.start()] + text[m.end():]).strip(' ,;-')
        return title, f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # due MM/DD (no year — assume current year or next year if date has passed)
    m = re.search(r'\bdue(?:\s+(?:by|on))?[:\s]+(\d{1,2})/(\d{1,2})\b', text, re.IGNORECASE)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        today = date.today()
        yr = today.year
        try:
            d = date(yr, month, day)
            if d < today:
                yr += 1
        except ValueError:
            return text, fallback_date
        title = (text[:m.start()] + text[m.end():]).strip(' ,;-')
        return title, f"{yr:04d}-{month:02d}-{day:02d}"

    # due Month DD [YYYY] — e.g. "due Jan 15" or "due January 15 2027"
    months = {
        'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
        'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6,
        'jul': 7, 'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'september': 9,
        'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
    }
    m = re.search(
        r'\bdue(?:\s+(?:by|on))?[:\s]+(' + '|'.join(months.keys()) + r')\s+(\d{1,2})(?:\s+(\d{4}))?\b',
        text, re.IGNORECASE,
    )
    if m:
        month = months[m.group(1).lower()]
        day = int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else date.today().year
        try:
            d = date(yr, month, day)
            if not m.group(3) and d < date.today():
                yr += 1
        except ValueError:
            return text, fallback_date
        title = (text[:m.start()] + text[m.end():]).strip(' ,;-')
        return title, f"{yr:04d}-{month:02d}-{day:02d}"

    return text, fallback_date


def create_ultrabridge_task(title, due_date_str=None, description=None):
    """Create a task in Ultrabridge via CalDAV PUT with description."""
    if not UB_TASKS_PASS:
        return False
    import uuid
    task_uid = uuid.uuid4().hex
    due_line = ""
    if due_date_str:
        due_line = f"DUE:{due_date_str.replace('-', '')}T235900Z\r\n"
    desc_line = ""
    if description:
        escaped = description.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
        desc_line = f"DESCRIPTION:{escaped}\r\n"
    now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    vcal = (
        "BEGIN:VCALENDAR\r\n"
        "PRODID:-//Ledger Processor//EN\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VTODO\r\n"
        f"UID:{task_uid}\r\n"
        f"DTSTAMP:{now}\r\n"
        f"SUMMARY:{title}\r\n"
        f"{desc_line}"
        f"{due_line}"
        "STATUS:NEEDS-ACTION\r\n"
        "END:VTODO\r\n"
        "END:VCALENDAR\r\n"
    )
    caldav_url = UB_TASKS_URL.replace("/tasks", f"/caldav/user/calendars/tasks/{task_uid}.ics")
    try:
        resp = requests.put(
            caldav_url,
            data=vcal,
            headers={"Content-Type": "text/calendar"},
            auth=(UB_TASKS_USER, UB_TASKS_PASS),
            timeout=15,
        )
        if resp.status_code in (200, 201, 204):
            log.info(f"Created Ultrabridge task: {title}")
            return True
        else:
            log.error(f"Ultrabridge task creation failed ({resp.status_code}): {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"Ultrabridge task creation error: {e}")
        return False


def forward_to_webhook(payload):
    """POST processed data to an optional webhook endpoint."""
    if not WEBHOOK_URL:
        return False

    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SECRET:
        headers["X-Ledger-Secret"] = WEBHOOK_SECRET

    try:
        resp = requests.post(WEBHOOK_URL, json=payload, headers=headers, timeout=30)
        if resp.ok:
            log.info(f"Webhook forwarded: {resp.status_code}")
            return True
        else:
            log.error(f"Webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"Webhook forward failed: {e}")
        return False


def merge_day_data(file_paths):
    """Merge multiple device JSONs for the same date into one data dict.

    Strokes are deduplicated by strokeId. Scalar metadata (year/month/day/
    startHour/locale) is taken from any file (they're invariant per date).
    hasLanes is OR'd across all sources.
    """
    merged = {}
    all_cal_by_style = {}  # style -> {strokeId: stroke}
    all_notes_by_page = {}  # page_key -> {strokeId: stroke}
    all_text_by_id = {}  # elementId -> textElement (deduped across device merges)
    all_text_noid = []   # text boxes with no elementId (legacy) — kept as-is
    all_images_by_id = {}  # elementId -> imageElement (deduped across device merges)
    has_lanes = False
    latest_updated = ""

    # Read everything first so the merge can (a) honour erase tombstones from EVERY
    # device — ignoring deletedStrokeIds meant erased ink resurrected in the OCR and
    # re-created CalDAV tasks — and (b) apply files oldest→newest so the NEWEST
    # device's copy of a shared id wins (path sort order used to pick the winner,
    # so a stale mirror could revert edits). Tombstone ids compare lowercased:
    # Android writes lowercase UUIDs, iOS uppercase. The tombstone arrays are NOT
    # added to `merged` — a new key would change every day's content hash and
    # re-OCR all history (the imageElements lesson).
    loaded = []
    for fp in file_paths:
        try:
            with open(fp) as f:
                loaded.append(json.load(f))
        except Exception as e:
            log.warning(f"Could not read {fp}: {e}")
    loaded.sort(key=lambda d: str(d.get("updated", "")))
    deleted_ids = set()
    for data in loaded:
        for key in ("deletedStrokeIds", "deletedElementIds"):
            for t in data.get(key) or []:
                deleted_ids.add(str(t).lower())

    for data in loaded:
        for k in ("year", "month", "day", "startHour", "locale"):
            if k in data and k not in merged:
                merged[k] = data[k]

        has_lanes = has_lanes or bool(data.get("hasLanes"))

        for style, strokes in (data.get("calendarStrokes") or {}).items():
            bucket = all_cal_by_style.setdefault(style, {})
            for s in strokes or []:
                sid = s.get("strokeId")
                if sid and str(sid).lower() not in deleted_ids:
                    bucket[sid] = s

        for page_key, strokes in (data.get("noteStrokes") or {}).items():
            bucket = all_notes_by_page.setdefault(page_key, {})
            for s in strokes or []:
                sid = s.get("strokeId")
                if sid and str(sid).lower() not in deleted_ids:
                    bucket[sid] = s

        for te in data.get("textElements") or []:
            tid = te.get("elementId")
            if tid:
                if str(tid).lower() not in deleted_ids:
                    all_text_by_id[tid] = te
            else:
                all_text_noid.append(te)

        for ie in data.get("imageElements") or []:
            iid = ie.get("elementId")
            if iid and str(iid).lower() not in deleted_ids:
                all_images_by_id[iid] = ie

        upd = str(data.get("updated", ""))
        if upd > latest_updated:
            latest_updated = upd
            # Carry through fields from the most recently updated source
            for k in ("events", "readingProgress", "calendarValues", "created"):
                if k in data:
                    merged[k] = data[k]

    merged["hasLanes"] = has_lanes
    merged["calendarStrokes"] = {style: list(strokes.values()) for style, strokes in all_cal_by_style.items()}
    merged["noteStrokes"] = {pk: list(strokes.values()) for pk, strokes in all_notes_by_page.items()}
    merged["textElements"] = list(all_text_by_id.values()) + all_text_noid
    merged["imageElements"] = list(all_images_by_id.values())
    if latest_updated:
        merged["updated"] = latest_updated

    return merged


def task_key(date_str, row, title):
    """Stable identifier for an Ultrabridge task creation."""
    return f"{date_str}|{(title or '').strip().lower()}"


# Pickings page zones (canvas coords) — must match drawPickingsPage in CalendarDayPageNotes.kt.
# Side-by-side layout: NOTES = left column, QUOTES = right column; two image tiles below.
PICKINGS_NOTES_BOX = (40, 60, 684, 1150)
PICKINGS_QUOTES_BOX = (720, 60, 1364, 1150)
PICKINGS_IMAGE_BOX_1 = (40, 1218, 684, 1862)    # IMAGE 1 tile (left)
PICKINGS_IMAGE_BOX_2 = (720, 1218, 1364, 1862)  # IMAGE 2 tile (right)


def images_in_box(images, box):
    """ImageElements whose center falls inside the given (left, top, right, bottom) box."""
    left, top, right, bottom = box
    out = []
    for ie in images or []:
        cx = float(ie.get("x", 0)) + float(ie.get("width", 0)) / 2.0
        cy = float(ie.get("y", 0)) + float(ie.get("height", 0)) / 2.0
        if left <= cx <= right and top <= cy <= bottom:
            out.append(ie)
    return out


def render_pickings_box(box, strokes, images, scale=2):
    """Flatten everything filling one Pickings image tile — any pasted image(s) PLUS the
    ink strokes drawn in or on top of them — into a single cropped PNG. Returns bytes or
    None if the tile is empty. Mirrors the Boox view: 'an image out of whatever's in the box.'"""
    left, top, right, bottom = box
    box_w, box_h = int(right - left), int(bottom - top)
    img = Image.new("RGB", (max(1, box_w * scale), max(1, box_h * scale)), (255, 255, 255))
    has_content = False

    # 1) Paste any images at their position relative to the box.
    for ie in images or []:
        b64 = ie.get("data")
        if not b64:
            continue
        try:
            sub = Image.open(BytesIO(base64.b64decode(b64))).convert("RGBA")
        except Exception as e:
            log.error(f"Pickings box image decode failed: {e}")
            continue
        iw = int(float(ie.get("width") or sub.width) * scale)
        ih = int(float(ie.get("height") or sub.height) * scale)
        if iw < 1 or ih < 1:
            continue
        sub = sub.resize((iw, ih))
        px = int((float(ie.get("x", left)) - left) * scale)
        py = int((float(ie.get("y", top)) - top) * scale)
        img.paste(sub, (px, py), sub)
        has_content = True

    # 2) Draw the ink strokes on top, offset into the box.
    draw = ImageDraw.Draw(img)
    for stroke in strokes or []:
        pts = stroke.get("strokePoints") or []
        if len(pts) < 2:
            continue
        color = android_color_to_rgb(stroke.get("color", -16777216))
        width = max(1, round(stroke.get("strokeWidth", 3.0) * scale * 0.8))
        coords = [((p["x"] - left) * scale, (p["y"] - top) * scale) for p in pts]
        draw.line(coords, fill=color, width=width, joint="curve")
        has_content = True

    if not has_content:
        return None
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def strokes_in_box(strokes, box):
    """Strokes whose centroid falls inside the given (left, top, right, bottom) box."""
    left, top, right, bottom = box
    out = []
    for stroke in strokes or []:
        pts = stroke.get("strokePoints") or []
        if not pts:
            continue
        ax = sum(p["x"] for p in pts) / len(pts)
        ay = sum(p["y"] for p in pts) / len(pts)
        if left <= ax <= right and top <= ay <= bottom:
            out.append(stroke)
    return out


def post_doodle(date_str, timestamp, doodle_png):
    """Post the gratitude-page doodle to the journal (michaeljoelhall.com) as an image entry.
    Journal-only: the mjh.yoga sketch note already rides the webhook (sketch_png_b64)."""
    if not SOCIAL_ARCHIVE_SECRET:
        log.warning("No SOCIAL_ARCHIVE_SECRET set; skipping Doodle journal post")
        return
    if not doodle_png:
        return
    headers = {"X-MJH-Secret": SOCIAL_ARCHIVE_SECRET, "Content-Type": "application/json"}
    b64 = base64.b64encode(doodle_png).decode()
    try:
        from datetime import datetime as _dt, timedelta as _td
        from zoneinfo import ZoneInfo as _ZI
        _ddts = int((_dt.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_ZI("America/New_York")) + _td(days=1, minutes=1)).timestamp())
    except Exception:
        _ddts = timestamp + 86400
    body = {
        "platform": "doodle", "type": "image",
        "source_id": f"doodle-{date_str}", "timestamp": _ddts,  # end-of-day gratitude doodle surfaces at the start of the next day
        "title": f"Doodle · {date_str}", "caption": "",
        "media": [{"data_base64": b64, "kind": "image", "filename": f"doodle-{date_str}.png"}],
        "tags": ["doodle"],
    }
    try:
        r = requests.post(SOCIAL_ARCHIVE_URL, json=body, headers=headers, timeout=90)
        log.info(f"Doodle -> journal: {r.status_code}")
    except Exception as e:
        log.error(f"Doodle journal post failed: {e}")


def post_pickings(date_str, timestamp, image_elements, notes_text, quotes_text,
                  notes_png=None, quotes_png=None):
    """Post a Pickings page to the journal (michaeljoelhall.com) + mirror to mjh.yoga /notes/.
    Notes and Quotes each become their OWN entry — the rendered handwriting image plus the
    OCR transcription as the caption — so the journal shows both in its own box."""
    if not SOCIAL_ARCHIVE_SECRET:
        log.warning("No SOCIAL_ARCHIVE_SECRET set; skipping Pickings journal post")
        return
    headers = {"X-MJH-Secret": SOCIAL_ARCHIVE_SECRET, "Content-Type": "application/json"}

    notes_text = (notes_text or "").strip()
    quotes_text = (quotes_text or "").strip()

    # Pickings = 4 independent items per day (Notes, Quotes, Image 1, Image 2).
    # The notes/quotes PNGs already include any images placed inside their own boxes
    # (handled upstream in render_pickings_box).
    hand_urls = {}
    for kind, label, text, png in (("notes", "Notes", notes_text, notes_png),
                                   ("quotes", "Quotes", quotes_text, quotes_png)):
        if not text and not png:
            continue
        media = []
        if png:
            media.append({"data_base64": base64.b64encode(png).decode(), "kind": "image",
                          "filename": f"pickings-{date_str}-{kind}.png"})
        body = {
            "platform": "pickings", "type": "note",
            "source_id": f"pickings-{date_str}-{kind}", "timestamp": timestamp,
            "title": f"Pickings · {date_str} — {label}",
            "caption": text, "media": media, "tags": ["pickings", kind],
        }
        try:
            r = requests.post(SOCIAL_ARCHIVE_URL, json=body, headers=headers, timeout=90)
            log.info(f"Pickings {kind} -> journal: {r.status_code}")
            if r.ok:
                for m in (r.json().get("media") or []):
                    if m.get("url"):
                        hand_urls[kind] = m["url"]
        except Exception as e:
            log.error(f"Pickings {kind} post failed: {e}")

    # Image 1 + Image 2 -> their own entries (square composites of pasted photo + ink).
    image_urls = []
    for i, ie in enumerate(image_elements, 1):
        b64 = ie.get("data")
        if not b64:
            continue
        body = {
            "platform": "pickings", "type": "image",
            "source_id": f"pickings-{date_str}-img{i}", "timestamp": timestamp,
            "title": f"Pickings · {date_str}", "caption": "",
            "media": [{"data_base64": b64, "kind": "image", "filename": f"pickings-{date_str}-{i}.png"}],
            "tags": ["pickings"],
        }
        try:
            r = requests.post(SOCIAL_ARCHIVE_URL, json=body, headers=headers, timeout=90)
            log.info(f"Pickings image {i} -> journal: {r.status_code}")
            if r.ok:
                for m in (r.json().get("media") or []):
                    if m.get("url"):
                        image_urls.append(m["url"])
        except Exception as e:
            log.error(f"Pickings image {i} post failed: {e}")

    # 3) Mirror to mjh.yoga /notes/ as one Pickings note (handwriting + text + images)
    if NOTES_INGEST_SECRET and (notes_text or quotes_text or image_urls):
        html = []
        if notes_text or hand_urls.get("notes"):
            html.append("<h3>Notes</h3>")
            if hand_urls.get("notes"):
                html.append(f'<p><img src="{hand_urls["notes"]}" style="max-width:100%;border-radius:4px"></p>')
            if notes_text:
                html.append("<p>" + notes_text.replace("\n", "<br>") + "</p>")
        if quotes_text or hand_urls.get("quotes"):
            html.append("<h3>Quotes</h3>")
            if hand_urls.get("quotes"):
                html.append(f'<p><img src="{hand_urls["quotes"]}" style="max-width:100%;border-radius:4px"></p>')
            if quotes_text:
                html.append("<blockquote>" + quotes_text.replace("\n", "<br>") + "</blockquote>")
        for u in image_urls:
            html.append(f'<p><img src="{u}" style="max-width:100%;border-radius:4px"></p>')
        html.append(f'<p><a href="https://michaeljoelhall.com/journal/{date_str}/">View in the journal &rarr;</a></p>')
        note_body = {
            "title": f"Pickings — {date_str}",
            "body": "".join(html),
            "tags": ["Pickings"],
            "idem_key": f"pickings-{date_str}",
        }
        try:
            r = requests.post(
                NOTES_CREATE_TAGGED_URL, json=note_body,
                headers={"X-MJH-Ingest-Secret": NOTES_INGEST_SECRET, "Content-Type": "application/json"}, timeout=30
            )
            log.info(f"Pickings -> mjh.yoga note: {r.status_code}")
        except Exception as e:
            log.error(f"Pickings mjh.yoga note failed: {e}")


def post_text_elements(date_str, timestamp, text_elements):
    """Forward typed/pasted text boxes on a day page to BOTH the field ledger
    (michaeljoelhall.com journal) and mjh.yoga /notes/.

    Pasted text is already digital text — no OCR, no stroke rendering — so it ships
    verbatim. The server-side pipeline otherwise only OCRs rendered strokes, so text
    boxes used to reach the VPS in the merged JSON and then go nowhere. All of a day's
    text boxes combine into one entry per destination; stable source_id / idem_key let
    the endpoints dedup across the 15-minute cron re-runs (same pattern as pickings)."""
    texts = []
    for te in text_elements or []:
        t = (te.get("text") or "").strip()
        if t:
            texts.append(t)
    if not texts:
        return

    combined = "\n\n".join(texts)

    # 1) Field ledger on michaeljoelhall.com (journal social-archive).
    if SOCIAL_ARCHIVE_SECRET:
        body = {
            "platform": "ledger", "type": "note",
            "source_id": f"text-{date_str}", "timestamp": timestamp,
            "title": f"Field Ledger · {date_str} — Text",
            "caption": combined, "media": [], "tags": ["field-ledger", "text"],
        }
        try:
            r = requests.post(
                SOCIAL_ARCHIVE_URL, json=body,
                headers={"X-MJH-Secret": SOCIAL_ARCHIVE_SECRET, "Content-Type": "application/json"},
                timeout=90,
            )
            log.info(f"Text elements -> journal: {r.status_code} ({len(texts)} box(es))")
        except Exception as e:
            log.error(f"Text elements journal post failed: {e}")

    # 2) mjh.yoga /notes/.
    if NOTES_INGEST_SECRET:
        html = "".join("<p>" + t.replace("\n", "<br>") + "</p>" for t in texts)
        html += f'<p><a href="https://michaeljoelhall.com/journal/{date_str}/">View in the journal &rarr;</a></p>'
        note_body = {
            "title": f"Ledger Text — {date_str}",
            "body": html,
            "tags": ["Ledger"],
            "idem_key": f"text-{date_str}",
        }
        try:
            r = requests.post(
                NOTES_CREATE_TAGGED_URL, json=note_body,
                headers={"X-MJH-Ingest-Secret": NOTES_INGEST_SECRET, "Content-Type": "application/json"},
                timeout=30,
            )
            log.info(f"Text elements -> mjh.yoga note: {r.status_code}")
        except Exception as e:
            log.error(f"Text elements mjh.yoga note failed: {e}")


def process_day(merged_data, label):
    """Process merged JSON data for a single date.

    label is a string identifier used in log messages (e.g. the JSON filename).
    """
    log.info(f"Processing {label}")

    data = merged_data
    year = data.get("year", 0)
    month = data.get("month", 0)
    day = data.get("day", 0)
    start_hour = data.get("startHour")

    if year < 2020 or not (1 <= month <= 12) or not (1 <= day <= 31):
        log.warning(f"Invalid date in {label}, skipping")
        return

    # Forward typed/pasted text boxes BEFORE the stroke checks below — a page can carry
    # pasted text with no handwriting at all, and the "No strokes, skipping" guard would
    # otherwise drop it. Text is already digital, so no rendering/OCR is needed.
    # Skip intake-page text boxes — those are share-to-Ledger URLs the device already
    # delivered to the intake pipeline; forwarding them here would duplicate raw links
    # into the field ledger. Everything else (pickings, day, etc.) is genuine pasted text.
    text_elements = [te for te in (data.get("textElements") or []) if te.get("pageKey") != "intake"]
    if text_elements:
        from datetime import datetime as _dt, timezone as _tz
        t_date = f"{year:04d}-{month:02d}-{day:02d}"
        t_ts = int(_dt(year, month, day, 12, 0, tzinfo=_tz.utc).timestamp())
        post_text_elements(t_date, t_ts, text_elements)

    # Flatten calendarStrokes
    all_cal = []
    for style_strokes in (data.get("calendarStrokes") or {}).values():
        if isinstance(style_strokes, list):
            all_cal.extend(style_strokes)

    # Keep note pages separate, keyed by page number string
    note_strokes_by_page = {}
    for page_key, page_strokes in (data.get("noteStrokes") or {}).items():
        if isinstance(page_strokes, list) and page_strokes:
            note_strokes_by_page[page_key] = page_strokes

    if not all_cal and not note_strokes_by_page:
        log.info(f"No strokes in {label}, skipping")
        return

    # Render the day page with template grid + calendar strokes only (no note pages here)
    png = render_full_page(all_cal, [], start_hour, scale=2)

    # OCR the day page
    ocr = ocr_full_page(png, start_hour)

    # OCR each note page separately
    note_page_texts = {}
    for page_key in sorted(note_strokes_by_page.keys(), key=lambda k: int(k) if k.isdigit() else 999):
        page_png = render_note_page(note_strokes_by_page[page_key], scale=2)
        text = ocr_note_page(page_png)
        if text:
            note_page_texts[page_key] = text
            log.info(f"OCR'd note page {page_key} for {label}: {len(text)} chars")

    # Build payload for mjh.yoga
    payload = {
        "year": year,
        "month": month,
        "day": day,
        "startHour": start_hour,
        "calendarStrokes": data.get("calendarStrokes", {}),
        "noteStrokes": data.get("noteStrokes", {}),
    }
    # Full-page handwriting render -> featured image on the ledger note
    payload["page_png_b64"] = base64.b64encode(png).decode()

    task_texts = {}
    task_checked = {}
    if ocr:
        for t in ocr.get("tasks", []):
            if isinstance(t, dict) and t.get("text"):
                task_texts[t["row"]] = t["text"]
                task_checked[t["row"]] = t.get("checked", False)
        payload["task_texts"] = task_texts
        payload["task_checked"] = task_checked
        payload["schedule_texts"] = ocr.get("schedule", [])
        payload["notes_text"] = ocr.get("notes", "")

    if note_page_texts:
        payload["note_page_texts"] = note_page_texts

    # Doodle from the gratitude page → separate Sketch note. Composite any pasted image(s)
    # in the doodle box PLUS the ink strokes, on the full landscape box (like the capture card).
    gratitude_strokes = note_strokes_by_page.get("gratitude") or []
    doodle_strokes = extract_doodle_strokes(gratitude_strokes)
    gratitude_images = [ie for ie in (data.get("imageElements") or []) if ie.get("page") == "gratitude"]
    doodle_images = images_in_box(gratitude_images, DOODLE_BOX)
    if doodle_strokes or doodle_images:
        doodle_png = render_pickings_box(DOODLE_BOX, doodle_strokes, doodle_images, scale=2)
        if doodle_png:
            payload["sketch_png_b64"] = base64.b64encode(doodle_png).decode()
            log.info(f"Extracted doodle for {label}: {len(doodle_strokes)} strokes, {len(doodle_images)} images, {len(doodle_png)} bytes")
            from datetime import datetime, timezone
            ddate = f"{year:04d}-{month:02d}-{day:02d}"
            try:
                dts = int(datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp())
            except Exception:
                dts = int(time.time())
            post_doodle(ddate, dts, doodle_png)

    # Pickings page -> journal (michaeljoelhall.com) + mjh.yoga /notes/
    pickings_strokes = note_strokes_by_page.get("pickings") or []
    pickings_images = [ie for ie in (data.get("imageElements") or []) if ie.get("page") == "pickings"]
    if pickings_strokes or pickings_images:
        notes_zone   = strokes_in_box(pickings_strokes, PICKINGS_NOTES_BOX)
        quotes_zone  = strokes_in_box(pickings_strokes, PICKINGS_QUOTES_BOX)
        # Images placed WITHIN the notes/quotes boxes themselves (a quote can be an image
        # at the top + handwriting below — both belong to the same composite).
        notes_imgs   = images_in_box(pickings_images, PICKINGS_NOTES_BOX)
        quotes_imgs  = images_in_box(pickings_images, PICKINGS_QUOTES_BOX)
        notes_text   = ocr_note_page(render_note_page(notes_zone, scale=2)) if notes_zone else ""
        quotes_text  = ocr_note_page(render_note_page(quotes_zone, scale=2)) if quotes_zone else ""
        # Composite (strokes + in-box images) for each text zone.
        notes_png = (render_pickings_box(PICKINGS_NOTES_BOX, notes_zone, notes_imgs, scale=2)
                     if (notes_zone or notes_imgs) else None)
        quotes_png = (render_pickings_box(PICKINGS_QUOTES_BOX, quotes_zone, quotes_imgs, scale=2)
                      if (quotes_zone or quotes_imgs) else None)
        from datetime import datetime, timezone
        pdate = f"{year:04d}-{month:02d}-{day:02d}"
        try:
            pts = int(datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp())
        except Exception:
            pts = int(time.time())
        # Each image tile -> one flattened composite (pasted photo(s) + ink drawn on/in it).
        box_images = []
        for box in (PICKINGS_IMAGE_BOX_1, PICKINGS_IMAGE_BOX_2):
            b_imgs = images_in_box(pickings_images, box)
            b_strokes = strokes_in_box(pickings_strokes, box)
            png = render_pickings_box(box, b_strokes, b_imgs, scale=2)
            if png:
                box_images.append({"data": base64.b64encode(png).decode()})
        log.info(f"Pickings for {label}: notes_zone={len(notes_zone)} quotes_zone={len(quotes_zone)} tiles={len(box_images)}")
        post_pickings(pdate, pts, box_images, notes_text or "", quotes_text or "", notes_png, quotes_png)

    # MichaelFilter intake page -> mjh.yoga intake endpoint (four panels: THE READ /
    # THE WATCH / THE LISTEN / EDUCATE ME). Crop each panel's ink like the pickings
    # zones, then hand off to intake_page (OCR + web-search resolution / educate
    # overview + POST with the panel image). Per-panel content-hash idempotency
    # lives in the state file ("intake_sent"), handled inside the module.
    intake_strokes = note_strokes_by_page.get("intake") or []
    intake_images = [ie for ie in (data.get("imageElements") or []) if ie.get("page") == "intake"]
    if intake_strokes or intake_images:
        try:
            import intake_page
            idate = f"{year:04d}-{month:02d}-{day:02d}"
            panels = {}
            for kind, box in intake_page.INTAKE_PANELS:
                p_strokes = strokes_in_box(intake_strokes, box)
                p_images = images_in_box(intake_images, box)
                if not p_strokes and not p_images:
                    continue
                p_png = render_pickings_box(box, p_strokes, p_images, scale=2)
                if not p_png:
                    continue
                panels[kind] = {"png": p_png, "hash": intake_page.panel_hash(p_strokes, p_images)}
            if panels:
                log.info(f"Intake page for {label}: panels={sorted(panels.keys())}")
                intake_page.process_intake_panels(idate, panels, STATE_FILE)
        except Exception as e:
            log.error(f"Intake page processing failed for {label}: {e}")

    # Create Ultrabridge tasks for unchecked items on recent dates
    if task_texts:
        from datetime import date
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        try:
            page_date = date.fromisoformat(date_str)
            is_recent = (date.today() - page_date).days <= TASK_CREATE_LOOKBACK_DAYS
        except ValueError:
            is_recent = False

        if is_recent and UB_TASKS_PASS:
            # Persist which tasks we've already pushed so the same (date, row, title)
            # never gets sent twice — multi-device merges and stroke edits to the
            # same date used to recreate every task on every reprocess.
            state = load_state()
            sent = set(state.get("tasks_sent", []))
            new_keys = []
            date_label = page_date.strftime('%B %d, %Y')
            for row_num, text in task_texts.items():
                if not task_checked.get(row_num, False) and text:
                    task_title, task_due = parse_due_date(text, date_str)
                    key = task_key(date_str, row_num, task_title)
                    if key in sent:
                        continue
                    desc = f"From Boox planner {date_label}"
                    create_ultrabridge_task(task_title, task_due, desc)
                    new_keys.append(key)
            if new_keys:
                state["tasks_sent"] = sorted(sent | set(new_keys))
                save_state(state)

    # A failed forward must leave the day unprocessed so the next pass retries —
    # otherwise a webhook outage becomes a permanent gap on mjh.yoga.
    if WEBHOOK_URL and not forward_to_webhook(payload):
        raise RuntimeError(f"webhook forward failed for {label}")


def find_device_day_files():
    """Walk LEDGER_DATA_ROOT for all per-device day JSONs.

    Returns a dict: { base_filename (e.g. 'day-2026-05-27-v2.json'): [Path, Path, ...] }

    Falls back to scanning only WEBDAV_JSON_DIR if LEDGER_DATA_ROOT doesn't exist
    or contains no device subfolders.
    """
    root = Path(LEDGER_DATA_ROOT)
    by_date = {}

    if root.exists():
        # Walk every immediate subdir as a "device" folder. The planner data may live under
        # EITHER `toolsboox/ToolsForBoox/json` or `toolsforboox/ToolsForBoox/json` (the device
        # started syncing fresh pages to the latter) — scan any `*/ToolsForBoox/json` so both
        # copies are collected and merge_day_data() unions their strokes (dedup by strokeId).
        for device_dir in root.iterdir():
            if not device_dir.is_dir():
                continue
            for json_dir in device_dir.glob("*/ToolsForBoox/json"):
                if not json_dir.is_dir():
                    continue
                # day-*.json also matches -v2 files — exclude them or every v2 day is
                # listed (and read/merged/hashed) twice per pass.
                for f in list(json_dir.glob("day-*-v2.json")) + [
                    p for p in json_dir.glob("day-*.json") if not p.name.endswith("-v2.json")
                ]:
                    by_date.setdefault(f.name, []).append(f)

    if not by_date:
        # Fall back to single-dir scan
        single = Path(WEBDAV_JSON_DIR)
        if single.exists():
            for f in list(single.glob("day-*-v2.json")) + [
                p for p in single.glob("day-*.json") if not p.name.endswith("-v2.json")
            ]:
                by_date.setdefault(f.name, []).append(f)

    # If both v1 and v2 exist for the same date, prefer v2
    deduped = {}
    bases_seen = set()
    # First pass: v2 files
    for name, paths in by_date.items():
        if name.endswith("-v2.json"):
            base = name[:-8]  # strip "-v2.json"
            deduped[name] = paths
            bases_seen.add(base)
    # Second pass: v1 files only if no v2 equivalent
    for name, paths in by_date.items():
        if not name.endswith("-v2.json"):
            base = name[:-5]  # strip ".json"
            if base not in bases_seen:
                deduped[name] = paths

    return deduped


def backfill_doodles():
    """One-shot: post the doodle from every past day to the journal. Idempotent —
    the journal dedups by source_id `doodle-DATE`, so re-running is safe and only
    fills gaps. Does NOT re-OCR pages or touch the change-hash state."""
    by_date = find_device_day_files()
    posted, skipped = 0, 0
    for name in sorted(by_date.keys()):
        paths = sorted(by_date[name], key=lambda p: str(p))
        try:
            merged = merge_day_data(paths)
        except Exception as e:
            log.error(f"Backfill merge failed for {name}: {e}")
            continue
        year = merged.get("year", 0); month = merged.get("month", 0); day = merged.get("day", 0)
        if year < 2020 or not (1 <= month <= 12) or not (1 <= day <= 31):
            continue
        gratitude_strokes = (merged.get("noteStrokes") or {}).get("gratitude") or []
        doodle_strokes = extract_doodle_strokes(gratitude_strokes)
        gratitude_images = [ie for ie in (merged.get("imageElements") or []) if ie.get("page") == "gratitude"]
        doodle_images = images_in_box(gratitude_images, DOODLE_BOX)
        if not doodle_strokes and not doodle_images:
            skipped += 1
            continue
        doodle_png = render_pickings_box(DOODLE_BOX, doodle_strokes, doodle_images, scale=2)
        if not doodle_png:
            skipped += 1
            continue
        from datetime import datetime, timezone
        ddate = f"{year:04d}-{month:02d}-{day:02d}"
        try:
            dts = int(datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp())
        except Exception:
            dts = int(time.time())
        post_doodle(ddate, dts, doodle_png)
        posted += 1
    log.info(f"Doodle backfill done. {posted} doodles posted, {skipped} days had no doodle.")


def main():
    state = load_state()
    processed = state.get("processed", {})
    changed = False

    by_date = find_device_day_files()
    if not by_date:
        log.info("No day JSON files found in LEDGER_DATA_ROOT or WEBDAV_JSON_DIR")
        return

    for name in sorted(by_date.keys()):
        paths = by_date[name]
        # Sort for stable hash even if iteration order changes
        paths = sorted(paths, key=lambda p: str(p))
        # Hash the merged content so cross-device updates trigger reprocessing
        merged = merge_day_data(paths)
        # Exclude imageElements from the change-hash: it's a new key (including it would
        # re-hash and re-OCR all history once), and image edits ride along with the page's
        # stroke edits anyway — so existing days keep their original hash.
        hashable = {k: v for k, v in merged.items() if k != "imageElements"}
        merged_bytes = json.dumps(hashable, sort_keys=True, default=str).encode()
        merged_hash = hashlib.sha256(merged_bytes).hexdigest()

        if processed.get(name) == merged_hash:
            continue

        try:
            sources_label = name if len(paths) == 1 else f"{name} ({len(paths)} devices)"
            process_day(merged, sources_label)
            processed[name] = merged_hash
            changed = True
        except Exception as e:
            log.error(f"Error processing {name}: {e}")

    if changed:
        # process_day() persists tasks_sent AND intake_sent during the loop; rebase on
        # a fresh load so the end-of-run save clobbers NO mid-run key (the dupe-tasks
        # bug, later repeated by intake_sent).
        latest = load_state()
        latest["processed"] = processed
        latest["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_state(latest)

    total = len(by_date)
    up_to_date = sum(1 for name in by_date if processed.get(name))
    log.info(f"Done. {total} dates found, {up_to_date} up to date.")


if __name__ == "__main__":
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        WEBDAV_JSON_DIR = os.environ.get("LEDGER_JSON_DIR", WEBDAV_JSON_DIR)
        LEDGER_DATA_ROOT = os.environ.get("LEDGER_DATA_ROOT", LEDGER_DATA_ROOT)
        STATE_FILE = os.environ.get("LEDGER_STATE_FILE", STATE_FILE)
        WEBHOOK_URL = os.environ.get("LEDGER_WEBHOOK_URL", WEBHOOK_URL)
        WEBHOOK_SECRET = os.environ.get("LEDGER_WEBHOOK_SECRET", WEBHOOK_SECRET)
        ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)
        UB_TASKS_URL = os.environ.get("UB_TASKS_URL", UB_TASKS_URL)
        UB_TASKS_USER = os.environ.get("UB_TASKS_USER", UB_TASKS_USER)
        UB_TASKS_PASS = os.environ.get("UB_TASKS_PASS", UB_TASKS_PASS)
        TASK_CREATE_LOOKBACK_DAYS = int(os.environ.get("TASK_CREATE_LOOKBACK_DAYS", str(TASK_CREATE_LOOKBACK_DAYS)))
        SOCIAL_ARCHIVE_URL = os.environ.get("SOCIAL_ARCHIVE_URL", SOCIAL_ARCHIVE_URL)
        SOCIAL_ARCHIVE_SECRET = os.environ.get("SOCIAL_ARCHIVE_SECRET", SOCIAL_ARCHIVE_SECRET)
        NOTES_CREATE_TAGGED_URL = os.environ.get("NOTES_CREATE_TAGGED_URL", NOTES_CREATE_TAGGED_URL)
        NOTES_INGEST_SECRET = os.environ.get("NOTES_INGEST_SECRET", NOTES_INGEST_SECRET)

    # Single-instance guard: stop a manual run and the 15-min cron (or two
    # crons) from running concurrently and double-creating tasks.
    import fcntl, sys
    _lock_fh = open("/tmp/ledger-processor.lock", "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another process-ledger.py run is in progress; exiting.")
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "backfill-doodles":
        backfill_doodles()
    else:
        main()
