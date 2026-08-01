#!/usr/bin/env python3
"""Offline harness for the sidecar extensions (grid-index / sketch-index names,
Text Notes) in process-ledger.py. No network, no VPS, no state file: fabricated
index/note files in a temp tree, fed straight through the new functions.

Run: python3 test_sidecar_extensions.py
"""
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

# process-ledger.py imports requests/PIL at module level; neither is needed by the
# functions under test, so stub them out for an offline run on a machine without them.
for missing in ("requests",):
    if missing not in sys.modules:
        sys.modules[missing] = types.ModuleType(missing)
if "PIL" not in sys.modules:
    pil = types.ModuleType("PIL")
    for sub in ("Image", "ImageChops", "ImageDraw", "ImageFont"):
        setattr(pil, sub, types.ModuleType("PIL." + sub))
    sys.modules["PIL"] = pil

spec = importlib.util.spec_from_file_location(
    "process_ledger", str(Path(__file__).parent / "process-ledger.py"))
pl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pl)

DATE = "2026-07-31"
passed = 0


def ok(cond, label):
    global passed
    assert cond, label
    passed += 1
    print("  ok - %s" % label)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    tree_a = root / "boox" / "data"
    tree_b = root / "ipad" / "data"

    # --- fabricate name indexes -------------------------------------------------
    write(tree_a / "grid-index" / "pages.json", json.dumps([
        {"key": "grid", "name": "Day Grid", "date": DATE},          # daily title, this day
        {"key": "grid", "name": "Old Grid", "date": "2026-07-21"},  # daily title, other day
        {"key": "grid-111", "name": "Wiring Map", "date": "2026-07-29"},  # minted, any day
        {"bogus": True}, "not-a-dict",                               # malformed entries
    ]))
    write(tree_a / "sketch-index" / "pages.json", json.dumps([
        {"key": "sketch", "name": "Doodles", "date": "2026-07-30"},  # daily, WRONG day
        {"key": "sketch-222", "name": "Logo", "date": "2026-07-28"},
    ]))
    write(tree_a / "synth-index" / "pages.json", json.dumps([
        {"key": "synthesize-9", "name": "Spiral", "date": DATE}]))
    write(tree_a / "pickings-index" / ("%s.json" % DATE), json.dumps([
        {"key": "pickings-5", "name": "Android"}]))
    # A second tree with a malformed grid index: must be skipped, not fatal.
    write(tree_b / "grid-index" / "pages.json", "{{{ not json")

    # --- fabricate text notes ---------------------------------------------------
    write(tree_a / "text-notes" / ("notes-%s.json" % DATE), json.dumps([
        {"id": "tn-1", "title": "Punchlist", "body": "old body", "u": 100},
        {"id": "tn-2", "title": "", "body": "", "updatedAt": 5},     # blank -> dropped
        {"id": "tn-3", "title": "Solo", "body": "only here", "u": 1},
        {"no_id": 1},                                                 # malformed entry
    ]))
    write(tree_b / "text-notes" / ("notes-%s.json" % DATE), json.dumps([
        {"id": "tn-1", "title": "Punchlist", "body": "newer body", "updatedAt": 200},
    ]))

    pl.LEDGER_DATA_ROOT = str(root)
    pl.WEBDAV_JSON_DIR = "/nonexistent/none/json"

    print("named_page_names")
    names = pl.named_page_names(DATE)
    ok(names.get("grid") == "Day Grid", "daily grid title resolves for its own day")
    ok(names.get("grid-111") == "Wiring Map", "minted grid key names its document on any day")
    ok("sketch" not in names, "daily sketch title is date-scoped (wrong day excluded)")
    ok(names.get("sketch-222") == "Logo", "minted sketch key resolves")
    ok(names.get("synthesize-9") == "Spiral", "synth index still resolves")
    ok(names.get("pickings-5") == "Android", "pickings index still resolves")
    ok("bogus" not in str(names), "malformed grid-index entries skipped")

    print("key predicates + idem keys")
    ok(pl.is_grid_key("grid") and pl.is_grid_key("grid#1") and pl.is_grid_key("grid-123#2"),
       "grid keys match, sub-page tails included")
    ok(not pl.is_grid_key("gratitude") and not pl.is_grid_key("gridlock"),
       "gratitude/gridlock are not grid keys")
    ok(pl.is_sketch_key("sketch-222#3") and not pl.is_sketch_key("synthesize"),
       "sketch predicate matches its own family only")
    ok(pl.base_page_key("grid-123#2") == "grid-123", "base key strips sub-page tail")
    ok(pl.document_idem_key("grid", "grid", DATE) == "grid-" + DATE,
       "daily page idem key is date-stamped")
    ok(pl.document_idem_key("grid", "grid#1", DATE) == "grid-%s#1" % DATE,
       "daily sub-page idem key keeps its tail")
    ok(pl.document_idem_key("grid", "grid-123#2", DATE) == "grid-123#2",
       "minted key idem key is the key itself")

    print("day_text_notes")
    notes = pl.day_text_notes(DATE)
    ok([n["id"] for n in notes] == ["tn-1", "tn-3"], "blank note dropped, ids sorted")
    ok(notes[0]["body"] == "newer body", "newer updatedAt wins the cross-tree merge ('u' vs 'updatedAt')")
    ok(all(set(n) == {"id", "title", "body"} for n in notes),
       "edit stamps not returned (a timestamp touch never re-hashes)")

    print("change-hash participation (the main() seam)")
    day = {"year": 2026, "month": 7, "day": 31, "noteStrokes": {}}
    def hash_with_notes(tn):
        h = dict(day)
        if tn:
            h["textNotes"] = tn
        return json.dumps(h, sort_keys=True, default=str)
    base = hash_with_notes([])
    with_notes = hash_with_notes(pl.day_text_notes(DATE))
    ok(base != with_notes, "text notes change the hashable input")
    (tree_b / "text-notes" / ("notes-%s.json" % DATE)).write_text(json.dumps([
        {"id": "tn-1", "title": "Punchlist", "body": "edited again", "updatedAt": 300}]))
    ok(hash_with_notes(pl.day_text_notes(DATE)) != with_notes,
       "an edited note body changes the hashable input (re-run triggers)")

    print("single-dir derived root (the deployed crontab layout)")
    pl.LEDGER_DATA_ROOT = "/nonexistent-force-single-dir"
    json_dir = tree_a / "ToolsForBoox" / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    pl.WEBDAV_JSON_DIR = str(json_dir)
    names = pl.named_page_names(DATE)
    ok(names.get("grid") == "Day Grid" and names.get("pickings-5") == "Android",
       "sidecars found two levels above the json dir when LEDGER_DATA_ROOT is absent")
    ok([n["id"] for n in pl.day_text_notes(DATE)] == ["tn-1", "tn-3"],
       "text notes found via the derived root too")

    print("missing everything is silent")
    pl.LEDGER_DATA_ROOT = "/nonexistent-a"
    pl.WEBDAV_JSON_DIR = "/nonexistent-b/c/json"
    ok(pl.named_page_names(DATE) == {}, "no roots -> no names, no error")
    ok(pl.day_text_notes(DATE) == [], "no roots -> no notes, no error")

print("\nAll %d assertions passed." % passed)
