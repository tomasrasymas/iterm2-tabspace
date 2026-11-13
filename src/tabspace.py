#!/usr/bin/env python3
"""Tabspace: a live, read-only overview of all your iTerm2 tabs, inside a tab.

This single file has two roles:

* Service (default): an iTerm2 AutoLaunch Python API script. It watches all
  windows, tabs and split panes, reads their screen contents (read-only), and
  renders an overview grid. It owns the keyboard shortcut and the
  ``tabspace_toggle()`` script function.

* Viewer (``--viewer SOCKET``): a tiny standard-library terminal client that
  runs inside the dedicated Overview tab. It shows the frames the service
  renders and reports clicks, key presses and resizes back to it. It has no
  access to other sessions; it can only ask the service to *switch* tabs.

Nothing in this file ever sends input to, resizes, or otherwise modifies a
session other than the Overview tab itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field

VERSION = "1.0.0"
ROLE_VARIABLE = "user.tabspace_role"
ROLE_OVERVIEW = "overview"
OVERVIEW_TITLE = "Tabspace"
CONFIG_PATH = os.path.expanduser("~/.config/tabspace/config.json")
DEFAULT_CONFIG = {
    # Global shortcut handled by the service. Set to null/"" to disable it and
    # bind the script function tabspace_toggle() in iTerm2 Settings instead.
    "shortcut": "cmd+opt+o",
    # Seconds between preview refreshes while the Overview tab is visible.
    "refresh_seconds": 1.0,
    # Seconds a tab stays "active" (highlighted border) after its output changed.
    "activity_seconds": 5.0,
}


def socket_path() -> str:
    return os.path.join(tempfile.gettempdir(), f"tabspace-{os.getuid()}.sock")


def load_config(path: str = CONFIG_PATH) -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            config.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log(f"Ignoring unreadable config {path}: {e}")
    try:
        config["refresh_seconds"] = max(0.25, float(config["refresh_seconds"]))
        config["activity_seconds"] = max(0.0, float(config["activity_seconds"]))
    except (TypeError, ValueError):
        config["refresh_seconds"] = DEFAULT_CONFIG["refresh_seconds"]
        config["activity_seconds"] = DEFAULT_CONFIG["activity_seconds"]
    return config


def log(msg: str) -> None:
    # Shows up in iTerm2's Scripts > Manage > Console.
    print(f"[tabspace] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Rendering model (pure Python, no iTerm2 dependency; unit-tested).
# ---------------------------------------------------------------------------

# A rendered line is a list of (text, sgr, cells) runs; sgr is the SGR
# parameter string ("" for default style) and cells is the display width.
Run = tuple  # (str, str, int)


@dataclass
class PaneNode:
    session_id: str
    cols: int
    rows: int
    # Called with the number of rows available; returns up to that many lines.
    lines: object = None  # Callable[[int, int], list[list[Run]]]


@dataclass
class SplitNode:
    vertical: bool  # True: vertical divider, children side by side.
    children: list = field(default_factory=list)


@dataclass
class Tile:
    tab_id: str
    title: str
    root: object  # PaneNode | SplitNode
    busy: bool = False
    window_label: str = ""
    color: tuple | None = None  # the tab's iTerm2 tab color as (r, g, b), if set


@dataclass
class Hit:
    x0: int
    y0: int
    x1: int  # exclusive
    y1: int  # exclusive
    tab_id: str
    session_id: str | None


def node_size(node) -> tuple[int, int]:
    """Natural (cols, rows) of a layout node, including 1-cell dividers."""
    if isinstance(node, PaneNode):
        return max(1, node.cols), max(1, node.rows)
    sizes = [node_size(c) for c in node.children] or [(1, 1)]
    if node.vertical:
        return sum(w for w, _ in sizes) + len(sizes) - 1, max(h for _, h in sizes)
    return max(w for w, _ in sizes), sum(h for _, h in sizes) + len(sizes) - 1


def split_lengths(total: int, weights: list[int]) -> list[int]:
    """Split total cells (after dividers) proportionally to weights."""
    n = len(weights)
    avail = total - (n - 1)
    if avail <= 0:
        return [0] * n
    wsum = sum(max(1, w) for w in weights)
    out = [max(0, (avail * max(1, w)) // wsum) for w in weights]
    # Hand out rounding leftovers, largest weights first.
    leftover = avail - sum(out)
    for i in sorted(range(n), key=lambda i: -weights[i])[: max(0, leftover)]:
        out[i] += 1
    return out


def grid_dims(n: int, width: int, height: int) -> tuple[int, int]:
    """Pick columns/rows for n tiles so tiles are as large and readable as possible."""
    if n <= 0:
        return 0, 0
    best, best_score = (1, n), -1.0
    for cols in range(1, n + 1):
        rows = -(-n // cols)
        w, h = width // cols, height // rows
        if w < 8 or h < 4:
            continue
        # Terminal cells are ~2x taller than wide; aim for tiles ~3:1 in cells.
        score = min(w / 3.0, float(h)) * 1000 - (cols * rows - n)
        if score > best_score:
            best, best_score = (cols, rows), score
    return best


def sgr(code: str) -> str:
    return f"\x1b[0;{code}m" if code else "\x1b[0m"


def char_width(ch: str) -> int:
    if not ch:
        return 0
    if unicodedata.east_asian_width(ch[0]) in ("W", "F"):
        return 2
    return 1


def crop_runs(runs: list, width: int) -> tuple[str, int]:
    """Render runs into at most `width` cells. Returns (ansi, used_cells)."""
    out = []
    used = 0
    for text, code, cells in runs:
        if used >= width:
            break
        if used + cells <= width:
            out.append(sgr(code) + text)
            used += cells
            continue
        # Partial run: walk characters (each run element may be a grapheme).
        piece = []
        for ch in text:
            w = char_width(ch)
            if used + w > width:
                break
            piece.append(ch)
            used += w
        out.append(sgr(code) + "".join(piece))
        break
    return "".join(out), used


class Canvas:
    """Collects positioned writes into one frame (cursor-addressed)."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.parts: list[str] = []

    def put(self, x: int, y: int, text: str) -> None:
        if 0 <= y < self.height and 0 <= x < self.width:
            self.parts.append(f"\x1b[{y + 1};{x + 1}H{text}")

    def frame(self) -> str:
        # Synchronized update (DEC 2026) avoids flicker; clear then redraw.
        return ("\x1b[?2026h\x1b[?25l\x1b[0m\x1b[2J" + "".join(self.parts)
                + "\x1b[0m\x1b[?2026l")


BORDER_IDLE = "90"  # bright black / grey
BORDER_BUSY = "32"  # green
BORDER_TITLE = "1"
DIVIDER = "90"
BUSY_MARKER = "●"


def fit(text: str, width: int) -> tuple[str, int]:
    """Crop text to `width` display cells (adding … when cut). Returns (text, cells)."""
    out, used = [], 0
    total = sum(char_width(ch) for ch in text)
    if total <= width:
        return text, total
    for ch in text:
        w = char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    if width >= 1:
        out.append("…")
        used += 1
    return "".join(out), used


def draw_box(c: Canvas, x: int, y: int, w: int, h: int, color: str, title: str,
             marker: str = "", marker_color: str = "") -> None:
    """Box with a bold title in the border color and an optional colored marker."""
    if w < 2 or h < 2:
        return
    inner = w - 2
    top = sgr(color) + "┌"
    used = 0
    if marker and inner >= 4:
        top += sgr(color) + " " + sgr(marker_color + ";" + BORDER_TITLE) + marker
        used = 1 + char_width(marker)
    label, label_w = fit(f" {title} ", inner - used) if title else ("", 0)
    top += sgr(color + ";" + BORDER_TITLE) + label
    used += label_w
    c.put(x, y, top + sgr(color) + "─" * (inner - used) + "┐")
    for row in range(1, h - 1):
        c.put(x, y + row, sgr(color) + "│")
        c.put(x + w - 1, y + row, sgr(color) + "│")
    c.put(x, y + h - 1, sgr(color) + "└" + "─" * inner + "┘")


def draw_node(c: Canvas, node, x: int, y: int, w: int, h: int, hits: list, tab_id: str) -> None:
    if w <= 0 or h <= 0:
        return
    if isinstance(node, PaneNode):
        hits.append(Hit(x, y, x + w, y + h, tab_id, node.session_id))
        lines = node.lines(w, h) if node.lines else []
        for row, runs in enumerate(lines[:h]):
            text, _ = crop_runs(runs, w)
            c.put(x, y + row, text + "\x1b[0m")
        return
    sizes = [node_size(ch) for ch in node.children]
    if node.vertical:
        widths = split_lengths(w, [s[0] for s in sizes])
        cx = x
        for i, (child, cw) in enumerate(zip(node.children, widths)):
            draw_node(c, child, cx, y, cw, h, hits, tab_id)
            cx += cw
            if i < len(node.children) - 1:
                for row in range(h):
                    c.put(cx, y + row, sgr(DIVIDER) + "│")
                cx += 1
    else:
        heights = split_lengths(h, [s[1] for s in sizes])
        cy = y
        for i, (child, ch_) in enumerate(zip(node.children, heights)):
            draw_node(c, child, x, cy, w, ch_, hits, tab_id)
            cy += ch_
            if i < len(node.children) - 1:
                c.put(x, cy, sgr(DIVIDER) + "─" * w)
                cy += 1


def render(tiles: list, width: int, height: int) -> tuple[str, list]:
    """Render the overview grid. Returns (ansi_frame, hits)."""
    c = Canvas(width, height)
    hits: list[Hit] = []
    header = f" Tabspace · {len(tiles)} tab{'s' if len(tiles) != 1 else ''}"
    help_text = "click a preview or press 1-9 to switch · q closes overview "
    gap = width - len(header) - len(help_text)
    line = header + (" " * gap + help_text if gap > 0 else "")
    c.put(0, 0, sgr("7") + line[:width].ljust(width))
    top = 1
    avail_h = height - top
    if not tiles:
        msg = "No other tabs to show. Open some tabs and they will appear here."
        c.put(max(0, (width - len(msg)) // 2), top + avail_h // 2, sgr("90") + msg[:width])
        return c.frame(), hits
    cols, rows = grid_dims(len(tiles), width, avail_h)
    if cols == 0 or width // cols < 8 or avail_h // rows < 4:
        msg = "Window too small for the overview."
        c.put(0, top + 1, sgr("90") + msg[:width])
        return c.frame(), hits
    tw, th = width // cols, avail_h // rows
    for i, tile in enumerate(tiles):
        col, row = i % cols, i // cols
        x, y = col * tw, top + row * th
        w = tw if col < cols - 1 else width - x
        h = th if row < rows - 1 else height - y
        number = f"{i + 1} " if i < 9 else ""
        title = f"{number}{tile.window_label}{tile.title}"
        if tile.color:
            border = "38;2;%d;%d;%d" % tile.color
        else:
            border = BORDER_BUSY if tile.busy else BORDER_IDLE
        draw_box(c, x, y, w, h, border, title,
                 BUSY_MARKER if tile.busy else "", BORDER_BUSY)
        # The whole tile (border included) switches to the tab; the panes
        # inside additionally focus the clicked pane.
        hits.append(Hit(x, y, x + w, y + h, tile.tab_id, None))
        draw_node(c, tile.root, x + 1, y + 1, w - 2, h - 2, hits, tile.tab_id)
    return c.frame(), hits


def hit_test(hits: list, x: int, y: int) -> Hit | None:
    # Pane hits are appended after their tile hit, so the last match is the
    # most specific one.
    found = None
    for h in hits:
        if h.x0 <= x < h.x1 and h.y0 <= y < h.y1:
            found = h
    return found


def tile_hits(hits: list) -> list:
    return [h for h in hits if h.session_id is None]


# ---------------------------------------------------------------------------
# Converting iTerm2 screen contents into runs.
# ---------------------------------------------------------------------------

def color_sgr(color, foreground: bool) -> str:
    if color is None:
        return ""
    if color.is_rgb:
        rgb = color.rgb
        return f"{38 if foreground else 48};2;{rgb.red};{rgb.green};{rgb.blue}"
    if color.is_standard:
        n = color.standard
        if n < 8:
            return str((30 if foreground else 40) + n)
        if n < 16:
            return str((90 if foreground else 100) + n - 8)
        return f"{38 if foreground else 48};5;{n}"
    return ""  # Alternate (default) colors: use the Overview tab's defaults.


def style_sgr(style) -> str:
    if style is None:
        return ""
    parts = []
    if style.bold:
        parts.append("1")
    if style.faint:
        parts.append("2")
    if style.italic:
        parts.append("3")
    if style.underline:
        parts.append("4")
    if style.inverse:
        parts.append("7")
    if style.strikethrough:
        parts.append("9")
    for code in (color_sgr(style.fg_color, True), color_sgr(style.bg_color, False)):
        if code:
            parts.append(code)
    return ";".join(parts)


def line_to_runs(line, max_cells: int) -> list:
    """Convert an iterm2 LineContents into runs, reading at most max_cells cells."""
    runs: list = []
    cur_text: list[str] = []
    cur_code = None
    cur_cells = 0
    used = 0
    x = 0
    style_cache: dict = {}
    while used < max_cells:
        try:
            ch = line.string_at(x)
        except IndexError:
            break
        style = line.style_at(x)
        x += 1
        if ch == "":
            continue  # right half of a wide character
        if style is not None and style.invisible:
            ch = " "
        sid = id(style)
        code = style_cache.get(sid)
        if code is None:
            code = style_sgr(style)
            style_cache[sid] = code
        if ch in ("\x00", "\t"):
            ch = " "
        w = char_width(ch)
        if code != cur_code and cur_text:
            runs.append(("".join(cur_text), cur_code, cur_cells))
            cur_text, cur_cells = [], 0
        cur_code = code
        cur_text.append(ch)
        cur_cells += w
        used += w
    if cur_text:
        runs.append(("".join(cur_text), cur_code or "", cur_cells))
    return runs


def latest_line_range(contents, rows: int) -> range:
    """Indices of the last `rows` meaningful lines (up to cursor/last text)."""
    n = contents.number_of_lines
    last = -1
    for i in range(n - 1, -1, -1):
        if contents.line(i).string.strip():
            last = i
            break
    try:
        last = max(last, contents.cursor_coord.y - contents.number_of_lines_above_screen)
    except Exception:  # noqa: BLE001
        pass
    end = min(n, last + 1)
    return range(max(0, end - rows), end)


# ---------------------------------------------------------------------------
# Service (runs inside iTerm2's Python runtime).
# ---------------------------------------------------------------------------

def parse_shortcut(spec):
    """'cmd+opt+o' -> (set_of_modifier_names, keycode_name) or None."""
    if not spec or not isinstance(spec, str):
        return None
    tokens = [t.strip().lower() for t in spec.replace("-", "+").split("+") if t.strip()]
    if not tokens:
        return None
    aliases = {
        "cmd": "COMMAND", "command": "COMMAND", "⌘": "COMMAND",
        "opt": "OPTION", "option": "OPTION", "alt": "OPTION", "⌥": "OPTION",
        "ctrl": "CONTROL", "control": "CONTROL", "⌃": "CONTROL",
        "shift": "SHIFT", "⇧": "SHIFT",
    }
    mods = set()
    for t in tokens[:-1]:
        if t not in aliases:
            raise ValueError(f"unknown modifier '{t}' in shortcut '{spec}'")
        mods.add(aliases[t])
    key = tokens[-1]
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        keycode = f"ANSI_{key.upper()}"
    elif key.startswith("f") and key[1:].isdigit():
        keycode = key.upper()
    else:
        names = {"space": "SPACE", "tab": "TAB", "return": "RETURN", "enter": "RETURN",
                 "escape": "ESCAPE", "esc": "ESCAPE", "`": "ANSI_GRAVE", "grave": "ANSI_GRAVE",
                 ".": "ANSI_PERIOD", ",": "ANSI_COMMA", "/": "ANSI_SLASH", ";": "ANSI_SEMICOLON"}
        if key not in names:
            raise ValueError(f"unsupported key '{key}' in shortcut '{spec}'")
        keycode = names[key]
    if not mods:
        raise ValueError(f"shortcut '{spec}' needs at least one modifier")
    return mods, keycode


class Service:
    def __init__(self, connection, app, config: dict) -> None:
        import iterm2  # noqa: F401  (available in iTerm2's runtime)

        self.connection = connection
        self.app = app
        self.config = config
        self.viewers: dict = {}  # writer -> ViewerState
        self.overview_sessions: set[str] = set()
        self.last_hash: dict[str, int] = {}
        self.last_change: dict[str, float] = {}
        self.return_target: str | None = None  # session id to go back to
        self.wake = asyncio.Event()

    # -- overview identification ------------------------------------------

    async def discover_overview_sessions(self) -> None:
        for window in self.app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    try:
                        role = await session.async_get_variable(ROLE_VARIABLE)
                    except Exception:  # noqa: BLE001
                        role = None
                    if role == ROLE_OVERVIEW:
                        self.overview_sessions.add(session.session_id)

    def live_overview_session(self):
        for sid in list(self.overview_sessions):
            session = self.app.get_session_by_id(sid)
            if session is None:
                self.overview_sessions.discard(sid)
                continue
            return session
        return None

    def is_overview_tab(self, tab) -> bool:
        return any(s.session_id in self.overview_sessions for s in tab.sessions)

    def focused_session(self):
        window = self.app.current_terminal_window
        if window is None or window.current_tab is None:
            return None
        return window.current_tab.current_session

    # -- toggle --------------------------------------------------------------

    async def toggle(self) -> None:
        overview = self.live_overview_session()
        focused = self.focused_session()
        if overview is not None and focused is not None and focused.session_id == overview.session_id:
            # Pressed while looking at the overview: go back where we came from.
            target = self.app.get_session_by_id(self.return_target) if self.return_target else None
            if target is not None:
                await target.async_activate(select_tab=True, order_window_front=True)
            return
        if focused is not None:
            self.return_target = focused.session_id
        if overview is not None:
            await overview.async_activate(select_tab=True, order_window_front=True)
            await self.app.async_activate()
            self.wake.set()
            return
        await self.create_overview()

    async def create_overview(self) -> None:
        import iterm2

        window = self.app.current_terminal_window
        script = os.path.abspath(__file__)
        command = " ".join(shlex.quote(p) for p in (sys.executable, script, "--viewer", socket_path()))
        if window is None:
            window = await iterm2.Window.async_create(self.connection, command=command)
            tab = window.current_tab if window else None
        else:
            tab = await window.async_create_tab(command=command)
        if tab is None:
            log("Could not create the Overview tab")
            return
        session = tab.current_session
        self.overview_sessions.add(session.session_id)
        await session.async_set_variable(ROLE_VARIABLE, ROLE_OVERVIEW)
        await tab.async_set_title(OVERVIEW_TITLE)
        await self.app.async_activate()

    # -- snapshot ------------------------------------------------------------

    def overview_visible(self) -> bool:
        """True if any Overview tab is the selected tab of its window."""
        for sid in self.overview_sessions:
            session = self.app.get_session_by_id(sid)
            if session is None:
                continue
            window, tab = self.app.get_window_and_tab_for_session(session)
            if window and tab and window.current_tab and window.current_tab.tab_id == tab.tab_id:
                return True
        return False

    async def read_session(self, session):
        try:
            return await session.async_get_screen_contents()
        except Exception:  # noqa: BLE001  (e.g. browser sessions)
            return None

    def build_node(self, node, contents: dict):
        import iterm2

        if isinstance(node, iterm2.Session):
            size = node.grid_size
            cols = size.width if size else 80
            rows = size.height if size else 24
            sc = contents.get(node.session_id)

            def lines(width: int, height: int, sc=sc):
                if sc is None:
                    return [[("(no preview available)", "90", 22)]]
                return [line_to_runs(sc.line(i), width) for i in latest_line_range(sc, height)]

            return PaneNode(node.session_id, cols, rows, lines)
        return SplitNode(bool(node.vertical), [self.build_node(c, contents) for c in node.children])

    async def snapshot(self) -> list:
        tabs = []
        windows = self.app.terminal_windows
        for wi, window in enumerate(windows):
            for tab in window.tabs:
                if self.is_overview_tab(tab):
                    continue
                tabs.append((wi, window, tab))
        sessions = [s for _, _, tab in tabs for s in tab.sessions]
        results = await asyncio.gather(*(self.read_session(s) for s in sessions))
        contents = {s.session_id: r for s, r in zip(sessions, results)}
        titles = await asyncio.gather(*(self.tab_title(tab) for _, _, tab in tabs))
        dark = await self.dark_mode()
        colors = await asyncio.gather(*(self.tab_color(tab, dark) for _, _, tab in tabs))
        now = time.monotonic()
        tiles = []
        for (wi, window, tab), title, color in zip(tabs, titles, colors):
            busy = False
            for s in tab.sessions:
                sc = contents.get(s.session_id)
                if sc is None:
                    continue
                digest = hash(tuple(sc.line(i).string for i in range(sc.number_of_lines)))
                if s.session_id in self.last_hash and self.last_hash[s.session_id] != digest:
                    self.last_change[s.session_id] = now
                self.last_hash[s.session_id] = digest
                if now - self.last_change.get(s.session_id, -1e9) < self.config["activity_seconds"]:
                    busy = True
            label = f"W{wi + 1} · " if len(windows) > 1 else ""
            tiles.append(Tile(tab.tab_id, title, self.build_node(tab.root, contents), busy, label,
                              color))
        return tiles

    async def dark_mode(self) -> bool:
        try:
            return "dark" in await self.app.async_get_theme()
        except Exception:  # noqa: BLE001
            return True

    async def tab_color(self, tab, dark: bool):
        """The tab color iTerm2 shows for this tab (from its active pane), or None."""
        session = tab.current_session
        if session is None:
            return None
        try:
            profile = await session.async_get_profile()
            if profile.use_separate_colors_for_light_and_dark_mode:
                use = profile.use_tab_color_dark if dark else profile.use_tab_color_light
                color = profile.tab_color_dark if dark else profile.tab_color_light
            else:
                use, color = profile.use_tab_color, profile.tab_color
        except Exception:  # noqa: BLE001
            return None
        if not use or color is None:
            return None
        return tuple(max(0, min(255, round(v))) for v in (color.red, color.green, color.blue))

    async def tab_title(self, tab) -> str:
        try:
            value = await tab.async_get_variable("title")
            if value:
                return str(value)
        except Exception:  # noqa: BLE001
            pass
        session = tab.current_session
        return session.name if session and session.name else "Untitled"

    # -- viewer protocol -----------------------------------------------------

    async def handle_viewer(self, reader, writer) -> None:
        state = ViewerState(writer)
        self.viewers[writer] = state
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                await self.on_viewer_message(state, msg)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.viewers.pop(writer, None)
            writer.close()

    async def on_viewer_message(self, state, msg: dict) -> None:
        kind = msg.get("type")
        if kind in ("hello", "resize"):
            state.cols = int(msg.get("cols", state.cols))
            state.rows = int(msg.get("rows", state.rows))
            sid = str(msg.get("session_id") or "")
            if kind == "hello" and sid:
                state.session_id = sid
                session = self.app.get_session_by_id(sid)
                if sid not in self.overview_sessions:
                    self.overview_sessions.add(sid)
                    if session is not None:
                        await session.async_set_variable(ROLE_VARIABLE, ROLE_OVERVIEW)
                if session is not None:
                    # Keep the tab name current (also renames tabs opened by
                    # older versions, e.g. "Overview" -> "Tabspace").
                    _, tab = self.app.get_window_and_tab_for_session(session)
                    if tab is not None:
                        await tab.async_set_title(OVERVIEW_TITLE)
            state.last_frame = None
            self.wake.set()
        elif kind == "click":
            hit = hit_test(state.hits, int(msg.get("x", -1)), int(msg.get("y", -1)))
            if hit is not None:
                await self.navigate(hit.tab_id, hit.session_id)
        elif kind == "close":
            await self.close_overview(state.session_id)
        elif kind == "key":
            key = str(msg.get("key", ""))
            if key.isdigit() and key != "0":
                tiles = tile_hits(state.hits)
                idx = int(key) - 1
                if idx < len(tiles):
                    await self.navigate(tiles[idx].tab_id, None)
            elif key == "r":
                state.last_frame = None
                self.wake.set()

    async def close_overview(self, session_id: str) -> None:
        """Close the Overview tab that asked for it. Only ever closes Overview tabs."""
        if session_id not in self.overview_sessions:
            return
        session = self.app.get_session_by_id(session_id)
        if session is None:
            return
        _, tab = self.app.get_window_and_tab_for_session(session)
        if tab is not None and all(s.session_id in self.overview_sessions for s in tab.sessions):
            self.overview_sessions.discard(session_id)
            await tab.async_close(force=True)

    async def navigate(self, tab_id: str, session_id: str | None) -> None:
        """Switch to a tab (and optionally focus one of its panes). Never sends input."""
        if session_id:
            session = self.app.get_session_by_id(session_id)
            if session is not None:
                await session.async_activate(select_tab=True, order_window_front=True)
                return
        tab = self.app.get_tab_by_id(tab_id)
        if tab is not None:
            await tab.async_activate(order_window_front=True)

    async def send_frames(self, force: bool = False) -> None:
        if not self.viewers:
            return
        if not force and not self.overview_visible():
            return
        tiles = await self.snapshot()
        for state in list(self.viewers.values()):
            if state.cols < 10 or state.rows < 5:
                continue
            frame, hits = render(tiles, state.cols, state.rows)
            state.hits = hits
            if frame == state.last_frame:
                continue
            state.last_frame = frame
            try:
                state.writer.write((json.dumps({"type": "frame", "data": frame}) + "\n").encode())
                await state.writer.drain()
            except (ConnectionError, RuntimeError):
                self.viewers.pop(state.writer, None)

    async def refresh_loop(self) -> None:
        while True:
            force = self.wake.is_set()
            self.wake.clear()
            try:
                await self.send_frames(force=force)
            except Exception as e:  # noqa: BLE001
                log(f"refresh failed: {e!r}")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.config["refresh_seconds"])
            except asyncio.TimeoutError:
                pass

    # -- shortcut ------------------------------------------------------------

    async def shortcut_loop(self) -> None:
        import iterm2

        try:
            parsed = parse_shortcut(self.config.get("shortcut"))
        except ValueError as e:
            log(f"Shortcut disabled: {e}")
            return
        if parsed is None:
            log("Built-in shortcut disabled; bind tabspace_toggle() in Settings > Keys.")
            return
        mods, keycode_name = parsed
        keycode = getattr(iterm2.Keycode, keycode_name, None)
        if keycode is None:
            log(f"Shortcut disabled: unknown key {keycode_name}")
            return
        wanted = {getattr(iterm2.Modifier, m) for m in mods}
        pattern = iterm2.KeystrokePattern()
        pattern.required_modifiers = list(wanted)
        pattern.forbidden_modifiers = [m for m in (iterm2.Modifier.COMMAND, iterm2.Modifier.OPTION,
                                                   iterm2.Modifier.CONTROL, iterm2.Modifier.SHIFT)
                                       if m not in wanted]
        pattern.keycodes = [keycode]
        relevant = {iterm2.Modifier.COMMAND, iterm2.Modifier.OPTION,
                    iterm2.Modifier.CONTROL, iterm2.Modifier.SHIFT}
        log(f"Shortcut {self.config['shortcut']} active")
        # The filter stops iTerm2 (and the focused program) from receiving the
        # shortcut; the monitor tells us it was pressed. Keystrokes are only
        # compared against the shortcut, never stored.
        async with iterm2.KeystrokeFilter(self.connection, [pattern]):
            async with iterm2.KeystrokeMonitor(self.connection) as monitor:
                while True:
                    ks = await monitor.async_get()
                    if ks.keycode == keycode and set(ks.modifiers) & relevant == wanted:
                        try:
                            await self.toggle()
                        except Exception as e:  # noqa: BLE001
                            log(f"toggle failed: {e!r}")


@dataclass
class ViewerState:
    writer: object
    cols: int = 80
    rows: int = 24
    session_id: str = ""
    hits: list = field(default_factory=list)
    last_frame: str | None = None


async def service_main(connection) -> None:
    import iterm2

    config = load_config()
    app = await iterm2.async_get_app(connection)
    service = Service(connection, app, config)
    await service.discover_overview_sessions()

    path = socket_path()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    old_umask = os.umask(0o077)
    try:
        server = await asyncio.start_unix_server(service.handle_viewer, path=path)
    finally:
        os.umask(old_umask)

    @iterm2.RPC
    async def tabspace_toggle():
        await service.toggle()

    await tabspace_toggle.async_register(connection)
    log(f"Tabspace {VERSION} running (socket {path})")
    async with server:
        await asyncio.gather(service.refresh_loop(), service.shortcut_loop())


# ---------------------------------------------------------------------------
# Viewer (runs in the Overview tab; standard library only).
# ---------------------------------------------------------------------------

ENTER_SCREEN = "\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1000h\x1b[?1006h\x1b[2J"
LEAVE_SCREEN = "\x1b[?1006l\x1b[?1000l\x1b[?7h\x1b[?25h\x1b[?1049l"


def parse_input(buf: bytes) -> tuple[list, bytes]:
    """Parse SGR mouse reports and plain keys. Returns (events, leftover)."""
    events = []
    i = 0
    while i < len(buf):
        if buf.startswith(b"\x1b[<", i):
            end = i + 3
            while end < len(buf) and buf[end] not in b"Mm":
                end += 1
            if end >= len(buf):
                break  # incomplete; wait for more bytes
            try:
                b, x, y = (int(p) for p in buf[i + 3:end].split(b";"))
                if buf[end:end + 1] == b"M" and b == 0:
                    events.append(("click", x - 1, y - 1))
            except ValueError:
                pass
            i = end + 1
        elif buf[i:i + 1] == b"\x1b":
            # Some other escape sequence (focus, arrows...): skip it.
            j = i + 1
            if j < len(buf) and buf[j:j + 1] in (b"[", b"O"):
                j += 1
                while j < len(buf) and not (0x40 <= buf[j] <= 0x7E):
                    j += 1
                j += 1
            i = j
        else:
            events.append(("key", chr(buf[i])))
            i += 1
    return events, buf[i:]


def viewer_main(sock: str) -> int:
    import signal
    import termios
    import tty

    fd_in, fd_out = sys.stdin.fileno(), sys.stdout.fileno()
    old_attrs = termios.tcgetattr(fd_in)
    session_id = os.environ.get("ITERM_SESSION_ID", "").split(":")[-1]

    def write(s: str) -> None:
        os.write(fd_out, s.encode("utf-8", "replace"))

    def size() -> tuple[int, int]:
        sz = os.get_terminal_size(fd_out)
        return sz.columns, sz.lines

    async def run() -> None:
        loop = asyncio.get_running_loop()
        quit_event = asyncio.Event()
        keys: asyncio.Queue = asyncio.Queue()
        pending = b""

        def on_stdin() -> None:
            nonlocal pending
            try:
                data = os.read(fd_in, 4096)
            except OSError:
                data = b""
            if not data:
                quit_event.set()
                return
            events, pending = parse_input(pending + data)
            for ev in events:
                keys.put_nowait(ev)

        loop.add_reader(fd_in, on_stdin)
        resized = asyncio.Event()
        loop.add_signal_handler(signal.SIGWINCH, resized.set)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, quit_event.set)

        while not quit_event.is_set():
            try:
                reader, writer = await asyncio.open_unix_connection(sock, limit=64 * 1024 * 1024)
            except OSError:
                cols, rows = size()
                write("\x1b[2J\x1b[H\x1b[90m Waiting for the Tabspace service… "
                      "(is tabspace.py running in iTerm2 > Scripts?)  q closes\x1b[0m")
                try:
                    ev = await asyncio.wait_for(keys.get(), timeout=1.0)
                    if ev == ("key", "q"):
                        return
                except asyncio.TimeoutError:
                    pass
                continue

            def send(msg: dict) -> None:
                writer.write((json.dumps(msg) + "\n").encode())

            cols, rows = size()
            send({"type": "hello", "session_id": session_id, "cols": cols, "rows": rows,
                  "version": VERSION})

            async def pump_frames() -> None:
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("type") == "frame":
                        write(msg.get("data", ""))

            async def pump_input() -> None:
                while True:
                    ev = await keys.get()
                    if ev[0] == "click":
                        send({"type": "click", "x": ev[1], "y": ev[2]})
                    elif ev == ("key", "q") or ev == ("key", "\x03"):
                        quit_event.set()
                        return
                    else:
                        send({"type": "key", "key": ev[1]})
                    await writer.drain()

            async def pump_resize() -> None:
                while True:
                    await resized.wait()
                    resized.clear()
                    c, r = size()
                    send({"type": "resize", "cols": c, "rows": r})
                    await writer.drain()

            tasks = [asyncio.ensure_future(t()) for t in (pump_frames, pump_input, pump_resize)]
            tasks.append(asyncio.ensure_future(quit_event.wait()))
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
            if quit_event.is_set():
                # Ask the service to close this Overview tab rather than exiting
                # on our own: works regardless of the profile's "when session
                # ends" setting, and avoids iTerm2's modal "session ended very
                # soon after starting" warning. Closing the tab sends SIGHUP,
                # which (with the default handler restored) ends this process.
                loop.remove_signal_handler(signal.SIGHUP)
                try:
                    send({"type": "close"})
                    await asyncio.wait_for(writer.drain(), timeout=1.0)
                    await asyncio.sleep(5)  # fallback if the tab wasn't closed
                except (OSError, asyncio.TimeoutError):
                    pass
            writer.close()
            if not quit_event.is_set():
                await asyncio.sleep(0.5)  # service went away; reconnect

    try:
        tty.setraw(fd_in)
        write(ENTER_SCREEN)
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        write(LEAVE_SCREEN)
        termios.tcsetattr(fd_in, termios.TCSADRAIN, old_attrs)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--viewer":
        return viewer_main(argv[2])
    if len(argv) >= 2 and argv[1] == "--version":
        print(VERSION)
        return 0
    import iterm2

    iterm2.run_forever(service_main)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
