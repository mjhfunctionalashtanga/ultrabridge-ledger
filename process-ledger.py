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


def process_file(filepath):
    """Process a single day JSON file."""
    log.info(f"Processing {filepath.name}")

    with open(filepath) as f:
        data = json.load(f)

    year = data.get("year", 0)
    month = data.get("month", 0)
    day = data.get("day", 0)
    start_hour = data.get("startHour")

    if year < 2020 or not (1 <= month <= 12) or not (1 <= day <= 31):
        log.warning(f"Invalid date in {filepath.name}, skipping")
        return

    # Flatten calendarStrokes
    all_cal = []
    for style_strokes in (data.get("calendarStrokes") or {}).values():
        if isinstance(style_strokes, list):
            all_cal.extend(style_strokes)

    # Flatten noteStrokes
    all_notes = []
    for page_strokes in (data.get("noteStrokes") or {}).values():
        if isinstance(page_strokes, list):
            all_notes.extend(page_strokes)

    if not all_cal and not all_notes:
        log.info(f"No strokes in {filepath.name}, skipping")
        return

    # Render full page with template grid + all strokes
    png = render_full_page(all_cal, all_notes, start_hour, scale=2)

    # OCR the full page
    ocr = ocr_full_page(png, start_hour)

    # Build payload for mjh.yoga
    payload = {
        "year": year,
        "month": month,
        "day": day,
        "startHour": start_hour,
        "calendarStrokes": data.get("calendarStrokes", {}),
        "noteStrokes": data.get("noteStrokes", {}),
    }

    if ocr:
        # Task texts keyed by row number
        task_texts = {}
        task_checked = {}
        for t in ocr.get("tasks", []):
            if isinstance(t, dict) and t.get("text"):
                task_texts[t["row"]] = t["text"]
                task_checked[t["row"]] = t.get("checked", False)
        payload["task_texts"] = task_texts
        payload["task_checked"] = task_checked

        # Schedule texts
        payload["schedule_texts"] = ocr.get("schedule", [])

        # Notes text
        payload["notes_text"] = ocr.get("notes", "")

        # Create Ultrabridge tasks for unchecked items on recent dates
        from datetime import date, timedelta
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        try:
            page_date = date.fromisoformat(date_str)
            is_recent = (date.today() - page_date).days <= TASK_CREATE_LOOKBACK_DAYS
        except ValueError:
            is_recent = False

        if is_recent and UB_TASKS_PASS:
            import re
            date_label = page_date.strftime('%B %d, %Y')
            for row_num, text in task_texts.items():
                if not task_checked.get(row_num, False) and text:
                    task_title, task_due = parse_due_date(text, date_str)
                    desc = f"From Boox planner {date_label}"
                    create_ultrabridge_task(task_title, task_due, desc)

    forward_to_webhook(payload)


def main():
    json_dir = Path(WEBDAV_JSON_DIR)
    if not json_dir.exists():
        log.info(f"JSON dir {json_dir} does not exist yet, nothing to process")
        return

    state = load_state()
    processed = state.get("processed", {})
    changed = False

    day_files = sorted(json_dir.glob("day-*-v2.json")) + sorted(json_dir.glob("day-*.json"))
    seen_bases = set()
    unique_files = []
    for f in day_files:
        base = f.name.replace("-v2.json", ".json")
        if base not in seen_bases:
            seen_bases.add(base)
            unique_files.append(f)

    for filepath in unique_files:
        fhash = file_hash(filepath)
        if processed.get(filepath.name) == fhash:
            continue

        try:
            process_file(filepath)
            processed[filepath.name] = fhash
            changed = True
        except Exception as e:
            log.error(f"Error processing {filepath.name}: {e}")

    if changed:
        state["processed"] = processed
        state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_state(state)

    log.info(f"Done. {len(unique_files)} files found, {sum(1 for f in unique_files if processed.get(f.name) == file_hash(f))} up to date.")


if __name__ == "__main__":
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        WEBDAV_JSON_DIR = os.environ.get("LEDGER_JSON_DIR", WEBDAV_JSON_DIR)
        STATE_FILE = os.environ.get("LEDGER_STATE_FILE", STATE_FILE)
        WEBHOOK_URL = os.environ.get("LEDGER_WEBHOOK_URL", WEBHOOK_URL)
        WEBHOOK_SECRET = os.environ.get("LEDGER_WEBHOOK_SECRET", WEBHOOK_SECRET)
        ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)
        UB_TASKS_URL = os.environ.get("UB_TASKS_URL", UB_TASKS_URL)
        UB_TASKS_USER = os.environ.get("UB_TASKS_USER", UB_TASKS_USER)
        UB_TASKS_PASS = os.environ.get("UB_TASKS_PASS", UB_TASKS_PASS)
        TASK_CREATE_LOOKBACK_DAYS = int(os.environ.get("TASK_CREATE_LOOKBACK_DAYS", str(TASK_CREATE_LOOKBACK_DAYS)))

    main()
