#!/usr/bin/env python3
"""
recallimport — bulk .md importer for the recall knowledge base

Usage:
  recallimport.py <folder> [options]
  recallimport.py --dedupe [--dry-run]
  recallimport.py --clear-cache

Options:
  --dry-run            Preview what would happen, write nothing
  --skip-existing      Skip entries whose title already exists in DB
  --update-existing    Overwrite entries if title matches but content changed
  --fix-conflicts      Show side-by-side diff for every conflicting title
  --dedupe             Remove duplicate entries from DB (keeps newest)
  --clear-cache        Delete all source_path records from DB (resets import provenance)
  --category CMD|NOTE|TOOL   Force a category for all imported entries
  --file FILE          Import only this specific filename from the folder
"""

import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".local" / "share" / "recall" / "recall.db"

CMD_SIGNALS = re.compile(
    r'`{1,3}[^`]|\$ |sudo |chmod |chown |systemctl |grep |awk |sed |'
    r'curl |wget |ssh |scp |nmap |gobuster |ffuf |hydra |sqlmap |'
    r'msfconsole|hashcat|john |netcat|nc |python3? |pip |apt |cargo |make |gcc ',
    re.IGNORECASE,
)
TOOL_SIGNALS = re.compile(
    r'\bnmap\b|\bffuf\b|\bgobuster\b|\bburpsuite\b|\bmetasploit\b|\bhydra\b|'
    r'\bsqlmap\b|\bwireshark\b|\bhashcat\b|\bjohn\b|\blinpeas\b|\bwinpeas\b|'
    r'\bchisel\b|\bligolo\b|\bimpacket\b|\bbloodhound\b|\bcme\b|'
    r'\bnethunter\b|\bbettercap\b|\baircrack\b',
    re.IGNORECASE,
)


# ── DB helpers ─────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            content     TEXT NOT NULL DEFAULT '',
            category    TEXT NOT NULL DEFAULT 'note',
            tags        TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)").fetchall()}
    if "source_path" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN source_path TEXT NOT NULL DEFAULT ''")
    conn.commit()
    return conn


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_db_index(conn: sqlite3.Connection) -> dict[str, dict]:
    """Return {lower_title: {id, content, updated_at, source_path}} for fast lookup."""
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT id, title, content, updated_at, source_path FROM entries").fetchall():
        key = r["title"].lower().strip()
        if key not in out:
            out[key] = {
                "id": r["id"],
                "title": r["title"],
                "content": r["content"],
                "updated_at": r["updated_at"],
                "source_path": r["source_path"],
            }
    return out


# ── Deduplication ──────────────────────────────────────────────────────────────

def cmd_dedupe(conn: sqlite3.Connection, dry_run: bool) -> None:
    rows = conn.execute(
        "SELECT id, title, updated_at FROM entries ORDER BY LOWER(title), updated_at DESC"
    ).fetchall()
    seen: dict[str, int] = {}
    to_delete: list[int] = []
    for r in rows:
        key = r["title"].lower().strip()
        if key in seen:
            to_delete.append(r["id"])
        else:
            seen[key] = r["id"]

    if not to_delete:
        print("  No duplicates found.")
        return

    print(f"  Found {len(to_delete)} duplicate(s) across {len(seen)} unique titles.")
    for tid in to_delete:
        row = next(r for r in rows if r["id"] == tid)
        action = "WOULD DELETE" if dry_run else "DELETED"
        print(f"    {action} [{tid}] {row['title']}  (updated: {row['updated_at']})")

    if not dry_run:
        conn.executemany("DELETE FROM entries WHERE id=?", [(i,) for i in to_delete])
        conn.commit()
        print(f"  Removed {len(to_delete)} duplicate(s). Kept the most recently updated version.")
    else:
        print("  Dry run — nothing changed. Re-run without --dry-run to delete.")


# ── Clear cache ────────────────────────────────────────────────────────────────

def cmd_clear_cache(conn: sqlite3.Connection, dry_run: bool) -> None:
    count = conn.execute("SELECT COUNT(*) FROM entries WHERE source_path != ''").fetchone()[0]
    print(f"  {count} entries have a source_path set.")
    if dry_run:
        print("  Dry run — would clear source_path on all entries.")
        return
    conn.execute("UPDATE entries SET source_path=''")
    conn.commit()
    print(f"  Cleared source_path on {count} entries.")


# ── Fix conflicts ──────────────────────────────────────────────────────────────

def cmd_fix_conflicts(conn: sqlite3.Connection, entries: list[dict]) -> None:
    index = load_db_index(conn)
    conflicts = []
    for e in entries:
        key = e["title"].lower().strip()
        if key in index:
            db = index[key]
            if content_hash(e["content"]) != content_hash(db["content"]):
                conflicts.append((e, db))

    if not conflicts:
        print("  No conflicts — all titles match their DB content.")
        return

    print(f"  {len(conflicts)} conflict(s) found:\n")
    for e, db in conflicts:
        print(f"  ┌── Title: {e['title']}")
        print(f"  │   DB  [{db['updated_at']}] ({len(db['content'])} chars):")
        for line in db["content"].splitlines()[:5]:
            print(f"  │     {line}")
        print(f"  │   FILE ({len(e['content'])} chars)  src: {e.get('source_path', '?')}:")
        for line in e["content"].splitlines()[:5]:
            print(f"  │     {line}")
        print(f"  └──")
        print()


# ── Parser ─────────────────────────────────────────────────────────────────────

def parse_file(path: Path) -> list[dict]:
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    in_fence = False
    is_heading: list[bool] = []
    for line in raw_lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
        is_heading.append(not in_fence and line.startswith("# ") and not line.startswith("## "))

    entries: list[dict] = []
    cur_title = ""
    cur_body: list[str] = []

    def _flush(title: str, body: list[str]) -> None:
        if not title:
            return
        tags: list[str] = []
        kept: list[str] = []
        for ln in body:
            m = re.match(r'(?i)^\s*tags?\s*:\s*(.+)$', ln)
            if m:
                tags = [t.strip() for t in re.split(r'[,;|]', m.group(1)) if t.strip()]
            else:
                kept.append(ln)
        while kept and not kept[0].strip():
            kept.pop(0)
        while kept and not kept[-1].strip():
            kept.pop()
        entries.append({
            "title": title,
            "content": "\n".join(kept),
            "tags": tags,
            "file": path.name,
            "source_path": str(path.resolve()),
        })

    for i, line in enumerate(raw_lines):
        if is_heading[i]:
            _flush(cur_title, cur_body)
            cur_title = line[2:].strip()
            cur_body = []
        else:
            cur_body.append(line)
    _flush(cur_title, cur_body)
    return entries


# ── Category detection ─────────────────────────────────────────────────────────

def infer_category(entry: dict, forced: str | None) -> str:
    if forced:
        return forced
    fname = entry["file"].lower()
    if any(k in fname for k in ["tool", "tools"]):
        return "tool"
    if any(k in fname for k in ["cmd", "command", "commands", "cheatsheet"]):
        return "command"
    if any(k in fname for k in ["note", "notes", "theory", "concept"]):
        return "note"
    text = f"{entry['title']} {entry['content']}"
    if TOOL_SIGNALS.search(text):
        return "tool"
    if CMD_SIGNALS.search(text):
        return "command"
    return "note"


# ── Import ─────────────────────────────────────────────────────────────────────

def cmd_import(
    conn: sqlite3.Connection,
    entries: list[dict],
    *,
    dry_run: bool,
    skip_existing: bool,
    update_existing: bool,
    fix_conflicts: bool,
) -> None:
    if fix_conflicts:
        cmd_fix_conflicts(conn, entries)
        return

    if dry_run:
        print(f"\n── DRY RUN ── (nothing written)\n")
        for e in entries:
            cat = e["category"].upper()[:4].ljust(4)
            lines = e["content"].splitlines()
            snippet = next((l.strip() for l in lines if l.strip()), "(empty)")[:72]
            print(f"  [{cat}]  {e['title']}")
            print(f"         tags  : {', '.join(e['tags']) or '(none)'}")
            print(f"         lines : {len(lines)}")
            print(f"         peek  : {snippet}")
        print(f"\nWould import up to {len(entries)} entries into {DB_PATH}")
        return

    index = load_db_index(conn)
    imported = updated = skipped = 0
    cats: dict[str, int] = {"command": 0, "note": 0, "tool": 0}

    for e in entries:
        key = e["title"].lower().strip()
        cat = e["category"]
        now = _now()

        if key in index:
            db_entry = index[key]
            same_content = content_hash(e["content"]) == content_hash(db_entry["content"])

            if skip_existing:
                print(f"  SKIP   {e['title']}")
                skipped += 1
                continue

            if update_existing and not same_content:
                conn.execute(
                    "UPDATE entries SET title=?,content=?,category=?,tags=?,source_path=?,updated_at=? WHERE id=?",
                    (e["title"], e["content"], cat,
                     ",".join(e["tags"]), e["source_path"], now, db_entry["id"]),
                )
                print(f"  UPDATE [{cat.upper()[:4]}]  {e['title']}")
                updated += 1
                cats[cat] = cats.get(cat, 0) + 1
                continue

            if same_content:
                print(f"  SAME   {e['title']}")
                skipped += 1
                continue

            # Content differs but no --update flag — skip with warning
            print(f"  CONFLICT (use --update-existing or --fix-conflicts)  {e['title']}")
            skipped += 1
            continue

        conn.execute(
            "INSERT INTO entries (title,content,category,tags,source_path,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (e["title"], e["content"], cat, ",".join(e["tags"]), e["source_path"], now, now),
        )
        print(f"  +  [{cat.upper()[:4]}]  {e['title']}")
        imported += 1
        cats[cat] = cats.get(cat, 0) + 1

    conn.commit()
    print(f"""
─────────────────────────────────────
 Done.
 Imported  : {imported}
 Updated   : {updated}
 Skipped   : {skipped}
 CMD       : {cats.get('command', 0)}
 NOTE      : {cats.get('note', 0)}
 TOOL      : {cats.get('tool', 0)}
 Database  : {DB_PATH}
─────────────────────────────────────""")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="recallimport",
        description="Bulk-import .md files into the recall knowledge base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("folder", nargs="?", help="Folder containing .md files")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing")
    ap.add_argument("--skip-existing", action="store_true", help="Skip titles already in DB")
    ap.add_argument("--update-existing", action="store_true", help="Overwrite entries if content changed")
    ap.add_argument("--fix-conflicts", action="store_true", help="Show diff for every conflicting title")
    ap.add_argument("--dedupe", action="store_true", help="Remove duplicate titles from DB")
    ap.add_argument("--clear-cache", action="store_true", help="Clear all source_path fields in DB")
    ap.add_argument("--category", choices=["command", "note", "tool"], default=None)
    ap.add_argument("--file", default=None, help="Import only this filename")
    args = ap.parse_args()

    conn = get_db()

    if args.dedupe:
        print(f"\n◈  recallimport — deduplication\n")
        cmd_dedupe(conn, dry_run=args.dry_run)
        conn.close()
        return

    if args.clear_cache:
        print(f"\n◈  recallimport — clear cache\n")
        cmd_clear_cache(conn, dry_run=args.dry_run)
        conn.close()
        return

    if not args.folder:
        ap.print_help()
        sys.exit(1)

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        print(f"ERROR: not a directory: {folder}")
        sys.exit(1)

    if args.file:
        md_files = [folder / args.file]
    else:
        md_files = sorted(folder.glob("*.md"))
    md_files = [f for f in md_files if f.exists()]

    if not md_files:
        print(f"No .md files found in {folder}")
        sys.exit(0)

    print(f"\n◈  recallimport  →  {DB_PATH}")
    print(f"  Scanning {len(md_files)} file(s) in {folder}\n")

    all_entries: list[dict] = []
    for f in md_files:
        parsed = parse_file(f)
        for e in parsed:
            e["category"] = infer_category(e, args.category)
        all_entries.extend(parsed)
        print(f"  [{len(parsed):>4}]  {f.name}")

    print(f"\n  Total parsed: {len(all_entries)} entries")
    if not all_entries:
        print("  Nothing to import.")
        conn.close()
        return

    print()
    cmd_import(
        conn,
        all_entries,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
        update_existing=args.update_existing,
        fix_conflicts=args.fix_conflicts,
    )
    conn.close()


if __name__ == "__main__":
    main()
