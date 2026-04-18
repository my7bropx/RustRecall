#!/usr/bin/env python3
"""recall — redesigned vim-like terminal knowledge base with stable Textual UI."""

import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


from rich.markup import escape as esc
from rich.console import Group
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, ScrollableContainer
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TextArea

# ── Nightfox-like palette ─────────────────────────────────────────────────────

PX = {
    "bg": "#050816",
    "bg1": "#0b1220",
    "bg2": "#111a2e",
    "bg3": "#1b2a4a",
    "bg4": "#29406e",
    "fg": "#ecf3ff",
    "fg2": "#d6e4ff",
    "muted": "#8aa4d6",
    "red": "#ff6b81",
    "green": "#7dffb3",
    "yellow": "#ffd166",
    "blue": "#66b3ff",
    "cyan": "#7ce7ff",
    "purple": "#b794ff",
    "orange": "#ff9f68",
}

ALL_CATS = ["command", "note", "tool"]
CAT_LABEL = {"command": "CMD", "note": "NOTE", "tool": "TOOL"}
CAT_COLOR = {"command": PX["cyan"], "note": PX["yellow"], "tool": PX["orange"]}


class Mode(Enum):
    NORMAL = "NORMAL"
    SEARCH = "SEARCH"
    COMMAND = "COMMAND"


DB_PATH = Path.home() / ".local" / "share" / "recall" / "recall.db"


class DB:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                content     TEXT NOT NULL DEFAULT '',
                category    TEXT NOT NULL DEFAULT 'note',
                tags        TEXT NOT NULL DEFAULT '',
                source_path TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            """
        )
        self.conn.commit()
        cols = {row['name'] for row in self.conn.execute("PRAGMA table_info(entries)").fetchall()}
        if 'source_path' not in cols:
            self.conn.execute("ALTER TABLE entries ADD COLUMN source_path TEXT NOT NULL DEFAULT ''")
            self.conn.commit()

    def _row(self, r) -> dict:
        d = dict(r)
        d["tags"] = [t.strip() for t in d["tags"].split(",") if t.strip()] if d["tags"] else []
        d["source_path"] = (d.get("source_path") or "").strip()
        return d

    def all(self) -> list[dict]:
        return [self._row(r) for r in self.conn.execute("SELECT * FROM entries ORDER BY updated_at DESC").fetchall()]

    def add(self, title, content, category, tags, source_path: str = "") -> int:
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO entries (title,content,category,tags,source_path,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (title, content, category, _tags_str(tags), source_path.strip(), now, now),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore

    def update(self, id, title, content, category, tags, source_path: str = ""):
        self.conn.execute(
            "UPDATE entries SET title=?,content=?,category=?,tags=?,source_path=?,updated_at=? WHERE id=?",
            (title, content, category, _tags_str(tags), source_path.strip(), _now(), id),
        )
        self.conn.commit()

    def delete(self, id):
        self.conn.execute("DELETE FROM entries WHERE id=?", (id,))
        self.conn.commit()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _tags_str(tags: list[str]) -> str:
    return ",".join(t.strip() for t in tags if t.strip())


def _display_path(entry: dict) -> str:
    source_path = (entry.get("source_path") or "").strip()
    if source_path:
        return source_path
    return f"entry://{entry.get('id', '?')}-{entry.get('title', 'untitled')}"


def _query_terms(query: str) -> list[str]:
    return [w for w in re.split(r"\s+", (query or "").strip()) if w]


def _all_match_spans(text: str, query: str) -> list[tuple[int, int]]:
    if not text or not (query or "").strip():
        return []
    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    tokens = [query.strip()] + [t for t in _query_terms(query) if len(t) >= 2]
    unique_tokens: list[str] = []
    seen_tokens: set[str] = set()
    for tok in tokens:
        key = tok.lower()
        if key not in seen_tokens:
            seen_tokens.add(key)
            unique_tokens.append(tok)
    for tok in sorted(unique_tokens, key=len, reverse=True):
        for m in re.finditer(re.escape(tok), text, flags=re.IGNORECASE):
            span = m.span()
            if span not in seen:
                seen.add(span)
                spans.append(span)
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(a, b) for a, b in merged]


def _text_with_highlights(text: str, query: str, base_style: Optional[str] = None, hit_style: Optional[str] = None) -> Text:
    out = Text(text or "", style=base_style or "")
    for start, end in _all_match_spans(text or "", query):
        out.stylize(hit_style or f"bold black on {PX['yellow']}", start, end)
    return out


def _fuzzy_score(text: str, query: str) -> int:
    text = (text or "").lower()
    query = "".join((query or "").lower().split())
    if not text or not query:
        return 0
    qi = 0
    first = -1
    consecutive = 0
    bonus = 0
    for i, ch in enumerate(text):
        if qi < len(query) and ch == query[qi]:
            if first < 0:
                first = i
            qi += 1
            consecutive += 1
            bonus += 8 + min(consecutive, 6)
        else:
            consecutive = 0
    if qi != len(query):
        return 0
    start_bonus = max(0, 40 - max(first, 0))
    density_bonus = max(0, 30 - max(0, len(text) - len(query)))
    return bonus + start_bonus + density_bonus


def _collect_entry_hits(entry: dict, query: str, limit: int = 8) -> list[dict]:
    """
    Strict literal search: every word in `query` must appear in the SAME line
    (or title/tags field). No fuzzy, no cross-line fallback.
    Returns list of hit dicts compatible with the existing renderer.
    """
    raw = (query or "").strip()
    if not raw:
        return []

    words = [w for w in raw.lower().split() if w]
    hits:  list[dict] = []
    seen:  set[tuple] = set()

    def _add(field: str, line_no: Optional[int], line_text: str, column: Optional[int] = None):
        key = (field, line_no, line_text)
        if key in seen:
            return
        seen.add(key)
        hits.append({
            "field":       field,
            "line_no":     line_no,
            "line_text":   line_text,
            "path":        _display_path(entry),
            "match_count": sum(line_text.lower().count(w) for w in words),
            "source":      "db",
            "column":      column,
            "fuzzy":       False,
            "fuzzy_score": 0,
        })

    title     = entry.get("title", "")
    tags_text = " ".join(entry.get("tags", []))

    # Title: all words must appear in the title
    if title and all(w in title.lower() for w in words):
        _add("title", 1, title, title.lower().find(words[0]) + 1)

    # Tags: all words must appear in the tags string
    if tags_text and all(w in tags_text.lower() for w in words):
        _add("tags", 1, tags_text, tags_text.lower().find(words[0]) + 1)

    # Content: all words must appear in the SAME line
    for idx, line in enumerate(entry.get("content", "").splitlines(), 1):
        lower = line.lower()
        if all(w in lower for w in words):
            _add("content", idx, line, lower.find(words[0]) + 1)
            if len(hits) >= limit:
                break

    return hits[:limit]


def _match_meta(entry: dict, query: str) -> Optional[dict]:
    """Return the best single hit for preview/list display, or empty meta if no query."""
    raw = (query or "").strip()
    if not raw:
        return {
            "line_no": None, "line_text": "", "field": None,
            "path": _display_path(entry), "match_count": 0,
            "source": "db", "column": None,
        }
    hits = _collect_entry_hits(entry, raw, limit=1)
    if hits:
        return hits[0]
    return None


def search_entries(entries: list[dict], query: str, cat: Optional[str]) -> list[dict]:
    base_entries = [e for e in entries if not cat or e["category"] == cat]

    raw = (query or "").strip()
    if not raw:
        out = []
        for e in base_entries:
            x = dict(e)
            x["_match"]  = _match_meta(x, "")
            x["_hits"]   = []
            x["_origin"] = "db"
            out.append(x)
        return out

    ranked = []
    for e in base_entries:
        hits = _collect_entry_hits(e, raw)
        if not hits:
            continue
        x           = dict(e)
        x["_hits"]  = hits
        x["_match"] = hits[0]
        x["_origin"]= "db"
        primary     = hits[0]

        # Score purely by where the match is and how many hits there are
        if   primary["field"] == "title":   score = 300
        elif primary["field"] == "tags":    score = 200
        else:                               score = 100 + max(0, 50 - min(primary.get("line_no") or 50, 50))
        score += min(sum(h.get("match_count", 0) for h in hits), 40)
        score += min(len(hits) * 6, 36)

        ranked.append((score, x))

    ranked.sort(key=lambda t: (-t[0], t[1].get("title", "").lower()))
    return [t[1] for t in ranked]

def render_preview(entry: dict, width: int = 80, query: str = "") -> Group:
    cat = entry["category"]
    cc = CAT_COLOR.get(cat, PX["cyan"])
    lbl = CAT_LABEL.get(cat, cat.upper())
    match = entry.get("_match") or {}
    hits = entry.get("_hits") or ([] if not match else [match])
    source_path = _display_path(entry)
    line_no = match.get("line_no")
    origin = (entry.get("_origin") or match.get("source") or "db").upper()

    parts: list[Text] = []
    parts.append(Text("  Title    ", style=PX["muted"]) + _text_with_highlights(entry["title"], query, base_style=f"bold {cc}"))
    parts.append(Text("  Category ", style=PX["muted"]) + Text(lbl, style=cc))
    tags_text = ", ".join(entry.get("tags", [])) or "(none)"
    parts.append(Text("  Tags     ", style=PX["muted"]) + _text_with_highlights(tags_text, query, base_style=PX["blue"]))
    parts.append(Text("  File     ", style=PX["muted"]) + Text(source_path, style=PX["cyan"]))
    parts.append(Text("  Source   ", style=PX["muted"]) + Text(origin, style=PX["purple"]))
    parts.append(Text("  Updated  ", style=PX["muted"]) + Text(entry.get("updated_at", "")))
    if match.get("line_text"):
        t = Text("  Match    ", style=PX["muted"])
        t.append(f"line {line_no if line_no is not None else '-'}", style=PX["yellow"])
        col = match.get("column")
        if col:
            t.append(f" col {col}", style=PX["orange"])
        if len(hits) > 1:
            t.append(f"   +{len(hits) - 1} more", style=PX["muted"])
        t.append("  ")
        t += _text_with_highlights(match.get("line_text", ""), query, base_style=PX["fg2"])
        parts.append(t)
    parts.append(Text("  " + ("─" * min(width - 4, 72)), style=PX["bg3"]))
    parts.append(Text(""))

    content_lines = entry.get("content", "").splitlines()
    if entry.get("_origin") == "rg" and match.get("path"):
        try:
            path_obj = Path(match.get("path"))
            if path_obj.exists():
                content_lines = path_obj.read_text(errors="replace").splitlines()
        except Exception:
            content_lines = []
    if entry.get("_origin") == "rg" and not content_lines and match.get("line_text"):
        content_lines = [match.get("line_text", "")]

    in_code = False
    if query.strip() and content_lines and line_no:
        start_line = max(0, line_no - 4)
        end_line = min(len(content_lines), line_no + 18)
        if start_line > 0:
            parts.append(Text(f"  … {start_line} lines above …", style=PX["muted"]))
        window = list(enumerate(content_lines[start_line:end_line], start_line + 1))
    else:
        end_line = len(content_lines)
        window = list(enumerate(content_lines, 1))

    for idx, line in window:
        stripped = line.strip()
        prefix = Text(f"{idx:>4} ", style=PX["muted"])
        if stripped.startswith("```"):
            in_code = not in_code
            lang = stripped[3:].strip()
            bar = Text("  " + ("━" * min(width - 6, 68)), style=PX["bg3"])
            if lang:
                bar.append(f" {lang}")
            parts.append(bar)
            continue
        style = f"bold {PX['green']}" if in_code else PX["fg"]
        line_text = _text_with_highlights(line, query, base_style=style)
        if line_no == idx:
            prefix.stylize(f"bold black on {PX['blue']}")
            line_text.stylize(f"on {PX['bg2']}")
        parts.append(prefix + line_text)
    if query.strip() and content_lines and line_no and end_line < len(content_lines):
        parts.append(Text(f"  … {len(content_lines) - end_line} lines below …", style=PX["muted"]))
    return Group(*parts)


def yank_to_clipboard(text: str) -> str:
    for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]]:
        try:
            if subprocess.run(cmd, input=text.encode(), capture_output=True).returncode == 0:
                return f"yanked via {cmd[0]}"
        except FileNotFoundError:
            continue
    return "install xclip / xsel / wl-copy for clipboard"


HELP_TEXT = f"""\
[bold {PX['yellow']}]  recall — keys[/bold {PX['yellow']}]

  [bold {PX['fg']}]NORMAL[/bold {PX['fg']}]
  [{PX['cyan']}]j / k[/{PX['cyan']}]             down / up
  [{PX['cyan']}]gg / G[/{PX['cyan']}]            top / bottom
  [{PX['cyan']}]Ctrl+d / Ctrl+u[/{PX['cyan']}]   jump 8 lines
  [{PX['cyan']}]Enter[/{PX['cyan']}]             full view
  [{PX['cyan']}]a[/{PX['cyan']}]                 add entry
  [{PX['cyan']}]e[/{PX['cyan']}]                 edit selected
  [{PX['cyan']}]dd[/{PX['cyan']}]                delete selected
  [{PX['cyan']}]yy[/{PX['cyan']}]                yank content
  [{PX['cyan']}]1 2 3 4[/{PX['cyan']}]           filter all/cmd/note/tool
  [{PX['cyan']}]Tab[/{PX['cyan']}]               cycle filter
  [{PX['cyan']}]/[/{PX['cyan']}]                 focus search bar (exact + fuzzy + rg)
  [{PX['cyan']}]:[/{PX['cyan']}]                 focus command bar
  [{PX['cyan']}]m[/{PX['cyan']}]                 cycle top menu
  [{PX['cyan']}]q[/{PX['cyan']}]                 quit

  [bold {PX['fg']}]SEARCH[/bold {PX['fg']}]
  type to live filter, Enter keep, Esc clear

  [bold {PX['fg']}]COMMAND[/bold {PX['fg']}]
  :q  quit   :qa  quit   :help  help   :refresh  reload
  :all / :cmd / :note / :tool   filter

  [bold {PX['fg']}]FORMS[/bold {PX['fg']}]
  j/k move  i/Enter edit  ←/→ change category  ZZ save  :q cancel

  [{PX['muted']}]press any key to close[/{PX['muted']}]
"""


class HelpModal(ModalScreen):
    DEFAULT_CSS = f"""
    HelpModal {{ align: center middle; }}
    #hb {{ background: {PX['bg1']}; border: heavy {PX['blue']}; padding: 1 2; width: 64; height: auto; max-height: 92%; }}
    """

    def compose(self) -> ComposeResult:
        with Container(id="hb"):
            yield Static(HELP_TEXT, markup=True)

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self.dismiss()


class ConfirmModal(ModalScreen):
    DEFAULT_CSS = f"""
    ConfirmModal {{ align: center middle; }}
    #cb {{ background: {PX['bg1']}; border: heavy {PX['red']}; padding: 1 3; width: 60; height: auto; }}
    """

    def __init__(self, msg: str):
        super().__init__()
        self._msg = msg

    def compose(self) -> ComposeResult:
        with Container(id="cb"):
            yield Static(
                f"[bold {PX['red']}]{esc(self._msg)}[/bold {PX['red']}]\n\n"
                f"  [{PX['green']}]y[/{PX['green']}] confirm   [{PX['muted']}]n / Esc[/{PX['muted']}] cancel",
                markup=True,
            )

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self.dismiss(event.key.lower() == "y")


class ViewModal(ModalScreen):
    DEFAULT_CSS = f"""
    ViewModal {{ align: center middle; }}
    #vo {{ background: {PX['bg1']}; border: heavy {PX['cyan']}; width: 94%; height: 92%; }}
    #vbar    {{ height: 1; background: {PX['bg2']}; padding: 0 2; border-bottom: solid {PX['bg3']}; }}
    #vscroll {{ height: 1fr; }}
    #vcontent{{ height: auto; padding: 1 0; }}
    #vhint   {{ height: 1; background: {PX['bg2']}; padding: 0 2; color: {PX['muted']}; border-top: solid {PX['bg3']}; }}
    """

    def __init__(self, entry: dict):
        super().__init__()
        self._entry = entry
        self._pending = ""

    def compose(self) -> ComposeResult:
        cat = self._entry["category"]
        cc = CAT_COLOR[cat]
        with Container(id="vo"):
            yield Static(
                f"[{cc}]{CAT_LABEL[cat]}[/{cc}]  [bold {PX['fg']}]{esc(self._entry['title'])}[/bold {PX['fg']}]",
                id="vbar",
                markup=True,
            )
            with ScrollableContainer(id="vscroll"):
                yield Static(render_preview(self._entry, 96, self._entry.get("_query", "")), id="vcontent", markup=True)
            yield Static(" j/k scroll   gg/G top/bottom   Ctrl+d/u jump   yy yank   e edit   q back", id="vhint")

    def on_key(self, event: events.Key) -> None:
        event.stop()
        k = event.key
        sc = self.query_one("#vscroll", ScrollableContainer)
        if self._pending == "g" and k == "g":
            self._pending = ""
            sc.scroll_home(animate=False)
            return
        if self._pending == "y" and k == "y":
            self._pending = ""
            self.dismiss(f"status:{yank_to_clipboard(self._entry['content'])}")
            return
        if k in ("g", "y"):
            self._pending = k
            return
        self._pending = ""

        if k in ("q", "escape"):
            self.dismiss(None)
        elif k == "e":
            self.dismiss("edit")
        elif k in ("j", "down"):
            sc.scroll_down(animate=False)
        elif k in ("k", "up"):
            sc.scroll_up(animate=False)
        elif k == "ctrl+d":
            for _ in range(8):
                sc.scroll_down(animate=False)
        elif k == "ctrl+u":
            for _ in range(8):
                sc.scroll_up(animate=False)
        elif k == "G":
            sc.scroll_end(animate=False)


FIELD_NAMES = ["title", "category", "tags", "source_path", "content"]
FIELD_LABELS = ["Title", "Category", "Tags", "Source File", "Content"]


class FormModal(ModalScreen):
    DEFAULT_CSS = f"""
    FormModal {{ align: center middle; }}
    #fo {{ background: {PX['bg1']}; border: heavy {PX['blue']}; padding: 0 2 1 2; width: 88; height: auto; max-height: 92%; }}
    #fhdr {{ color: {PX['yellow']}; text-style: bold; height: 2; padding: 1 0 0 0; }}
    .frow {{ height: auto; margin-top: 1; }}
    .fcur {{ width: 2; color: {PX['yellow']}; }}
    .flbl {{ width: 12; color: {PX['muted']}; }}
    .flbl.sel {{ color: {PX['cyan']}; text-style: bold; }}
    #catd {{ width: 1fr; }}
    Input {{ border: solid {PX['bg3']}; background: {PX['bg2']}; color: {PX['fg']}; width: 1fr; }}
    Input:focus {{ border: solid {PX['blue']}; }}
    TextArea {{ border: solid {PX['bg3']}; background: {PX['bg2']}; color: {PX['fg']}; height: 12; width: 1fr; }}
    TextArea:focus {{ border: solid {PX['blue']}; }}
    #fst {{ height: 1; margin-top: 1; }}
    """

    def __init__(self, entry: Optional[dict] = None):
        super().__init__()
        self._entry = entry
        self._nav_idx = 0
        self._editing = False
        self._cat_idx = ALL_CATS.index(entry["category"]) if entry else 0
        self._cmdbuf = ""
        self._zbuf = ""

    def compose(self) -> ComposeResult:
        e = self._entry or {}
        with Container(id="fo"):
            yield Static(f"  {'Edit' if self._entry else 'Add'} Entry", id="fhdr")
            with Horizontal(classes="frow"):
                yield Static(" ", classes="fcur", id="cur-title")
                yield Static(FIELD_LABELS[0], classes="flbl", id="lbl-title")
                yield Input(value=e.get("title", ""), id="f-title", placeholder="title...")
            with Horizontal(classes="frow"):
                yield Static(" ", classes="fcur", id="cur-category")
                yield Static(FIELD_LABELS[1], classes="flbl", id="lbl-category")
                yield Static("", id="catd", markup=True)
            with Horizontal(classes="frow"):
                yield Static(" ", classes="fcur", id="cur-tags")
                yield Static(FIELD_LABELS[2], classes="flbl", id="lbl-tags")
                yield Input(value=", ".join(e.get("tags", [])), id="f-tags", placeholder="tag1, tag2...")
            with Horizontal(classes="frow"):
                yield Static(" ", classes="fcur", id="cur-source_path")
                yield Static(FIELD_LABELS[3], classes="flbl", id="lbl-source_path")
                yield Input(value=e.get("source_path", ""), id="f-source_path", placeholder="/path/to/file.md")
            with Horizontal(classes="frow"):
                yield Static(" ", classes="fcur", id="cur-content")
                yield Static(FIELD_LABELS[4], classes="flbl", id="lbl-content")
                yield TextArea(e.get("content", ""), id="f-content")
            yield Static("", id="fst", markup=True)

    def on_mount(self):
        self._redraw_cat()
        self._redraw_sel()
        self._redraw_status()

    def on_key(self, event: events.Key) -> None:
        k = event.key
        c = event.character or ""
        if self._editing:
            if k == "escape":
                event.stop()
                event.prevent_default()
                self._editing = False
                self.set_focus(None)
                self._redraw_status()
            elif k == "ctrl+o":
                event.stop()
                event.prevent_default()
                self._run_editor()
            return

        event.stop()
        event.prevent_default()
        if self._cmdbuf:
            if k == "escape":
                self._cmdbuf = ""
                self._redraw_status()
                return
            self._cmdbuf += c or k
            if self._cmdbuf == ":w":
                self._cmdbuf = ""
                self._save()
            elif self._cmdbuf == ":q":
                self._cmdbuf = ""
                self.dismiss(None)
            elif len(self._cmdbuf) > 12:
                self._cmdbuf = ""
                self._redraw_status()
            else:
                self._redraw_status(self._cmdbuf)
            return

        if self._zbuf == "Z" and k == "Z":
            self._zbuf = ""
            self._save()
            return
        if k == "Z":
            self._zbuf = "Z"
            return
        self._zbuf = ""

        if k in ("j", "down", "tab"):
            self._nav_idx = (self._nav_idx + 1) % 5
            self._redraw_sel()
        elif k in ("k", "up", "shift+tab"):
            self._nav_idx = (self._nav_idx - 1) % 5
            self._redraw_sel()
        elif k in ("i", "a", "enter"):
            if self._nav_idx == 1:
                self._cycle_cat(1)
            else:
                self._start_insert()
        elif k == "left" and self._nav_idx == 1:
            self._cycle_cat(-1)
        elif k == "right" and self._nav_idx == 1:
            self._cycle_cat(1)
        elif k == "ctrl+o":
            self._nav_idx = 4
            self._redraw_sel()
            self._run_editor()
        elif c == ":":
            self._cmdbuf = ":"
            self._redraw_status(":")
        elif k in ("q", "escape"):
            self.dismiss(None)

    def _start_insert(self):
        wids = ["#f-title", None, "#f-tags", "#f-source_path", "#f-content"]
        wid = wids[self._nav_idx]
        if wid:
            self._editing = True
            self.query_one(wid).focus()
            self._redraw_status()

    def _cycle_cat(self, d: int):
        self._cat_idx = (self._cat_idx + d) % len(ALL_CATS)
        self._redraw_cat()

    def _redraw_cat(self):
        cat = ALL_CATS[self._cat_idx]
        cc = CAT_COLOR[cat]
        lbl = CAT_LABEL[cat]
        self.query_one("#catd", Static).update(
            f"  [{PX['muted']}]◀[/{PX['muted']}]  [{cc}]{lbl}[/{cc}]  [{PX['muted']}]▶[/{PX['muted']}]  [{PX['bg4']}]← → or Enter[/{PX['bg4']}]"
        )

    def _redraw_sel(self):
        for i, name in enumerate(FIELD_NAMES):
            active = i == self._nav_idx
            self.query_one(f"#cur-{name}", Static).update("▶" if active else " ")
            lbl = self.query_one(f"#lbl-{name}", Static)
            if active:
                lbl.add_class("sel")
            else:
                lbl.remove_class("sel")
        self._redraw_status()

    def _redraw_status(self, cmd_hint: str = ""):
        st = self.query_one("#fst", Static)
        if cmd_hint:
            st.update(f"[bold {PX['yellow']}]{esc(cmd_hint)}[/bold {PX['yellow']}][{PX['muted']}]█[/{PX['muted']}]")
        elif self._editing:
            st.update(f"[bold {PX['blue']}]-- INSERT --[/bold {PX['blue']}]  [{PX['muted']}]Esc exit   Ctrl+O external editor[/{PX['muted']}]")
        else:
            st.update(f"[{PX['muted']}]-- NAV --  j/k move   i/Enter edit   ← → cat   ZZ/:w save   :q/q cancel[/{PX['muted']}]")

    def _save(self):
        title = self.query_one("#f-title", Input).value.strip()
        tags_s = self.query_one("#f-tags", Input).value
        source_path = self.query_one("#f-source_path", Input).value.strip()
        content = self.query_one("#f-content", TextArea).text
        if not title:
            self.query_one("#fst", Static).update(f"[bold {PX['red']}]title cannot be empty[/bold {PX['red']}]")
            return
        self.dismiss(
            {
                "title": title,
                "category": ALL_CATS[self._cat_idx],
                "tags": [t.strip() for t in tags_s.split(",") if t.strip()],
                "source_path": source_path,
                "content": content,
            }
        )

    def _run_editor(self):
        editor = os.environ.get("EDITOR", "nano")
        content = self.query_one("#f-content", TextArea).text
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write(content)
            tmp = f.name
        self.app.suspend()
        subprocess.run([editor, tmp])
        self.app.resume()
        try:
            self.query_one("#f-content", TextArea).load_text(Path(tmp).read_text())
        except Exception:
            pass
        finally:
            Path(tmp).unlink(missing_ok=True)
        self._nav_idx = 4
        self._redraw_sel()


APP_CSS = f"""
Screen {{ background: {PX['bg']}; color: {PX['fg']}; }}

#menubar {{
    height: 3; dock: top; background: {PX['blue']}; color: {PX['bg']}; padding: 0 1;
    border-bottom: tall {PX['cyan']};
}}
#titlebar {{
    height: 3; dock: top; background: {PX['bg3']}; color: {PX['fg']}; padding: 0 1; border-bottom: tall {PX['bg4']};
}}
#searchbar {{
    height: 3; dock: top; background: {PX['bg1']}; color: {PX['fg']}; padding: 0 1; border-bottom: tall {PX['green']};
}}
#body {{ height: 1fr; }}
#list-panel {{ width: 38%; border-right: tall {PX['bg4']}; }}
#list-head {{ height: 2; background: {PX['bg2']}; color: {PX['muted']}; padding: 0 1; border-bottom: solid {PX['bg4']}; }}
#list-scroll {{ height: 1fr; }}
#entry-list {{ height: auto; padding: 0 0; }}
#preview-panel {{ width: 62%; }}
#preview-title {{ height: 2; background: {PX['bg2']}; color: {PX['fg']}; padding: 0 2; border-bottom: solid {PX['bg4']}; }}
#preview-scroll {{ height: 1fr; }}
#preview-content {{ height: auto; padding: 1 0; }}
#cmdbar {{
    height: 3; dock: bottom; background: {PX['bg2']}; color: {PX['fg']}; padding: 0 1; border-top: tall {PX['yellow']};
}}
"""


class RecallApp(App):
    CSS = APP_CSS
    TITLE = "recall"

    def __init__(self):
        super().__init__()
        self.db = DB()
        self._all: list[dict] = []
        self._filtered: list[dict] = []
        self._selected = 0
        self._mode = Mode.NORMAL
        self._search_q = ""
        self._cmd_buf = ""
        self._cat_filter: Optional[str] = None
        self._kbuf = ""
        self._status_msg = ""
        self._stimer = None
        self._search_timer = None   # debounce timer for search
        self._menu_items = ["File", "Edit", "View", "Help"]
        self._menu_idx = 0
        self._window_top = 0
        self._window_size = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="menubar", markup=True)
        yield Static("", id="titlebar", markup=True)
        yield Static("", id="searchbar", markup=True)
        with Horizontal(id="body"):
            with Container(id="list-panel"):
                yield Static("", id="list-head", markup=True)
                with ScrollableContainer(id="list-scroll"):
                    yield Static("", id="entry-list", markup=False)
            with Container(id="preview-panel"):
                yield Static("", id="preview-title", markup=True)
                with ScrollableContainer(id="preview-scroll"):
                    yield Static("", id="preview-content", markup=False)
        yield Static("", id="cmdbar", markup=True)

    def on_mount(self):
        self._reload()

    def on_resize(self, event: events.Resize) -> None:
        self._redraw()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        k = event.key
        c = event.character or ""
        {Mode.NORMAL: self._normal, Mode.SEARCH: self._search, Mode.COMMAND: self._command}[self._mode](k, c)

    def _normal(self, k: str, c: str):
        buf = self._kbuf
        if buf == "g" and k == "g":
            self._kbuf = ""
            self._selected = 0
            self._redraw()
            return
        if buf == "d" and k == "d":
            self._kbuf = ""
            self._do_delete()
            return
        if buf == "y" and k == "y":
            self._kbuf = ""
            self._do_yank()
            return
        if k in ("g", "d", "y"):
            self._kbuf = k
            self._draw_bars()
            return
        self._kbuf = ""

        if k in ("j", "down"):
            self._move(1)
        elif k in ("k", "up"):
            self._move(-1)
        elif k == "ctrl+d":
            self._move(8)
        elif k == "ctrl+u":
            self._move(-8)
        elif k == "G":
            self._selected = max(0, len(self._filtered) - 1)
            self._redraw()
        elif k == "enter":
            self._open_view()
        elif c == "a":
            self._open_form(None)
        elif c == "e":
            self._open_form(self._cur())
        elif c == "m":
            self._menu_idx = (self._menu_idx + 1) % len(self._menu_items)
            self._draw_bars()
        elif c == "q":
            self.exit()
        elif c == "?":
            self.push_screen(HelpModal())
        elif c == "1":
            self._set_cat(None)
        elif c == "2":
            self._set_cat("command")
        elif c == "3":
            self._set_cat("note")
        elif c == "4":
            self._set_cat("tool")
        elif k == "tab":
            self._cycle_cat()
        elif c == "/":
            self._mode = Mode.SEARCH
            self._draw_bars()
        elif c == ":":
            self._mode = Mode.COMMAND
            self._cmd_buf = ""
            self._draw_bars()
        elif k == "escape":
            if self._search_q:
                self._search_q = ""
                self._selected = 0
                self._refresh()
            else:
                self._status_msg = ""
                self._draw_bars()

    def _search(self, k: str, c: str):
        if k == "escape":
            self._cancel_search_timer()
            self._search_q = ""
            self._mode = Mode.NORMAL
            self._selected = 0
            self._refresh()
        elif k == "enter":
            self._cancel_search_timer()
            self._mode = Mode.NORMAL
            self._draw_bars()
            # fire immediately on Enter so the current query is applied
            self._refresh()
        elif k == "backspace":
            self._search_q = self._search_q[:-1]
            self._selected = 0
            self._draw_bars()           # instant visual feedback
            self._schedule_search()     # debounced DB search
        elif c and len(c) == 1:
            self._search_q += c
            self._selected = 0
            self._draw_bars()           # instant visual feedback
            self._schedule_search()     # debounced DB search

    def _schedule_search(self):
        """Debounce: cancel any pending search and schedule a new one in 300 ms."""
        self._cancel_search_timer()
        self._search_timer = self.set_timer(0.30, self._do_debounced_search)

    def _cancel_search_timer(self):
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None

    def _do_debounced_search(self):
        self._search_timer = None
        self._filtered = search_entries(self._all, self._search_q, self._cat_filter)
        if self._selected >= len(self._filtered):
            self._selected = max(0, len(self._filtered) - 1)
        self._redraw()

    def _command(self, k: str, c: str):
        if k == "escape":
            self._mode = Mode.NORMAL
            self._cmd_buf = ""
            self._draw_bars()
        elif k == "enter":
            cmd = self._cmd_buf.strip()
            self._mode = Mode.NORMAL
            self._cmd_buf = ""
            self._run_command(cmd)
            self._draw_bars()
        elif k == "backspace":
            self._cmd_buf = self._cmd_buf[:-1]
            self._draw_bars()
        elif c and len(c) == 1:
            self._cmd_buf += c
            self._draw_bars()

    def _run_command(self, cmd: str):
        if cmd in ("q", "q!", "qa", "qa!"):
            self.exit()
        elif cmd in ("help", "h"):
            self.push_screen(HelpModal())
        elif cmd in ("refresh", "reload"):
            self._reload()
            self._flash("reloaded")
        elif cmd == "all":
            self._set_cat(None)
        elif cmd == "cmd":
            self._set_cat("command")
        elif cmd == "note":
            self._set_cat("note")
        elif cmd == "tool":
            self._set_cat("tool")
        elif cmd == "add":
            self._open_form(None)
        elif cmd == "edit":
            self._open_form(self._cur())
        elif cmd == "del":
            self._do_delete()
        elif cmd.startswith("search "):
            self._search_q = cmd[7:].strip()
            self._selected = 0
            self._refresh()
        elif cmd:
            self._flash(f"unknown: {cmd}")

    def _do_delete(self):
        entry = self._cur()
        if not entry:
            return

        def _done(ok):
            if ok:
                self.db.delete(entry["id"])
                self._reload()
                self._flash(f"deleted: {entry['title']}")

        self.push_screen(ConfirmModal(f"delete: {entry['title']!r} ?"), _done)

    def _do_yank(self):
        entry = self._cur()
        if entry:
            self._flash(yank_to_clipboard(entry["content"]))

    def _open_view(self):
        entry = self._cur()
        if not entry:
            return

        def _done(result):
            if isinstance(result, str) and result.startswith("status:"):
                self._flash(result[7:])
            elif result == "edit":
                self._open_form(entry)

        entry_for_view = dict(entry)
        entry_for_view["_query"] = self._search_q
        self.push_screen(ViewModal(entry_for_view), _done)

    def _open_form(self, entry: Optional[dict]):
        def _done(result):
            if not result:
                return
            if entry:
                self.db.update(entry["id"], result["title"], result["content"], result["category"], result["tags"], result.get("source_path", ""))
                self._flash(f"updated: {result['title']}")
            else:
                self.db.add(result["title"], result["content"], result["category"], result["tags"], result.get("source_path", ""))
                self._flash(f"added: {result['title']}")
            self._reload()

        self.push_screen(FormModal(entry), _done)

    def _reload(self):
        self._all = self.db.all()
        self._refresh()

    def _refresh(self):
        self._filtered = search_entries(self._all, self._search_q, self._cat_filter)
        if self._selected >= len(self._filtered):
            self._selected = max(0, len(self._filtered) - 1)
        self._redraw()

    def _move(self, d: int):
        if not self._filtered:
            return
        self._selected = max(0, min(len(self._filtered) - 1, self._selected + d))
        self._redraw()

    def _cur(self) -> Optional[dict]:
        if 0 <= self._selected < len(self._filtered):
            return self._filtered[self._selected]
        return None

    def _set_cat(self, cat: Optional[str]):
        self._cat_filter = cat
        self._selected = 0
        self._refresh()

    def _cycle_cat(self):
        cats = [None] + ALL_CATS
        idx = cats.index(self._cat_filter) if self._cat_filter in cats else 0
        self._set_cat(cats[(idx + 1) % len(cats)])

    def _flash(self, msg: str):
        self._status_msg = msg
        self._draw_bars()
        if self._stimer:
            self._stimer.cancel()
        self._stimer = self.set_timer(2.5, self._clear_flash)

    def _clear_flash(self):
        self._status_msg = ""
        self._stimer = None
        self._draw_bars()

    def _category_badge(self, cat: Optional[str]) -> str:
        mapping = {
            None: ("ALL", PX["blue"]),
            "command": ("CMD", CAT_COLOR["command"]),
            "note": ("NOTE", CAT_COLOR["note"]),
            "tool": ("TOOL", CAT_COLOR["tool"]),
        }
        label, color = mapping[cat]
        return f"[{color}] {label} [/{color}]"

    def _draw_bars(self):
        self.query_one("#menubar", Static).update(self._render_menu_bar())
        self.query_one("#titlebar", Static).update(self._render_title_bar())
        self.query_one("#searchbar", Static).update(self._render_search_bar())
        self.query_one("#cmdbar", Static).update(self._render_cmd_bar())

    def _redraw(self):
        self._draw_bars()
        self.query_one("#list-head", Static).update(self._render_list_head())
        self.query_one("#entry-list", Static).update(self._render_list_body())
        self.query_one("#preview-title", Static).update(self._render_preview_title())
        self.query_one("#preview-content", Static).update(self._render_preview_body())
        self._sync_scroll()

    def _render_menu_bar(self) -> str:
        chunks = []
        for i, name in enumerate(self._menu_items):
            if i == self._menu_idx:
                chunks.append(f"[bold black on {PX['yellow']}] {name} [/bold black on {PX['yellow']}]")
            else:
                chunks.append(f"[bold] {name} [/bold]")
        return (
            f"[bold black] MENU [/bold black]\n"
            + "  ".join(chunks)
            + f"  [black]m cycle menus[/black]"
        )

    def _render_title_bar(self) -> str:
        total = len(self._all)
        visible = len(self._filtered)
        mode = self._mode.value
        cat = self._category_badge(self._cat_filter)
        current = self._selected + 1 if visible else 0
        return (
            f"[bold {PX['cyan']}] RECALL DASHBOARD [/bold {PX['cyan']}]\n"
            f" {cat}   [{PX['muted']}]mode[/{PX['muted']}] [bold {PX['green']}]{mode}[/bold {PX['green']}]   "
            f"[{PX['muted']}]entry[/{PX['muted']}] {current}/{visible}   [{PX['muted']}]db[/{PX['muted']}] {total}"
        )

    def _render_search_bar(self) -> str:
        active = self._mode == Mode.SEARCH
        prefix_color = PX['green'] if active else PX['muted']
        cursor = f"[bold {PX['green']}]█[/bold {PX['green']}]" if active else ""
        hint = f"[{PX['muted']}]type after / for exact line, fuzzy ranking, and ripgrep live file hits[/{PX['muted']}]"
        return (
            f"[bold {PX['green']}] SEARCH [/bold {PX['green']}]\n"
            f"[bold {prefix_color}]/[/bold {prefix_color}][{PX['fg']}]{esc(self._search_q)}[/{PX['fg']}]"
            f"{cursor}  {hint}"
        )

    def _render_cmd_bar(self) -> str:
        if self._mode == Mode.COMMAND:
            body = f"[bold {PX['yellow']}]:[/bold {PX['yellow']}][{PX['fg']}]{esc(self._cmd_buf)}[/{PX['fg']}][bold {PX['yellow']}]█[/bold {PX['yellow']}]"
        elif self._status_msg:
            body = f"[{PX['green']}]{esc(self._status_msg)}[/{PX['green']}]"
        elif self._kbuf:
            body = f"[{PX['purple']}]pending: {esc(self._kbuf)}[/{PX['purple']}]"
        else:
            body = f"[{PX['muted']}]j/k move  gg/G jump  Enter view  a add  e edit  dd delete  yy yank  : commands  ? help[/{PX['muted']}]"
        return f"[bold {PX['yellow']}] COMMAND [/bold {PX['yellow']}]\n{body}"

    def _render_list_head(self) -> str:
        visible = len(self._filtered)
        current = self._selected + 1 if visible else 0
        return (
            f"[bold {PX['blue']}] ENTRY LIST [/bold {PX['blue']}]\n"
            f"[{PX['muted']}]row[/{PX['muted']}] {current}/{visible}   [{PX['muted']}]filter[/{PX['muted']}] {self._category_badge(self._cat_filter)}"
        )

    def _render_list_body(self):
        if not self._filtered:
            self._window_top = 0
            return Text("\n  No matching entries. Press a to add one.", style=PX["muted"])

        height = max(8, self.size.height - 8)
        self._window_size = height
        if self._selected < self._window_top:
            self._window_top = self._selected
        elif self._selected >= self._window_top + height:
            self._window_top = self._selected - height + 1

        end = min(len(self._filtered), self._window_top + height)
        rows: list[Text] = []
        for idx in range(self._window_top, end):
            e = self._filtered[idx]
            cat = e["category"]
            cc = CAT_COLOR.get(cat, PX["cyan"])
            tags = " ".join(f"#{t}" for t in e.get("tags", [])[:3])
            updated = e.get("updated_at", "")
            match = e.get("_match") or {}
            hits = e.get("_hits") or ([] if not match else [match])
            source_name = Path(match.get("path") or _display_path(e)).name or (match.get("path") or _display_path(e))
            origin = (e.get("_origin") or match.get("source") or "db").upper()

            row1 = Text()
            if idx == self._selected:
                row1.append(f"› {CAT_LABEL.get(cat, cat.upper()):<4}  ", style=f"black on {PX['blue']}")
                row1 += _text_with_highlights(e.get("title", ""), self._search_q, base_style=f"black on {PX['blue']}")
            else:
                row1.append(f"  {CAT_LABEL.get(cat, cat.upper()):<4}", style=cc)
                row1.append("  ")
                row1 += _text_with_highlights(e.get("title", ""), self._search_q, base_style=PX["fg2"])
            rows.append(row1)

            meta = Text("    ")
            meta.append("file:", style=PX["bg4"])
            meta.append(f" {source_name} ", style=PX["muted"])
            meta.append("hits:", style=PX["bg4"])
            meta.append(f" {len(hits)} ", style=PX["yellow"])
            meta.append("src:", style=PX["bg4"])
            meta.append(f" {origin} ", style=PX["purple"])
            if tags:
                meta.append("tags:", style=PX["bg4"])
                meta.append(f" {tags} ", style=PX["muted"])
            meta.append("updated:", style=PX["bg4"])
            meta.append(f" {updated}", style=PX["muted"])
            rows.append(meta)

            shown = 0
            for hit in hits[:2]:
                line_text = (hit.get("line_text") or "").strip()
                if not line_text:
                    continue
                if hit.get("field") == "title" and line_text == e.get("title", "") and shown > 0:
                    continue
                row = Text("    ")
                label = f"L{hit.get('line_no')}" if hit.get("line_no") is not None else str(hit.get("field") or "-").upper()
                row.append(f"{label:>4} ", style=PX["yellow"])
                row += _text_with_highlights(line_text[:160], self._search_q, base_style=PX["fg2"])
                rows.append(row)
                shown += 1
            if len(hits) > shown:
                rows.append(Text(f"      … {len(hits) - shown} more hit(s)", style=PX["muted"]))
        if end < len(self._filtered):
            rows.append(Text(f"\n  … {len(self._filtered) - end} more entries below", style=PX["muted"]))

        return Group(*rows)

    def _render_preview_title(self) -> str:
        entry = self._cur()
        if not entry:
            return f"[bold {PX['cyan']}] PREVIEW [/bold {PX['cyan']}]\n[{PX['muted']}]no entry selected[/{PX['muted']}]"
        cat = entry["category"]
        cc = CAT_COLOR[cat]
        return (
            f"[bold {PX['cyan']}] PREVIEW [/bold {PX['cyan']}]\n"
            f"[{cc}] {CAT_LABEL[cat]} [/{cc}]  [bold {PX['fg']}]{esc(entry['title'])}[/bold {PX['fg']}]"
        )

    def _render_preview_body(self):
        entry = self._cur()
        if not entry:
            return f"\n  [{PX['muted']}]Nothing selected.[/{PX['muted']}]"
        width = max(70, self.size.width - 44)
        return render_preview(entry, width, self._search_q)

    def _sync_scroll(self):
        lsc = self.query_one("#list-scroll", ScrollableContainer)
        current = self._cur()
        approx_rows = 4 if (current and (current.get("_hits") or [])) else 3
        list_y = max(0, (self._selected - self._window_top) * approx_rows)
        lsc.scroll_to(y=list_y, animate=False)
        psc = self.query_one("#preview-scroll", ScrollableContainer)
        entry = self._cur()
        if not entry:
            psc.scroll_to(y=0, animate=False)
            return
        match = entry.get("_match") or {}
        line_no = match.get("line_no") or 1
        psc.scroll_to(y=max(0, line_no + 4), animate=False)


def cli():
    args = sys.argv[1:]
    if not args:
        RecallApp().run()
        return
    cmd = args[0]
    if cmd == "add":
        import argparse

        p = argparse.ArgumentParser(prog="recall add")
        p.add_argument("-t", "--title", required=True)
        p.add_argument("-c", "--content", default="")
        p.add_argument("-C", "--category", default="note", choices=ALL_CATS)
        p.add_argument("--tags", default="")
        p.add_argument("--source", default="")
        ns = p.parse_args(args[1:])
        DB().add(ns.title, ns.content, ns.category, [t.strip() for t in ns.tags.split(",") if t.strip()], ns.source)
        print(f"added: {ns.title}")
    elif cmd == "search":
        if len(args) < 2:
            print("usage: recall search <query>")
            sys.exit(1)
        for e in search_entries(DB().all(), " ".join(args[1:]), None):
            match = e.get("_match") or {}
            path = match.get("path") or _display_path(e)
            line_no = match.get("line_no") or 1
            col = match.get("column") or 1
            line_text = (match.get("line_text") or e.get("title") or "").strip()
            origin = e.get("_origin") or match.get("source") or "db"
            print(f"{path}:{line_no}:{col}: [{origin}/{e['category']}] {e['title']}")
            if line_text:
                print(f"    {line_text}")
    else:
        print(f"unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
