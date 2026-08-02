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
import re
import time
from pathlib import Path
from io import BytesIO
import base64
import logging

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFont

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
# The media store for by-reference day-JSON payloads (WIRE-MEDIA-BY-REFERENCE.md): once the
# devices externalize, an element carries data="" plus dataRef="<sha256>.<png|jpg>" and the
# bytes live in a media/ dir the devices sync alongside calendar/. On the dav tree that is
# <root>/media, two levels up from the json dir (.../data/ToolsForBoox/json -> .../data/media),
# which is why the default is derived rather than fixed. Env-overridable like the dirs above.
LEDGER_MEDIA_DIR = os.environ.get(
    "LEDGER_MEDIA_DIR",
    os.path.normpath(os.path.join(WEBDAV_JSON_DIR, "..", "..", "media")),
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
SERVER_TASKS_ENABLED = os.environ.get("LEDGER_SERVER_TASKS", "0") == "1"  # demoted: device pushes VTODOs now
TASK_CREATE_LOOKBACK_DAYS = int(os.environ.get("TASK_CREATE_LOOKBACK_DAYS", "7"))

# Pickings → journal (michaeljoelhall.com social_archive) + mjh.yoga /notes/ mirror.
SOCIAL_ARCHIVE_URL = os.environ.get("SOCIAL_ARCHIVE_URL", "https://michaeljoelhall.com/wp-json/mjh/v1/social-archive")
SOCIAL_ARCHIVE_SECRET = os.environ.get("SOCIAL_ARCHIVE_SECRET", "")
NOTES_CREATE_TAGGED_URL = os.environ.get("NOTES_CREATE_TAGGED_URL", "https://mjh.yoga/wp-json/mjh/v1/notes/create-tagged")
NOTES_INGEST_SECRET = os.environ.get("NOTES_INGEST_SECRET", "")

# POSSE queue (mjh-syndication.php). Used ONLY to hold entries back from Bluesky, never to post:
# a Pickings day writes four part-entries plus the assembled page, and only the page should go out.
SYNDICATION_ACK_URL = os.environ.get("SYNDICATION_ACK_URL", "https://michaeljoelhall.com/wp-json/mjh/v1/syndicate-ack")
SYNDICATION_SECRET = os.environ.get("SYNDICATION_SECRET", "")

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
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            data = f.read()
        if not data.strip():
            raise ValueError("empty state file")
        return json.loads(data)
    except Exception as e:
        # Corrupt/empty (e.g. disk-full truncation). Sideline the bad file so the
        # next run doesn't trip on it again, then recover from the newest backup
        # rather than {} — a blank state re-sends every task (dupes).
        try:
            os.replace(STATE_FILE, f"{STATE_FILE}.corrupt-{int(time.time())}")
        except OSError:
            pass
        import glob
        baks = sorted(glob.glob(STATE_FILE + ".bak-*") + glob.glob(STATE_FILE + ".corrupt-*"),
                      key=lambda p: os.path.getmtime(p))
        for b in reversed(baks):
            try:
                with open(b) as f:
                    d = json.load(f)
                log.error(f"state.json unreadable ({e}); recovered from {b}")
                return d
            except Exception:
                continue
        log.error(f"state.json unreadable ({e}) and no valid backup; starting empty")
        return {}


def save_state(state):
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a crash/disk-full mid-write must not truncate state.json to
    # 0 bytes (that silently killed the cron for ~a day on 2026-07-14).
    tmp = STATE_FILE + ".tmp"
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
    def ical_escape(text):
        return (text.replace("\\", "\\\\").replace("\n", "\\n")
                .replace(",", "\\,").replace(";", "\\;"))

    desc_line = ""
    if description:
        desc_line = f"DESCRIPTION:{ical_escape(description)}\r\n"
    now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    vcal = (
        "BEGIN:VCALENDAR\r\n"
        "PRODID:-//Ledger Processor//EN\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VTODO\r\n"
        f"UID:{task_uid}\r\n"
        f"DTSTAMP:{now}\r\n"
        # SUMMARY was unescaped while DESCRIPTION was — a comma/semicolon in an
        # OCR'd title produced a malformed/truncated VTODO.
        f"SUMMARY:{ical_escape(title)}\r\n"
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
    all_ledger_by_id = {}  # ledgerItem id -> item (deduped across device merges)
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

        for li in data.get("ledgerItems") or []:
            lid = li.get("id")
            if lid and str(lid).lower() not in deleted_ids:
                all_ledger_by_id[lid] = li

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
    if all_ledger_by_id:
        merged["ledgerItems"] = list(all_ledger_by_id.values())
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


def texts_in_box(texts, box):
    """Pasted TextElements whose center falls in the box, joined top-to-bottom then
    left-to-right. Pasted text is already digital (no OCR needed) — this lets a Pickings
    zone's typed/pasted article text be routed like its handwriting."""
    left, top, right, bottom = box
    picked = []
    for te in texts or []:
        t = (te.get("text") or "").strip()
        if not t:
            continue
        cx = float(te.get("x", 0)) + float(te.get("width", 0)) / 2.0
        cy = float(te.get("y", 0)) + float(te.get("height", 0)) / 2.0
        if left <= cx <= right and top <= cy <= bottom:
            picked.append((cy, cx, t))
    picked.sort(key=lambda p: (p[0], p[1]))
    return "\n\n".join(p[2] for p in picked)


def ledger_image_b64(ie):
    """An image element's pixels, as base64 — from either face of the two-faced wire
    (WIRE-MEDIA-BY-REFERENCE.md, both device repos): inline in `data`, or in a media-store
    file named by `dataRef` (<sha256-of-bytes>.<png|jpg>) once the devices externalize.
    Inline wins when both appear. A missing or malformed ref returns None — the image is
    skipped and logged, never fatal, so an errored blob can't fail the day (the
    processed[name] success-path contract stays intact)."""
    b64 = ie.get("data")
    if b64:
        return b64
    ref = ie.get("dataRef") or ""
    if not ref:
        return None
    stem, _, ext = ref.partition(".")
    if len(stem) != 64 or ext not in ("png", "jpg") or any(c not in "0123456789abcdef" for c in stem):
        log.warning(f"Malformed dataRef skipped: {ref!r}")
        return None
    path = Path(LEDGER_MEDIA_DIR) / ref
    try:
        return base64.b64encode(path.read_bytes()).decode()
    except OSError as e:
        log.warning(f"Media blob missing for dataRef {ref}: {e}")
        return None


def gram_provenance(ie):
    """What a gram SAYS it is, and where it came from — (caption, source_link).

    This exists because the code here asked `ie.get("caption")` for years and always got
    nothing: `caption` is not a field on the wire. `ImageElement` carries elementId, timestamp,
    geometry, data/dataRef, page, **sourceLink, sourceLabel, sourceFeed, mediaTitle, mediaDate,
    cardText**, gramId, the A/V pointers, and the intake keys — and no caption anywhere. So every
    picking this mirrored went up mute, which is exactly what "the pickings provenance didn't
    carry over into anything meaningful" looks like from the reading end.

    The caption prefers what the creator chose (mediaTitle), then the words a text card was drawn
    from (cardText), then the human name of the origin (sourceLabel), then the publication
    (sourceFeed) — first non-empty wins, because they are increasingly generic. The link is
    sourceLink, which may be an http(s) URL (a feed article, a book's web source) or an internal
    address like `ledger://<date>/<page>` or `mail://<id>`; only the http kind is worth printing
    on the web, so the caller decides what to do with it rather than being handed a dead link.
    """
    def s(key):
        v = ie.get(key)
        return v.strip() if isinstance(v, str) else ""

    caption = s("mediaTitle") or s("cardText") or s("sourceLabel") or s("sourceFeed")
    # A card's own words can run long; a caption is a line, not the essay.
    if len(caption) > 300:
        caption = caption[:297].rstrip() + "…"
    return caption, s("sourceLink")


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
        b64 = ledger_image_b64(ie)
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


# ---------------------------------------------------------------------------
# The assembled Pickings page as ONE picture (for POSSE / Bluesky)
#
# michaeljoelhall.com builds the Pickings "look" in HTML and CSS — see
# mjh_journal_render_ledger_storycard() in mjh-social-archive.php — by laying the day's four
# SEPARATE entries (Notes, Quotes, Image 1, Image 2) two-up on a cream plate. That look exists
# only in a browser: there is no single picture of it anywhere. Bluesky can carry a picture and
# nothing else, and mjh-syndication.php's pickings branch reads _social_media[0]['url'], so
# without a render of the whole board a syndicated picking goes out as bare text and a link.
# These constants borrow the storycard's own palette and two-column shape so the thing that
# lands on Bluesky and the thing on the website read as the same object.
# ---------------------------------------------------------------------------
PICKINGS_PAGE_W = 1200               # ~2x Bluesky's in-feed display width, so it survives the downscale
PICKINGS_PAGE_PAD = 40
PICKINGS_PAGE_GAP = 24
PICKINGS_PAGE_INSET = 7              # the mat around each plate (the card's 4px border + 1px outline)
PICKINGS_PAGE_LABEL_H = 34
PICKINGS_PAGE_HEAD_H = 66
PICKINGS_PAGE_MAX_CELL_H = 700       # holds the finished page near 3:4, which Bluesky shows uncropped
PICKINGS_PAGE_TRIM_MARGIN = 18       # air left around the ink when a cell is cropped to its content
PICKINGS_PAGE_BYTE_BUDGET = 950000   # see _encode_within_budget: the PDS refuses a blob over ~1MB

# Storycard palette (mjh-social-archive.php): page #fbf7ec on #d9c9a3, plates #fff8ec on #b89a55,
# labels in the rust #8a3a2b, the header rule in the ochre #a98e3f.
_PAGE_BG, _PAGE_EDGE = (251, 247, 236), (217, 201, 163)
_PLATE_BG, _PLATE_EDGE = (255, 248, 236), (184, 154, 85)
_LABEL_INK, _HEAD_INK = (138, 58, 43), (169, 142, 63)


def _page_font(size, bold=False):
    """DejaVu at a given size, falling back to PIL's bitmap default like render_full_page does."""
    face = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/" + face, size)
    except OSError:
        return ImageFont.load_default()


def _trim_to_content(png_data, margin=PICKINGS_PAGE_TRIM_MARGIN):
    """Crop one rendered Pickings cell down to the ink and photos actually in it.

    A Notes column is a 644x1090 zone that on most days holds six lines of handwriting near the
    top. Dropping that whole mostly-empty zone into the composite would shrink the writing to
    nothing at the size Bluesky shows an image in the feed — and the render has to be readable as
    a THUMBNAIL, not only when someone taps it. Cropping to content is what buys that: a sparse
    day's few lines end up filling their plate instead of floating in white.

    Returns a PIL image, uncropped when the cell is uniform or the bounding box comes back
    degenerate (render_pickings_box already declines to emit a wholly empty cell).
    """
    img = Image.open(BytesIO(png_data)).convert("RGB")
    bbox = ImageChops.difference(img, Image.new("RGB", img.size, (255, 255, 255))).getbbox()
    if not bbox:
        return img
    left = max(0, bbox[0] - margin)
    top = max(0, bbox[1] - margin)
    right = min(img.width, bbox[2] + margin)
    bottom = min(img.height, bbox[3] + margin)
    if right - left < 8 or bottom - top < 8:
        return img
    return img.crop((left, top, right, bottom))


def _encode_within_budget(img, budget=PICKINGS_PAGE_BYTE_BUDGET):
    """Encode the composite as PNG if it fits the budget, otherwise as progressively cheaper JPEG.

    The Bluesky poster (/opt/reading-digest/bsky_poster.py) fetches the media URL and hands the
    bytes to upload_blob verbatim — it never resizes — and the PDS rejects a blob over ~1MB. A
    Pickings page carrying two photographs sails past that as PNG, and the failure surfaces only
    as an exception line in the syndication cron, so the ceiling is enforced here at render time
    where it can still be fixed. Line-art-only pages stay lossless PNG; photo-heavy ones give up
    a little fidelity rather than silently failing to post.

    Returns (bytes, "png" | "jpg").
    """
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    if buf.tell() <= budget:
        return buf.getvalue(), "png"
    for quality in (88, 80, 72, 62):
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        if buf.tell() <= budget:
            return buf.getvalue(), "jpg"
    # Still too heavy. Halve the canvas: a slightly soft page that actually posts beats a crisp
    # one the PDS refuses, and this only ever bites a board with two large photographs on it.
    small = img.resize((max(1, img.width // 2), max(1, img.height // 2)), Image.LANCZOS)
    buf = BytesIO()
    small.save(buf, format="JPEG", quality=70, optimize=True, progressive=True)
    return buf.getvalue(), "jpg"


def render_pickings_page(date_str, cells, board_name="", width=PICKINGS_PAGE_W):
    """Assemble one day's Pickings board into a single picture of the page.

    cells is the board in device order — [(label, png_or_None)] for NOTES, QUOTES, IMAGE 1,
    IMAGE 2 — and the layout keeps that order: the two text columns on top, the two image tiles
    beneath, exactly where they sit on the Boox. MJH: "the notes and quotes and image 1 and image
    2 used on michaeljoelhall.com and put together to resemble the pickings page."

    A partial board is the normal case, not an error — plenty of days are a note and one photo —
    so absent cells are dropped rather than reserved: a row left with one survivor spans the full
    width, a row left with none is omitted entirely, and an empty board renders nothing at all
    rather than an empty frame. What publishes is the page as far as it was actually filled in.

    Each plate hugs its own fitted picture instead of being a fixed rectangle, which is the other
    half of that honesty: a short cell leaves no hollow border around itself.

    Returns (bytes, "png" | "jpg", (w, h)), or None when the board carries nothing.
    """
    rows = []
    for pair in (cells[:2], cells[2:4]):
        row = [(label, _trim_to_content(png)) for label, png in pair if png]
        if row:
            rows.append(row)
    if not rows:
        return None

    inner = width - 2 * PICKINGS_PAGE_PAD
    avail_h = PICKINGS_PAGE_MAX_CELL_H - 2 * PICKINGS_PAGE_INSET
    laid = []
    for row in rows:
        cell_w = inner if len(row) == 1 else (inner - PICKINGS_PAGE_GAP) // 2
        avail_w = cell_w - 2 * PICKINGS_PAGE_INSET
        fitted = []
        for label, img in row:
            # Never magnify past what was rendered. A cell holding real work always trims to
            # something wider than its column and so scales DOWN; only a scrap ever wants scaling
            # up, and a scrap magnified to fill the plate reads as a page full of one squiggle.
            k = min(avail_w / float(img.width), avail_h / float(img.height), 1.0)
            fitted.append((label, img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))),
                                             Image.LANCZOS)))
        laid.append((cell_w, fitted, max(f[1].height for f in fitted)))

    total_h = PICKINGS_PAGE_PAD + PICKINGS_PAGE_HEAD_H
    for _cw, _fitted, plate_h in laid:
        total_h += PICKINGS_PAGE_LABEL_H + plate_h + 2 * PICKINGS_PAGE_INSET + PICKINGS_PAGE_GAP
    total_h += PICKINGS_PAGE_PAD - PICKINGS_PAGE_GAP

    page = Image.new("RGB", (width, total_h), _PAGE_BG)
    draw = ImageDraw.Draw(page)
    draw.rectangle([0, 0, width - 1, total_h - 1], outline=_PAGE_EDGE, width=2)

    # A renamed board can carry a whole headline as its name — one real board is called "Rise in
    # overseas surrogates 'increases risk of stateless babies'" — and at a fixed size that ran
    # straight off the right edge. Step the size down first, then clip the name from the end, so
    # the part that gets lost is always the tail of the title and never the date.
    label_board = (board_name or "").strip()
    head_stem = "PICKINGS · %s" % date_str
    head = head_stem + ((" · " + label_board) if label_board and label_board.lower() != "pickings" else "")
    head_max = width - 2 * PICKINGS_PAGE_PAD
    head_size = 38
    for head_size in (38, 34, 30, 26):
        head_font = _page_font(head_size, bold=True)
        if draw.textlength(head, font=head_font) <= head_max:
            break
    while len(head) > len(head_stem) + 4 and draw.textlength(head, font=head_font) > head_max:
        head = head[:-2] + "…"
    draw.text((PICKINGS_PAGE_PAD, PICKINGS_PAGE_PAD - 4 + (38 - head_size) // 2), head,
              font=head_font, fill=_HEAD_INK)
    rule_y = PICKINGS_PAGE_PAD + 50
    draw.line([(PICKINGS_PAGE_PAD, rule_y), (width - PICKINGS_PAGE_PAD, rule_y)], fill=_PAGE_EDGE, width=1)

    label_font = _page_font(23, bold=True)
    y = PICKINGS_PAGE_PAD + PICKINGS_PAGE_HEAD_H
    for cell_w, fitted, plate_h in laid:
        x = PICKINGS_PAGE_PAD
        for label, img in fitted:
            plate_w = img.width + 2 * PICKINGS_PAGE_INSET
            plate_x = x + (cell_w - plate_w) // 2  # centre each plate inside its column
            draw.text((plate_x, y), label.upper(), font=label_font, fill=_LABEL_INK)
            plate_y = y + PICKINGS_PAGE_LABEL_H
            draw.rectangle([plate_x, plate_y, plate_x + plate_w - 1,
                            plate_y + img.height + 2 * PICKINGS_PAGE_INSET - 1],
                           fill=_PLATE_BG, outline=_PLATE_EDGE, width=1)
            page.paste(img, (plate_x + PICKINGS_PAGE_INSET, plate_y + PICKINGS_PAGE_INSET))
            x += cell_w + PICKINGS_PAGE_GAP
        y += PICKINGS_PAGE_LABEL_H + plate_h + 2 * PICKINGS_PAGE_INSET + PICKINGS_PAGE_GAP

    data, ext = _encode_within_budget(page)
    return data, ext, page.size


def synd_suppress_from_queue(post_id, why):
    """Take a journal entry out of the POSSE queue without ever posting it to Bluesky.

    mjh-syndication.php queues EVERY unsyndicated social_archive entry whose platform is in the
    site's source list, and a Pickings day writes four of them (Notes, Quotes, Image 1, Image 2)
    plus the assembled page. Left alone that is five Bluesky posts for one board. Michael wants
    the page — one post of the thing he actually made — so the four parts are acked as handled
    the moment they are created, seconds after the ingest returns their post_id and well inside
    the 30-minute syndication cron's window.

    _bsky_posted is the same flag used to baseline the 86 pre-existing Pickings entries, and
    syndicate-ack only ever SETS it — nothing here can un-baseline history or re-post it.
    """
    if not (SYNDICATION_SECRET and post_id):
        return
    try:
        r = requests.post(SYNDICATION_ACK_URL, json={"post_id": int(post_id)},
                          headers={"X-MF-Secret": SYNDICATION_SECRET, "Content-Type": "application/json"},
                          timeout=30)
        log.info("Syndication: held back %s (post_id=%s): %s", why, post_id, r.status_code)
    except Exception as e:
        log.error("Syndication hold-back failed for %s (post_id=%s): %s", why, post_id, e)


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


# ---------------------------------------------------------------------------
# Named surfaces (renamed / extra Pickings boards, named Synthesize topic pages,
# named Grid / Jot documents)
#
# The device stores a surface's ink in the day JSON under a note-page KEY. The default
# board keeps the legacy key ("pickings" / "synthesize" / "grid" / "sketch"), but adding
# or renaming one mints a fresh key — PickingsStore uses "pickings-<millis>",
# SynthPageStore "synthesize-<millis>", and LedgerDocumentStore (Grid + Jot) mints
# "grid-<millis>" / "sketch-<millis>" — and records the display name in a sidecar index:
#
#   <tree>/pickings-index/<YYYY-MM-DD>.json  -> [{"key","name"}]        (per day)
#   <tree>/synth-index/pages.json            -> [{"key","name","date"}] (global)
#   <tree>/grid-index/pages.json             -> [{"key","name","date"}] (global)
#   <tree>/sketch-index/pages.json           -> [{"key","name","date"}] (global)
#
# grid-index / sketch-index carry LedgerDocumentStore's identity rule (its nameOf):
# the DAILY page shares its bare key across every day, so a daily-page title applies
# only when the entry's date matches; a minted document key is globally unique, so its
# name applies on any day the document was written on. Jot is the surface Michael names
# Jot but its keys have said "sketch" since before it had a name — the index dir and the
# keys keep the old spelling on purpose (renaming them would migrate his ink).
#
# Matching only the legacy key meant a renamed board's content rode the day JSON under a
# key nothing downstream recognised, and posted nowhere. Match the prefixes instead.
# ---------------------------------------------------------------------------

def is_pickings_key(key):
    key = key or ""
    return key == "pickings" or key.startswith("pickings-")


def is_synth_key(key):
    key = key or ""
    return key == "synthesize" or key.startswith("synthesize-")


def base_page_key(key):
    """A note-page key without its sub-page tail: 'grid#1' -> 'grid', 'grid-123#2' ->
    'grid-123'. The name indexes are keyed by the base — a document's sub-pages share
    its name (LedgerDocumentStore.isMine/nameOf strip the tail the same way)."""
    return (key or "").split("#", 1)[0]


def is_grid_key(key):
    base = base_page_key(key)
    return base == "grid" or base.startswith("grid-")


def is_sketch_key(key):
    base = base_page_key(key)
    return base == "sketch" or base.startswith("sketch-")


def document_idem_key(default_key, key, date_str):
    """Idempotency key for a Grid/Jot page note. The DAILY page's keys ('grid',
    'grid#1') are shared by every day in history, so they get date-stamped or one
    day's note would overwrite every other day's; a minted document key
    ('grid-<millis>', tails included) is already unique on its own and stable
    across days, exactly like the named Synthesize keys."""
    base, sep, tail = (key or "").partition("#")
    if base != default_key:
        return key
    return "%s-%s%s%s" % (default_key, date_str, sep, tail)


def _read_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def sidecar_tree_roots():
    """Every synced tree root that can hold the sidecar dirs (pickings-index/,
    synth-index/, grid-index/, sketch-index/, text-notes/), in merge order —
    later roots win on conflict.

    Two layouts exist. The multi-device layout is LEDGER_DATA_ROOT/<device>/<tree>/.
    The deployed single-dir layout (the crontab forces LEDGER_DATA_ROOT to a
    nonexistent path and points LEDGER_JSON_DIR straight at the dav tree) keeps the
    sidecars two levels above the json dir — .../data/ToolsForBoox/json -> .../data —
    the same derivation LEDGER_MEDIA_DIR uses. Without the derived root the name
    indexes were unreachable in deployment. A missing root is simply absent from
    the list: no sidecars means no names, never an error.
    """
    roots = []
    root = Path(LEDGER_DATA_ROOT)
    if root.exists():
        roots.extend(p for p in sorted(root.glob("*/*")) if p.is_dir())
    try:
        single = Path(WEBDAV_JSON_DIR).parent.parent
        if single.is_dir() and all(single.resolve() != r.resolve() for r in roots):
            roots.append(single)
    except OSError:
        pass
    return roots


def named_page_names(date_str):
    """note-page key -> display name, merged across every synced device tree.

    Later trees win on conflict, which matches the device-side merge (a locally renamed
    board keeps its local name). Missing/!unreadable indexes are simply skipped: a board
    with no registry entry still syncs, just under its default label.
    """
    names = {}
    for tree in sidecar_tree_roots():
        for entry in _read_json_file(str(tree / "pickings-index" / ("%s.json" % date_str)), []) or []:
            if isinstance(entry, dict) and entry.get("key"):
                names[entry["key"]] = (entry.get("name") or "").strip()
        for entry in _read_json_file(str(tree / "synth-index" / "pages.json"), []) or []:
            if isinstance(entry, dict) and entry.get("key") and str(entry.get("date") or "") == date_str:
                names[entry["key"]] = (entry.get("name") or "").strip()
        # Grid + Jot follow LedgerDocumentStore.nameOf: the bare daily key is
        # date-scoped (titling the 21st's grid must not title the 22nd's), a minted
        # "grid-<millis>" / "sketch-<millis>" key names its document on any day.
        for subdir, default_key in (("grid-index", "grid"), ("sketch-index", "sketch")):
            for entry in _read_json_file(str(tree / subdir / "pages.json"), []) or []:
                if not (isinstance(entry, dict) and entry.get("key")):
                    continue
                if entry["key"] == default_key and str(entry.get("date") or "") != date_str:
                    continue
                names[entry["key"]] = (entry.get("name") or "").strip()
    return names


def day_text_notes(date_str):
    """The day's typed Text Notes, merged across every synced tree:
    [{"id","title","body"}], blank notes dropped, sorted by id for a stable hash.

    Shape (TextNotesStore): <tree>/text-notes/notes-YYYY-MM-DD.json is a JSON array
    of {"id","title","body"} plus a last-edit millis the two platforms spell
    differently — Android writes "u", iOS writes "updatedAt" — so both are read.
    Cross-tree merge is the store's own: union of ids, newest edit wins per id.
    The edit stamp is used ONLY to pick a winner and is NOT returned, so a pure
    timestamp touch with identical text never re-triggers a day.

    Missing dirs/files mean no notes; a malformed file is logged and skipped,
    never fatal (the processed[name] success-path contract).
    """
    by_id = {}
    for tree in sidecar_tree_roots():
        path = tree / "text-notes" / ("notes-%s.json" % date_str)
        if not path.exists():
            continue
        entries = _read_json_file(str(path), None)
        if not isinstance(entries, list):
            log.warning("Text notes file unreadable, skipping: %s", path)
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            title = (entry.get("title") or "").strip()
            body = (entry.get("body") or "").strip()
            if not title and not body:
                continue
            updated = entry.get("u") or entry.get("updatedAt") or 0
            try:
                updated = int(updated)
            except (TypeError, ValueError):
                updated = 0
            cur = by_id.get(entry["id"])
            if cur is None or updated >= cur[0]:
                by_id[entry["id"]] = (updated, {"id": entry["id"], "title": title, "body": body})
    return [note for _, note in sorted(by_id.values(), key=lambda pair: pair[1]["id"])]


def post_named_note(date_str, key, display_name, surface, text, page_png=None, idem_key=None):
    """Mirror a named working surface (Synthesize topic page, Grid/Jot page, Text Note)
    to mjh.yoga /notes/.

    These are working pages, not field-journal entries, so they go to /notes/ only —
    tagged by surface and titled with the board's name so a renamed page stays findable.
    idem_key is the page key, so re-processing a day updates rather than duplicates.
    Callers whose keys don't follow the Synthesize default-key convention (Grid/Jot
    daily pages, whose bare key repeats every day) pass an explicit idem_key.
    """
    if not NOTES_INGEST_SECRET:
        log.warning("No NOTES_INGEST_SECRET set; skipping %s note for %s", surface, key)
        return
    text = (text or "").strip()
    if not text:
        return
    label = (display_name or "").strip()
    # An unnamed default page needs no trailing "· Synthesize" repeating the surface.
    label_suffix = " · %s" % label if label and label.lower() != surface.lower() else ""
    html = "<p>" + text.replace("\n", "<br>") + "</p>"
    body = {
        "title": "%s — %s%s" % (surface, date_str, label_suffix),
        "body": html,
        "tags": [surface],
        # A NAMED page's key already carries its own millis stamp, so it is unique on its
        # own and stable across days. The DEFAULT page's key is the bare surface name, which
        # would collide every day — so date-stamp that one.
        "idem_key": idem_key or (("%s-%s" % (surface.lower(), date_str)) if key == surface.lower() else key),
    }
    try:
        r = requests.post(
            NOTES_CREATE_TAGGED_URL, json=body,
            headers={"X-MJH-Ingest-Secret": NOTES_INGEST_SECRET, "Content-Type": "application/json"},
            timeout=30)
        log.info("%s[%s '%s'] -> mjh.yoga note: %s", surface, key, label or surface, r.status_code)
    except Exception as e:
        log.error("%s note post failed for %s: %s", surface, key, e)


def post_pickings(date_str, timestamp, image_elements, notes_text, quotes_text,
                  notes_png=None, quotes_png=None, board_suffix="", board_name=""):
    """Post a Pickings page to the journal (michaeljoelhall.com) + mirror to mjh.yoga /notes/.
    Notes and Quotes each become their OWN entry — the rendered handwriting image plus the
    OCR transcription as the caption — so the journal shows both in its own box."""
    if not SOCIAL_ARCHIVE_SECRET:
        log.warning("No SOCIAL_ARCHIVE_SECRET set; skipping Pickings journal post")
        return
    headers = {"X-MJH-Secret": SOCIAL_ARCHIVE_SECRET, "Content-Type": "application/json"}

    notes_text = (notes_text or "").strip()
    quotes_text = (quotes_text or "").strip()

    # Per-board identity. The DEFAULT board passes no suffix and no name, so its source_ids,
    # titles, filenames and idem_key stay byte-identical to before this change — existing
    # entries keep updating in place instead of forking into duplicates.
    sid = "pickings-%s%s" % (date_str, ("-" + board_suffix) if board_suffix else "")
    board_label = (board_name or "").strip()
    is_named = bool(board_label) and board_label.lower() != "pickings"
    title_stem = "Pickings \u00b7 %s%s" % (date_str, (" \u00b7 " + board_label) if is_named else "")
    note_title = "Pickings \u2014 %s%s" % (date_str, (" \u00b7 " + board_label) if is_named else "")
    board_tags = [board_label] if is_named else []

    if not SYNDICATION_SECRET:
        log.warning("No SYNDICATION_SECRET set; the Pickings part-entries cannot be held back from "
                    "the POSSE queue, so this board will syndicate as several Bluesky posts, not one")

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
                          "filename": f"{sid}-{kind}.png"})
        body = {
            "platform": "pickings", "type": "note",
            "source_id": f"{sid}-{kind}", "timestamp": timestamp,
            "title": f"{title_stem} — {label}",
            "caption": text, "media": media, "tags": ["pickings", kind] + board_tags,
        }
        try:
            r = requests.post(SOCIAL_ARCHIVE_URL, json=body, headers=headers, timeout=90)
            log.info(f"Pickings {kind} -> journal: {r.status_code}")
            if r.ok:
                payload = r.json()
                for m in (payload.get("media") or []):
                    if m.get("url"):
                        hand_urls[kind] = m["url"]
                synd_suppress_from_queue(payload.get("post_id"), f"pickings {kind} {sid}")
        except Exception as e:
            log.error(f"Pickings {kind} post failed: {e}")

    # Image 1 + Image 2 -> their own entries (square composites of pasted photo + ink).
    # Each tile carries the pasted quote/caption text so the picture shows the words too
    # (MJH: the pasted text should ride ON the image AND stand alone as the quote).
    image_urls = []  # (url, caption, source_link) so the mirror can caption AND link each image
    for i, ie in enumerate(image_elements, 1):
        b64 = ledger_image_b64(ie)
        if not b64:
            continue
        cap, src = gram_provenance(ie)
        body = {
            "platform": "pickings", "type": "image",
            "source_id": f"{sid}-img{i}", "timestamp": timestamp,
            "title": title_stem, "caption": cap,
            "media": [{"data_base64": b64, "kind": "image", "filename": f"{sid}-{i}.png"}],
            "tags": ["pickings"] + board_tags,
        }
        try:
            r = requests.post(SOCIAL_ARCHIVE_URL, json=body, headers=headers, timeout=90)
            log.info(f"Pickings image {i} -> journal: {r.status_code}")
            if r.ok:
                payload = r.json()
                for m in (payload.get("media") or []):
                    if m.get("url"):
                        image_urls.append((m["url"], cap, src))
                synd_suppress_from_queue(payload.get("post_id"), f"pickings image {i} {sid}")
        except Exception as e:
            log.error(f"Pickings image {i} post failed: {e}")

    # 3) The assembled page -> its OWN entry, and the only one of the day left in the POSSE queue.
    # The composite is the page, not each picking: the four entries above are the parts the site
    # arranges into a storycard, and this is one picture of that arrangement, so a Bluesky post
    # carries what Michael actually made rather than one cell of it. It is built from the very
    # same PNGs those entries carry, so the picture can never drift from the parts.
    #
    # A partial board still makes a page — a note and one photo is a normal day. A board holding
    # only stray marks does not: 2026-07-28 left a single 63x52 squiggle on the Quotes zone, and
    # composing that would publish, and syndicate, a page of one accidental pen stroke. So the
    # board has to hold a picture or something that read back as words before it is worth a page.
    legible = [t for t in (notes_text, quotes_text) if t and t.strip().lower() != "[illegible]"]
    page_cells = [("Notes", notes_png), ("Quotes", quotes_png)]
    for i, ie in enumerate(image_elements[:2], 1):
        raw = ledger_image_b64(ie)
        page_cells.append(("Image %d" % i, base64.b64decode(raw) if raw else None))
    if not (image_elements or legible):
        log.info("Pickings page %s-page: board holds no picture and nothing legible; not composing", sid)
        page = None
    else:
        page = render_pickings_page(date_str, page_cells, board_name=board_label)
    if page:
        page_bytes, page_ext, (page_w, page_h) = page
        page_sid = "%s-page" % sid
        page_hash = hashlib.sha256(page_bytes).hexdigest()
        # Idempotency has to hold at the byte level, not just at the entry level. The journal
        # dedups on source_id so the ENTRY is never doubled, but mjh_social_archive_upload_base64()
        # runs wp_upload_bits() on every ingest — re-posting an unchanged page would mint a fresh
        # attachment each pass and orphan the last, and this runs on a 15-minute cron where any
        # unrelated edit elsewhere on the day re-triggers process_day. So a day whose page render
        # is byte-identical to the one already published is simply not sent.
        if (load_state().get("pickings_page_sent") or {}).get(page_sid) == page_hash:
            log.info("Pickings page %s unchanged (%s…); not re-uploading", page_sid, page_hash[:12])
        else:
            page_body = {
                "platform": "pickings", "type": "image",
                "source_id": page_sid, "timestamp": timestamp,
                "title": title_stem,
                "caption": "\n\n".join(t for t in (notes_text, quotes_text) if t),
                "media": [{"data_base64": base64.b64encode(page_bytes).decode(), "kind": "image",
                           "filename": "%s.%s" % (page_sid, page_ext)}],
                # The "page" tag is what keeps the composite out of the site's own storycard and
                # email-digest buckets, which already show its four parts — it exists to BE the
                # picture that syndicates, not to be a fifth plate on the day's card.
                "tags": ["pickings", "page"] + board_tags,
            }
            try:
                r = requests.post(SOCIAL_ARCHIVE_URL, json=page_body, headers=headers, timeout=120)
                log.info("Pickings page -> journal: %s (%dx%d, %d bytes %s, cells=%s)",
                         r.status_code, page_w, page_h, len(page_bytes), page_ext,
                         [lbl for lbl, png in page_cells if png])
                if r.ok:
                    # Re-load and merge so the end-of-run save in main() clobbers no key written
                    # mid-run elsewhere (same pattern as tasks_sent / intake_sent).
                    latest = load_state()
                    latest.setdefault("pickings_page_sent", {})[page_sid] = page_hash
                    save_state(latest)
            except Exception as e:
                log.error("Pickings page post failed: %s", e)

    # 4) Mirror to mjh.yoga /notes/ as one Pickings note (handwriting + text + images)
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
        for u, cap, src in image_urls:
            html.append(f'<figure style="margin:0 0 1em"><img src="{u}" style="max-width:100%;border-radius:4px">')
            # The caption and the way back. A picking is a thing you took FROM somewhere, so the
            # note it becomes should say what it is and point at where it came from — a mirror
            # that shows the picture and drops the provenance turns a citation into an orphan.
            if cap or src:
                bits = []
                if cap:
                    bits.append(cap.replace("\n", "<br>"))
                if src.startswith("http"):
                    bits.append(f'<a href="{src}" rel="noopener">source &rarr;</a>')
                html.append('<figcaption style="font-size:.9em;color:#555;margin-top:.3em">'
                            + " &middot; ".join(bits) + "</figcaption>")
            html.append("</figure>")
        html.append(f'<p><a href="https://michaeljoelhall.com/journal/{date_str}/">View in the journal &rarr;</a></p>')
        note_body = {
            "title": note_title,
            "body": "".join(html),
            "tags": ["Pickings"] + board_tags,
            "idem_key": sid,
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
    # into the field ledger. Also skip pickings-page text — that's routed by zone in the
    # Pickings block below (folded into the Notes/Quotes entries AND captioned onto the
    # image tiles), so forwarding it here too would double-post it.
    text_elements = [te for te in (data.get("textElements") or []) if te.get("pageKey") not in ("intake", "pickings")]
    if text_elements:
        from datetime import datetime as _dt, timezone as _tz
        t_date = f"{year:04d}-{month:02d}-{day:02d}"
        t_ts = int(_dt(year, month, day, 12, 0, tzinfo=_tz.utc).timestamp())
        post_text_elements(t_date, t_ts, text_elements)

    # Typed Text Notes (text-notes/notes-<date>.json sidecars) — already text, no OCR.
    # Also BEFORE the stroke guard: a day can hold typed notes and no ink at all.
    # Each note is its own /notes draft via the named-note seam; idem_key is the note's
    # own id, which is minted once and never re-minted, so a re-run (or an edit — the
    # notes participate in the day's change-hash in main()) updates in place.
    # day_text_notes never raises on bad files, so a malformed note can't fail the day.
    for tn in day_text_notes(f"{year:04d}-{month:02d}-{day:02d}"):
        post_named_note(f"{year:04d}-{month:02d}-{day:02d}", tn["id"], tn["title"],
                        "Text Note", tn["body"], idem_key=tn["id"])

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
        # Display names for named surfaces, so downstream shows "Android" rather than the
        # opaque key "pickings-1784498609355". Indexes are keyed by BASE key, so a
        # sub-page ("grid#1", "grid-<millis>#2") inherits its document's name.
        _names = named_page_names(f"{year:04d}-{month:02d}-{day:02d}")
        if _names:
            _paged = {k: (_names.get(k) or _names.get(base_page_key(k))) for k in note_page_texts}
            payload["note_page_names"] = {k: v for k, v in _paged.items() if v is not None}

    # On-device structured items (dual-face task/event). Forward ONLY the user-curated
    # lasso/vision items — the ambient "auto" whole-section OCR is noisier than this
    # script's own Claude page OCR, so it must not override the rendered task/schedule text.
    ledger_items = [
        {
            "kind": li.get("kind"),
            "text": (li.get("text") or "").strip(),
            "done": bool(li.get("done")),
            "time": li.get("time"),
            "display": li.get("display"),
            "source": li.get("source"),
        }
        for li in (data.get("ledgerItems") or [])
        if str(li.get("source") or "").startswith("lasso") and (li.get("text") or "").strip()
    ]
    if ledger_items:
        payload["ledger_items"] = ledger_items

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

    # Pickings pages -> journal (michaeljoelhall.com) + mjh.yoga /notes/
    # A day can hold several boards: the default "pickings" plus any named/renamed board
    # ("pickings-<millis>"). Walk every one of them — matching only the default key is what
    # made a renamed board stop posting with no error and no log line.
    pdate = f"{year:04d}-{month:02d}-{day:02d}"
    surface_names = named_page_names(pdate)
    _all_images = data.get("imageElements") or []
    _all_texts = data.get("textElements") or []
    pickings_keys = {k for k in note_strokes_by_page if is_pickings_key(k)}
    pickings_keys |= {ie.get("page") for ie in _all_images if is_pickings_key(ie.get("page"))}
    pickings_keys |= {te.get("pageKey") for te in _all_texts if is_pickings_key(te.get("pageKey"))}
    # Default board first, then named boards oldest-first (the key carries a millis stamp).
    for pkey in sorted(pickings_keys, key=lambda k: (k != "pickings", k)):
        pickings_strokes = note_strokes_by_page.get(pkey) or []
        pickings_images = [ie for ie in _all_images if ie.get("page") == pkey]
        pickings_texts = [te for te in _all_texts if te.get("pageKey") == pkey]
        if pickings_strokes or pickings_images or pickings_texts:
            notes_zone   = strokes_in_box(pickings_strokes, PICKINGS_NOTES_BOX)
            quotes_zone  = strokes_in_box(pickings_strokes, PICKINGS_QUOTES_BOX)
            # Images placed WITHIN the notes/quotes boxes themselves (a quote can be an image
            # at the top + handwriting below — both belong to the same composite).
            notes_imgs   = images_in_box(pickings_images, PICKINGS_NOTES_BOX)
            quotes_imgs  = images_in_box(pickings_images, PICKINGS_QUOTES_BOX)
            notes_text   = ocr_note_page(render_note_page(notes_zone, scale=2)) if notes_zone else ""
            quotes_text  = ocr_note_page(render_note_page(quotes_zone, scale=2)) if quotes_zone else ""
            # Pasted (digital) text per zone — already text, so no OCR. Fold it into each zone's
            # transcription (pasted first, handwriting after) so the standalone Notes/Quotes
            # journal entries carry it. The Quotes-zone pasted text is ALSO used to caption the
            # image tiles below — MJH wants a pasted quote to ride ON the picture, not only as
            # its own quote card.
            notes_pasted  = texts_in_box(pickings_texts, PICKINGS_NOTES_BOX)
            quotes_pasted = texts_in_box(pickings_texts, PICKINGS_QUOTES_BOX)
            notes_text  = "\n\n".join(t for t in (notes_pasted, notes_text) if t)
            quotes_text = "\n\n".join(t for t in (quotes_pasted, quotes_text) if t)
            # The caption that rides on each image tile: prefer text pasted inside that tile,
            # else the page's quote (pasted preferred, else the handwritten quote transcription).
            shared_img_caption = quotes_pasted or quotes_text
            # Composite (strokes + in-box images) for each text zone.
            notes_png = (render_pickings_box(PICKINGS_NOTES_BOX, notes_zone, notes_imgs, scale=2)
                         if (notes_zone or notes_imgs) else None)
            quotes_png = (render_pickings_box(PICKINGS_QUOTES_BOX, quotes_zone, quotes_imgs, scale=2)
                          if (quotes_zone or quotes_imgs) else None)
            from datetime import datetime, timezone
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
                    b_caption = texts_in_box(pickings_texts, box) or shared_img_caption
                    box_images.append({"data": base64.b64encode(png).decode(), "caption": b_caption})
            board_suffix = "" if pkey == "pickings" else pkey[len("pickings-"):]
            board_name = surface_names.get(pkey, "")
            log.info(f"Pickings[{pkey}"
                     + (f" '{board_name}'" if board_name else "")
                     + f"] for {label}: notes_zone={len(notes_zone)} quotes_zone={len(quotes_zone)} "
                     f"tiles={len(box_images)} pasted(notes={len(notes_pasted)},quotes={len(quotes_pasted)})")
            post_pickings(pdate, pts, box_images, notes_text or "", quotes_text or "", notes_png, quotes_png,
                          board_suffix=board_suffix, board_name=board_name)

    # Synthesize pages -> mjh.yoga /notes/. Same rename problem, different surface: named
    # topic pages are keyed "synthesize-<millis>". These are working pages rather than field
    # journal entries, so they mirror to /notes/ (tagged Synthesize) and NOT to the public
    # journal. Their OCR also still rides the day's ledger note, as before.
    for skey in sorted(k for k in note_strokes_by_page if is_synth_key(k)):
        post_named_note(pdate, skey, surface_names.get(skey, ""), "Synthesize",
                        note_page_texts.get(skey) or "")

    # Grid and Jot pages -> mjh.yoga /notes/, the Synthesize seam exactly. Both are
    # sub-pageable ("grid#1", "sketch-<millis>#2"), so the name is looked up by the
    # BASE key — a document's sub-pages share its name — and the idem_key is
    # date-stamped for the daily page's keys only (see document_idem_key). Jot posts
    # under the surface Michael names ("Jot"); its keys keep their historic "sketch"
    # spelling, which is the wire format, not a label.
    for surface, default_key, matcher in (("Grid", "grid", is_grid_key),
                                          ("Jot", "sketch", is_sketch_key)):
        for gkey in sorted(k for k in note_strokes_by_page if matcher(k)):
            post_named_note(pdate, gkey, surface_names.get(base_page_key(gkey), ""), surface,
                            note_page_texts.get(gkey) or "",
                            idem_key=document_idem_key(default_key, gkey, pdate))

    # Anything else that carries ink under a key nothing above claims: log it by name so a
    # future named surface can never again go missing in silence.
    _claimed = {"gratitude", "intake", "0", "1", "2", "3"}
    for okey in sorted(note_strokes_by_page):
        if (is_pickings_key(okey) or is_synth_key(okey) or is_grid_key(okey)
                or is_sketch_key(okey) or okey in _claimed or okey.isdigit()):
            continue
        log.info(f"Note page '{okey}'"
                 + (f" ('{surface_names[okey]}')" if surface_names.get(okey) else "")
                 + f" for {label}: {len(note_strokes_by_page[okey])} strokes, "
                 f"{len(note_page_texts.get(okey) or '')} chars -> day note only")

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

        if is_recent and UB_TASKS_PASS and SERVER_TASKS_ENABLED:
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


def render_pickings_page_dry(date_str, out_dir="/tmp"):
    """Compose and render a day's Pickings page(s) WITHOUT publishing anything.

    Posting is a side effect on a live site that Michael's readers see, so the composite has to
    be inspectable before it ships: this walks the same merge -> zone -> render_pickings_box ->
    render_pickings_page path process_day uses, writes each board's page to disk, and prints the
    dimensions, the byte size and the exact payload shape that WOULD be posted. It touches
    neither michaeljoelhall.com nor the state file.

    OCR is deliberately skipped — the caption is the only thing that depends on it and it costs a
    Claude call per zone, so a dry run reports the caption as coming from OCR at post time.
    """
    by_date = find_device_day_files()
    name = next((n for n in sorted(by_date) if date_str in n), None)
    if not name:
        log.error("No day JSON found for %s", date_str)
        return
    merged = merge_day_data(sorted(by_date[name], key=lambda p: str(p)))

    all_images = merged.get("imageElements") or []
    all_texts = merged.get("textElements") or []
    note_strokes = {k: v for k, v in (merged.get("noteStrokes") or {}).items() if v}
    keys = {k for k in note_strokes if is_pickings_key(k)}
    keys |= {ie.get("page") for ie in all_images if is_pickings_key(ie.get("page"))}
    keys |= {te.get("pageKey") for te in all_texts if is_pickings_key(te.get("pageKey"))}
    if not keys:
        log.info("No Pickings board on %s", date_str)
        return
    surface_names = named_page_names(date_str)

    for pkey in sorted(keys, key=lambda k: (k != "pickings", k)):
        strokes = note_strokes.get(pkey) or []
        images = [ie for ie in all_images if ie.get("page") == pkey]
        cells = []
        for label, box in (("Notes", PICKINGS_NOTES_BOX), ("Quotes", PICKINGS_QUOTES_BOX),
                           ("Image 1", PICKINGS_IMAGE_BOX_1), ("Image 2", PICKINGS_IMAGE_BOX_2)):
            cells.append((label, render_pickings_box(box, strokes_in_box(strokes, box),
                                                     images_in_box(images, box), scale=2)))
        board_name = surface_names.get(pkey, "")
        out = render_pickings_page(date_str, cells, board_name=board_name)
        if not out:
            log.info("Pickings[%s]: nothing on the board; no page rendered", pkey)
            continue
        data, ext, (w, h) = out
        sid = "pickings-%s%s" % (date_str, "" if pkey == "pickings" else "-" + pkey[len("pickings-"):])
        path = os.path.join(out_dir, "%s-page.%s" % (sid, ext))
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        board_tags = [board_name] if board_name and board_name.lower() != "pickings" else []
        log.info("Pickings[%s%s] page: %dx%d, %d bytes (%s), cells=%s -> %s",
                 pkey, (" '%s'" % board_name) if board_name else "", w, h, len(data), ext,
                 [lbl for lbl, png in cells if png], path)
        log.info("  would POST %s  platform=pickings type=image source_id=%s-page "
                 "media[0]={data_base64:<%d bytes>, kind:image, filename:%s-page.%s} tags=%s "
                 "caption=<Notes+Quotes OCR at post time>",
                 SOCIAL_ARCHIVE_URL, sid, len(data), sid, ext, ["pickings", "page"] + board_tags)
        if not (cells[2][1] or cells[3][1]):
            log.info("  note: no image tile on this board, so at post time the page is composed "
                     "only if the Notes/Quotes OCR comes back with something legible")


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
        # Text Notes live in their own sidecar files (text-notes/notes-<date>.json), not
        # in the day JSON, so nothing above can see them change: they have to be folded
        # into the hashable input DELIBERATELY or an edited note would never re-trigger
        # the day. No other non-day-file input does this — the name indexes stay out on
        # purpose (a rename alone isn't worth a full re-OCR of the day). Added only when
        # the day has notes, so all history without them keeps its original hash.
        _day_match = re.search(r"day-(\d{4}-\d{2}-\d{2})", name)
        _tnotes = day_text_notes(_day_match.group(1)) if _day_match else []
        if _tnotes:
            hashable["textNotes"] = _tnotes
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
        # Re-derive from the (possibly .env-overridden) json dir unless set explicitly —
        # the crontab sets LEDGER_JSON_DIR inline, and media/ sits two levels up from it.
        LEDGER_MEDIA_DIR = os.environ.get(
            "LEDGER_MEDIA_DIR",
            os.path.normpath(os.path.join(WEBDAV_JSON_DIR, "..", "..", "media")),
        )
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
        SYNDICATION_ACK_URL = os.environ.get("SYNDICATION_ACK_URL", SYNDICATION_ACK_URL)
        SYNDICATION_SECRET = os.environ.get("SYNDICATION_SECRET", SYNDICATION_SECRET)

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
    elif len(sys.argv) > 2 and sys.argv[1] == "render-pickings-page":
        # Dry run: render only. Nothing is posted and no state is written.
        render_pickings_page_dry(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "/tmp")
    else:
        main()
