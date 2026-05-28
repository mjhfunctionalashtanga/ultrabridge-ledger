# Ultrabridge Ledger Processor

An add-on for [Ultrabridge](https://github.com/jdkruzr/ultrabridge) that turns handwritten daily planner pages from [Tools for Boox](https://github.com/mjhfunctionalashtanga/toolsboox-android) into structured digital data: OCR'd schedule entries, task lists with checkbox detection, freeform notes, and real tasks in your CalDAV task manager (Apple Reminders, tasks.org, Thunderbird, etc.).

## How it works

```
Boox tablet (stylus)
  → Tools for Boox app syncs day JSON to Ultrabridge via WebDAV
    → This processor renders strokes onto the template grid as a PNG
      → Claude Sonnet OCRs the full page (schedule + tasks + notes)
        → Unchecked tasks become CalDAV todos (with due date parsing)
        → Optionally forwards OCR'd data to a webhook
```

## What you get

- **Schedule**: transcribed time-slot entries (e.g., "9:00 — Team standup")
- **Tasks**: checkbox state + transcribed text → real CalDAV VTODOs
- **Notes**: freeform handwriting transcribed to text
- **Due dates**: write "due 6/15" or "due Jan 1 2027" in a task and it parses into a real due date
- **Descriptions**: each task includes the source planner date

## Requirements

- [Ultrabridge](https://github.com/jdkruzr/ultrabridge) instance with CalDAV enabled
- [Tools for Boox](https://github.com/mjhfunctionalashtanga/toolsboox-android) app with WebCal Backup enabled (fork of [Tools for Boox](https://github.com/AugustMJK/toolsboox-android) by Gabor Auth, GPLv3)
- Python 3.10+
- [Anthropic API key](https://console.anthropic.com/) (for Claude Sonnet OCR)

## Install

```bash
# On your Ultrabridge server
git clone https://github.com/mjhfunctionalashtanga/ultrabridge-ledger.git /opt/ultrabridge-ledger
cd /opt/ultrabridge-ledger

# Create a venv and install deps
python3 -m venv venv
venv/bin/pip install requests Pillow anthropic

# Configure
cp .env.example .env
nano .env  # fill in your settings

# Test
venv/bin/python3 process-ledger.py

# Set up cron (every 15 minutes)
(crontab -l 2>/dev/null; echo '*/15 * * * * cd /opt/ultrabridge-ledger && venv/bin/python3 process-ledger.py >> /var/log/ledger-processor.log 2>&1') | crontab -
```

Or use the deploy script from your local machine:

```bash
bash deploy.sh user@your-ultrabridge-server
```

## Configuration (.env)

```bash
# Where Tools for Boox uploads day JSON files via WebDAV
LEDGER_JSON_DIR=/path/to/ultrabridge-data/tab8/toolsboox/ToolsForBoox/json

# State file (tracks which files have been processed)
LEDGER_STATE_FILE=/opt/ultrabridge-ledger/state.json

# Anthropic API key for handwriting OCR
ANTHROPIC_API_KEY=sk-ant-...

# Ultrabridge CalDAV credentials (for task creation)
UB_TASKS_URL=https://your-ultrabridge.example.com/tasks
UB_TASKS_USER=admin
UB_TASKS_PASS=your-password

# Only create tasks for planner pages within this many days (avoids flooding old history)
TASK_CREATE_LOOKBACK_DAYS=7

# Optional: forward OCR'd data to a webhook (leave blank to skip)
LEDGER_WEBHOOK_URL=
LEDGER_WEBHOOK_SECRET=
```

### Minimal setup (tasks only, no webhook)

Just set `LEDGER_JSON_DIR`, `ANTHROPIC_API_KEY`, and the `UB_TASKS_*` credentials. Leave `LEDGER_WEBHOOK_URL` and `LEDGER_WEBHOOK_SECRET` blank.

## Due date parsing

Write a due date anywhere in a task and it gets parsed into a real CalDAV due date. The date text is stripped from the task title.

| You write | Task title | Due date |
|-----------|-----------|----------|
| `Buy milk due 6/15/2027` | Buy milk | 2027-06-15 |
| `Call dentist due: 01/01/2027` | Call dentist | 2027-01-01 |
| `File taxes due 4/15` | File taxes | next Apr 15 |
| `Review draft due Jan 1 2027` | Review draft | 2027-01-01 |
| `Fix bug due 2027-03-01` | Fix bug | 2027-03-01 |
| `No due date here` | No due date here | planner page date |

## CalDAV + Apple Reminders

Once tasks are created, they sync to any CalDAV client:

**iOS**: Settings → Apps → Calendar → Calendar Accounts → Add Account → Other → Add CalDAV Account. Enter your Ultrabridge URL, username, and password. Tasks appear in the Reminders app.

**Android**: [tasks.org](https://tasks.org) → Settings → Add Account → CalDAV.

## Tools for Boox app setup

1. Build or install the APK from [the repo](https://github.com/mjhfunctionalashtanga/toolsboox-android)
2. In the app: Settings → enable WebCal Backup
3. Enter your Ultrabridge WebDAV URL, username, and password
4. Day pages sync automatically — JSON files land in `ToolsForBoox/json/` on WebDAV

## How the OCR works

The processor renders all stylus strokes onto a replica of the planner template grid (with hour labels, checkbox squares, section dividers) as a 2x-scaled PNG. This gives Claude Sonnet the full visual context to read the handwriting accurately — it sees exactly what you see on the e-ink screen.

The grid constants match `CalendarDayPage.kt` in the app:
- Page: 1404 x 1872px
- Cell: 600w x 50h
- Schedule: left column (x: 20-620)
- Tasks: right column top, 16 rows with checkboxes (x: 670-1270)
- Notes: right column bottom (x: 670-1270)

## Credits

- [Ultrabridge](https://github.com/jdkruzr/ultrabridge) by jdkruzr — CalDAV/WebDAV bridge for Boox and Supernote
- [Tools for Boox](https://github.com/AugustMJK/toolsboox-android) by Gabor Auth — the original planner app (GPLv3)
- OCR powered by [Claude Sonnet](https://www.anthropic.com/) from Anthropic

## License

MIT — this processor is an independent add-on, not a derivative of the GPLv3 app or Ultrabridge.
