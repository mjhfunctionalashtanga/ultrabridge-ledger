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
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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
    pad = 40
    x_min = max(DOODLE_BOX[0], int(min(all_x) - pad))
    y_min = max(DOODLE_BOX[1], int(min(all_y) - pad))
    x_max = min(DOODLE_BOX[2], int(max(all_x) + pad))
    y_max = min(DOODLE_BOX[3], int(max(all_y) + pad))

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
        return None


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
        log.error(f"OCR returned invalid JSON: {e}")
        log.error(f"Raw response: {text[:500]}")
        return None
    except Exception as e:
        log.error(f"OCR API call failed: {e}")
        return None


def parse_due_date(text, fallback_date):
    """Extract a due date from task text. Returns (cleaned_title, date_str).

    Matches patterns like:
      due 1/1/2027, due: 01/01/2027, due 2027-01-01,
      due jan 1, due january 1 2027, due 1/1
    Strips the matched portion from the title.
    Falls back to the planner page date if no due date found.
    """
    import re
    from datetime import date

    # due MM/DD/YYYY or M/D/YYYY or MM/DD/YY
    m = re.search(r'\bdue[:\s]+(\d{1,2})/(\d{1,2})/(\d{2,4})\b', text, re.IGNORECASE)
    if m:
        month, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        title = (text[:m.start()] + text[m.end():]).strip(' ,;-')
        return title, f"{yr:04d}-{month:02d}-{day:02d}"

    # due YYYY-MM-DD
    m = re.search(r'\bdue[:\s]+(\d{4})-(\d{1,2})-(\d{1,2})\b', text, re.IGNORECASE)
    if m:
        title = (text[:m.start()] + text[m.end():]).strip(' ,;-')
        return title, f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # due MM/DD (no year — assume current year or next year if date has passed)
    m = re.search(r'\bdue[:\s]+(\d{1,2})/(\d{1,2})\b', text, re.IGNORECASE)
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
        r'\bdue[:\s]+(' + '|'.join(months.keys()) + r')\s+(\d{1,2})(?:\s+(\d{4}))?\b',
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
    all_text_elements = []  # de-dup not attempted here; we union and rely on writer behaviour
    has_lanes = False
    latest_updated = ""

    for fp in file_paths:
        try:
            with open(fp) as f:
                data = json.load(f)
        except Exception as e:
            log.warning(f"Could not read {fp}: {e}")
            continue

        for k in ("year", "month", "day", "startHour", "locale"):
            if k in data and k not in merged:
                merged[k] = data[k]

        has_lanes = has_lanes or bool(data.get("hasLanes"))

        for style, strokes in (data.get("calendarStrokes") or {}).items():
            bucket = all_cal_by_style.setdefault(style, {})
            for s in strokes or []:
                sid = s.get("strokeId")
                if sid:
                    bucket[sid] = s

        for page_key, strokes in (data.get("noteStrokes") or {}).items():
            bucket = all_notes_by_page.setdefault(page_key, {})
            for s in strokes or []:
                sid = s.get("strokeId")
                if sid:
                    bucket[sid] = s

        for te in data.get("textElements") or []:
            all_text_elements.append(te)

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
    merged["textElements"] = all_text_elements
    if latest_updated:
        merged["updated"] = latest_updated

    return merged


def task_key(date_str, row, title):
    """Stable identifier for an Ultrabridge task creation."""
    return f"{date_str}|{row}|{(title or '').strip().lower()}"


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

    # Doodle from the gratitude page → separate Sketch note
    gratitude_strokes = note_strokes_by_page.get("gratitude") or []
    doodle_strokes = extract_doodle_strokes(gratitude_strokes)
    if doodle_strokes:
        doodle_png = render_doodle_png(doodle_strokes, scale=2)
        if doodle_png:
            payload["sketch_png_b64"] = base64.b64encode(doodle_png).decode()
            log.info(f"Extracted doodle for {label}: {len(doodle_strokes)} strokes, {len(doodle_png)} bytes")

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

    forward_to_webhook(payload)


def find_device_day_files():
    """Walk LEDGER_DATA_ROOT for all per-device day JSONs.

    Returns a dict: { base_filename (e.g. 'day-2026-05-27-v2.json'): [Path, Path, ...] }

    Falls back to scanning only WEBDAV_JSON_DIR if LEDGER_DATA_ROOT doesn't exist
    or contains no device subfolders.
    """
    root = Path(LEDGER_DATA_ROOT)
    by_date = {}

    if root.exists():
        # Walk every immediate subdir as a "device" folder
        for device_dir in root.iterdir():
            if not device_dir.is_dir():
                continue
            json_dir = device_dir / "toolsboox" / "ToolsForBoox" / "json"
            if not json_dir.is_dir():
                continue
            for f in list(json_dir.glob("day-*-v2.json")) + list(json_dir.glob("day-*.json")):
                by_date.setdefault(f.name, []).append(f)

    if not by_date:
        # Fall back to single-dir scan
        single = Path(WEBDAV_JSON_DIR)
        if single.exists():
            for f in list(single.glob("day-*-v2.json")) + list(single.glob("day-*.json")):
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
        merged_bytes = json.dumps(merged, sort_keys=True, default=str).encode()
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
        state["processed"] = processed
        state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_state(state)

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

    main()
