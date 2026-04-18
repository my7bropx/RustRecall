#!/usr/bin/env python3
"""
recall-import v2.1 — bulk import .md files with duplicate prevention & DB cleanup
✨ New features:
- --dedupe / --dedupe-dry-run: Safely remove duplicates from existing DB
- --strict: Avoid importing near-duplicates (checks title + content hash)
- --update-existing: Overwrite entries if title matches but content differs
- Fixes source_path inconsistency & improves transaction safety
"""
import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
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

# ── Database ──────────────────────────────────────────────────────────────────
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
    # Ensure source_path exists for older DBs
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)").fetchall()}
    if "source_path" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN source_path TEXT NOT NULL DEFAULT ''")
    conn.commit()
    return conn

def existing_titles(conn: sqlite3.Connection) -> dict[str, dict]:
    """Return {lower_title: {id, content, updated_at}} for fast lookups."""
    out = {}
    for r in conn.execute("SELECT id, title, content, updated_at FROM entries").fetchall():
        key = r["title"].lower().strip()
        if key not in out:
            out[key] = {"id": r["id"], "content": r["content"], "updated_at": r["updated_at"]}
    return out

def insert_entry(conn: sqlite3.Connection, title: str, content: str,
                 category: str, tags: list[str], source_path: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        """INSERT INTO entries 
           (title, content, category, tags, source_path, created_at, updated_at) 
           VALUES (?,?,?,?,?,?,?)""",
        (title, content, category,
         ",".join(t.strip() for t in tags if t.strip()),
         source_path, now, now),
    )

def update_entry(conn: sqlite3.Connection, id: int, title: str, content: str,
                 category: str, tags: list[str], source_path: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        """UPDATE entries SET title=?, content=?, category=?, tags=?, 
           source_path=?, updated_at=? WHERE id=?""",
        (title, content, category,
         ",".join(t.strip() for t in tags if t.strip()),
         source_path, now, id),
    )

# ── Deduplication ─────────────────────────────────────────────────────────────
def dedupe_db(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """Remove duplicate entries by title (case-insensitive). Keeps the newest."""
    rows = conn.execute("""
        SELECT id, title, updated_at, content
        FROM entries
        ORDER BY LOWER(title), updated_at DESC
    """).fetchall()

    seen: dict[str, dict] = {}
    to_delete: list[int] = []

    for r in rows:
        key = r["title"].lower().strip()
        if key in seen:
            to_delete.append(r["id"])
        else:
            seen[key] = {"id": r["id"], "title": r["title"], "updated": r["updated_at"]}

    if not to_delete:
        print("  ✅ No duplicates found in the database.")
        return 0

    print(f"  🔍 Found {len(to_delete)} duplicate(s) across {len(seen)} unique titles.")
    if dry_run:
        for tid in to_delete:
            row = next(r for r in rows if r["id"] == tid)
            print(f"  🗑️  WOULD DELETE: [{row['id']}] {row['title']} (updated: {row['updated_at']})")
        print("  💡 Run without --dedupe-dry-run to actually delete them.")
        return len(to_delete)

    conn.executemany("DELETE FROM entries WHERE id = ?", [(tid,) for tid in to_delete])
    conn.commit()
    print(f"  🧹 Deleted {len(to_delete)} duplicate entry(ies). Kept the most recently updated version.")
    return len(to_delete)

def content_hash(text: str) -> str:
    """Generate a short stable hash for content comparison."""
    return hashlib.sha256(text.strip().encode()).hexdigest()[:12]

# ── Parser ────────────────────────────────────────────────────────────────────
def parse_file(path: Path) -> list[dict]:
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    in_fence = False
    is_heading = []
    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        heading = (not in_fence and line.startswith("# ") and not line.startswith("## "))
        is_heading.append(heading)

    entries: list[dict] = []
    current_title: str = ""
    current_body: list[str] = []

    def _flush(title: str, body: list[str]):
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
        while kept and not kept[0].strip(): kept.pop(0)
        while kept and not kept[-1].strip(): kept.pop()
        entries.append({
            "title": title,
            "content": "\n".join(kept),
            "tags": tags,
            "file": path.name,
            "source_path": str(path.resolve()),
        })

    for i, line in enumerate(raw_lines):
        if is_heading[i]:
            _flush(current_title, current_body)
            current_title = line[2:].strip()
            current_body = []
        else:
            current_body.append(line)
    _flush(current_title, current_body)
    return entries

# ── Category detection ────────────────────────────────────────────────────────
def infer_category(entry: dict, forced: str | None) -> str:
    if forced: return forced
    fname = entry["file"].lower()
    if any(k in fname for k in ["tool", "tools"]): return "tool"
    if any(k in fname for k in ["cmd", "command", "commands", "cheatsheet", "oneliner"]): return "command"
    if any(k in fname for k in ["note", "notes", "theory", "concept"]): return "note"
    text = f"{entry['title']} {entry['content']}"
    if TOOL_SIGNALS.search(text): return "tool"
    if CMD_SIGNALS.search(text): return "command"
    return "note"

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        prog="recall-import",
        description="Bulk-import .md files into recall with duplicate prevention",
    )
    ap.add_argument("folder", help="Folder containing your .md files")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing")
    ap.add_argument("--skip-existing", action="store_true", help="Skip entries with matching titles")
    ap.add_argument("--strict", action="store_true", help="Skip if title + content hash already exist")
    ap.add_argument("--update-existing", action="store_true", help="Overwrite existing entries if content differs")
    ap.add_argument("--dedupe", action="store_true", help="Remove duplicate entries from DB (keeps newest)")
    ap.add_argument("--dedupe-dry-run", action="store_true", help="Preview duplicates without deleting")
    ap.add_argument("--category", choices=["command", "note", "tool"], default=None, help="Force category")
    ap.add_argument("--file", default=None, help="Import only this specific filename")
    args = ap.parse_args()

    # ── Standalone dedupe mode ────────────────────────────────────────────
    if args.dedupe or args.dedupe_dry_run:
        print(f"\n◈  recall-import  (DB deduplication)")
        conn = get_db()
        dedupe_db(conn, dry_run=args.dedupe_dry_run)
        conn.close()
        if not (args.dedupe_dry_run and args.folder):
            return
        print()

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        print(f"ERROR: {folder} is not a directory")
        sys.exit(1)

    md_files = [folder / args.file] if args.file else sorted(folder.glob("*.md"))
    md_files = [f for f in md_files if f.exists()]
    if not md_files:
        print(f"No .md files found in {folder}")
        sys.exit(0)

    print(f"\n◈  recall-import  (code-fence-aware)")
    print(f"  Scanning {len(md_files)} file(s) in {folder}\n")

    all_entries: list[dict] = []
    for f in md_files:
        entries = parse_file(f)
        for e in entries:
            e["category"] = infer_category(e, args.category)
            all_entries.append(e)
        status = f"[{len(entries):>4} entries]"
        print(f"  {status}  {f.name}")

    total = len(all_entries)
    print(f"\nTotal parsed: {total} entries")
    if total == 0:
        print("  Nothing to import.")
        sys.exit(0)

    # ── Dry run ───────────────────────────────────────────────────────────
    if args.dry_run:
        print("\n── DRY RUN ── (nothing written)\n")
        for e in all_entries:
            tags = ", ".join(e["tags"]) or "(no tags)"
            cat = e["category"].upper()[:4].ljust(4)
            lines = e["content"].splitlines()
            snippet = next((l.strip() for l in lines if l.strip()), "(empty)")[:70]
            print(f"  [{cat}]  {e['title']}")
            print(f"         tags    : {tags}")
            print(f"         lines   : {len(lines)}")
            print(f"         preview : {snippet}")
            print()
        print(f"  Would import {total} entries into {DB_PATH}")
        return

    # ── Import ────────────────────────────────────────────────────────────
    conn = get_db()
    existing = existing_titles(conn)
    imported = skipped = updated = 0
    cats: dict[str, int] = {"command": 0, "note": 0, "tool": 0}
    print()

    for e in all_entries:
        key = e["title"].lower().strip()
        exists = key in existing

        # Strict mode: skip if title + content match
        if args.strict and exists:
            db_entry = existing[key]
            if content_hash(e["content"]) == content_hash(db_entry["content"]):
                print(f"  SKIP  {e['title']} (exact match)")
                skipped += 1
                continue

        # Update mode: overwrite if content differs
        if args.update_existing and exists:
            db_entry = existing[key]
            if content_hash(e["content"]) != content_hash(db_entry["content"]):
                update_entry(conn, db_entry["id"], e["title"], e["content"],
                             e["category"], e["tags"], e["source_path"])
                updated += 1
                print(f"  ⬆️  UPDATE {e['title']}")
                cats[e["category"]] += 1
                continue

        # Skip existing mode
        if args.skip_existing and exists:
            print(f"  SKIP  {e['title']}")
            skipped += 1
            continue

        insert_entry(conn, e["title"], e["content"], e["category"], e["tags"], e["source_path"])
        imported += 1
        cats[e["category"]] += 1
        print(f"  + [{e['category'].upper()[:4]}]  {e['title']}")

    conn.commit()
    conn.close()

    print(f"""
─────────────────────────────────────────
Done.
Imported : {imported}
Updated  : {updated}
Skipped  : {skipped}
CMD      : {cats['command']}
NOTE     : {cats['note']}
TOOL     : {cats['tool']}
Database : {DB_PATH}
─────────────────────────────────────────
""")

if __name__ == "__main__":
    main()
