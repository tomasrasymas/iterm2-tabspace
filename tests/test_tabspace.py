"""Unit tests for Tabspace's pure rendering/parsing logic (no iTerm2 needed).

Run: python3 -m unittest discover -s tests
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tabspace as ts  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def screen_from_frame(frame: str, width: int, height: int) -> list[str]:
    """Tiny terminal emulator: apply CUP-positioned writes to a char grid."""
    grid = [[" "] * width for _ in range(height)]
    x = y = 0
    i = 0
    while i < len(frame):
        m = ANSI.match(frame, i)
        if m:
            seq = m.group(0)
            if seq.endswith("H") and ";" in seq:
                row, col = seq[2:-1].split(";")
                y, x = int(row) - 1, int(col) - 1
            i = m.end()
            continue
        ch = frame[i]
        if 0 <= y < height and 0 <= x < width:
            grid[y][x] = ch
        x += ts.char_width(ch)
        i += 1
    return ["".join(r) for r in grid]


def pane(sid, cols, rows, text_lines):
    def lines(width, height):
        out = []
        for t in text_lines[-height:]:
            out.append([(t, "", len(t))])
        return out
    return ts.PaneNode(sid, cols, rows, lines)


class GridTests(unittest.TestCase):
    def test_grid_dims_six_tabs_wide_screen(self):
        cols, rows = ts.grid_dims(6, 240, 60)
        self.assertEqual(cols * rows >= 6, True)
        self.assertIn((cols, rows), [(3, 2), (2, 3)])

    def test_grid_dims_one(self):
        self.assertEqual(ts.grid_dims(1, 200, 50), (1, 1))

    def test_grid_dims_zero(self):
        self.assertEqual(ts.grid_dims(0, 200, 50), (0, 0))

    def test_split_lengths_proportional_with_divider(self):
        self.assertEqual(ts.split_lengths(21, [100, 100]), [10, 10])
        parts = ts.split_lengths(50, [30, 10, 10])
        self.assertEqual(sum(parts) + 2, 50)
        self.assertGreater(parts[0], parts[1])

    def test_split_lengths_too_small(self):
        self.assertEqual(ts.split_lengths(1, [5, 5]), [0, 0])

    def test_node_size(self):
        tree = ts.SplitNode(True, [ts.PaneNode("a", 100, 40),
                                   ts.SplitNode(False, [ts.PaneNode("b", 50, 20), ts.PaneNode("c", 50, 19)])])
        self.assertEqual(ts.node_size(tree), (151, 40))


class RenderTests(unittest.TestCase):
    def make_tiles(self):
        t1 = ts.Tile("tab1", "Work", ts.SplitNode(True, [
            pane("s1", 80, 24, ["left pane line 1", "left pane latest"]),
            pane("s2", 80, 24, ["right pane output"]),
        ]), busy=True)
        t2 = ts.Tile("tab2", "Agent 🤖 research", pane("s3", 120, 30, ["only pane"]))
        return [t1, t2]

    def test_render_layout_and_hits(self):
        frame, hits = ts.render(self.make_tiles(), 100, 20)
        screen = screen_from_frame(frame, 100, 20)
        self.assertIn("Tabspace · 2 tabs", screen[0])
        joined = "\n".join(screen)
        self.assertIn("1 Work", joined)
        self.assertIn("2 Agent", joined)
        self.assertIn("left pane latest", joined)
        self.assertIn("right pane output", joined)
        self.assertIn("only pane", joined)
        # Clicking a pane resolves to that session; clicking a border to the tab.
        tiles = ts.tile_hits(hits)
        self.assertEqual([t.tab_id for t in tiles], ["tab1", "tab2"])
        first = tiles[0]
        border = ts.hit_test(hits, first.x0, first.y0)
        self.assertEqual((border.tab_id, border.session_id), ("tab1", None))
        left = ts.hit_test(hits, first.x0 + 2, first.y0 + 2)
        self.assertEqual((left.tab_id, left.session_id), ("tab1", "s1"))
        right = ts.hit_test(hits, first.x1 - 3, first.y0 + 2)
        self.assertEqual((right.tab_id, right.session_id), ("tab1", "s2"))
        self.assertIsNone(ts.hit_test(hits, 0, 0))  # header row

    def test_nothing_drawn_outside_screen(self):
        frame, _ = ts.render(self.make_tiles(), 60, 12)
        for m in re.finditer(r"\x1b\[(\d+);(\d+)H", frame):
            self.assertLessEqual(int(m.group(1)), 12)
            self.assertLessEqual(int(m.group(2)), 60)

    def test_long_lines_are_cropped(self):
        tile = ts.Tile("t", "x", pane("s", 80, 24, ["A" * 500]))
        frame, _ = ts.render([tile], 40, 10)
        screen = screen_from_frame(frame, 40, 10)
        self.assertTrue(all(len(r) == 40 for r in screen))
        self.assertIn("A" * 38, screen[2])

    def test_empty_state(self):
        frame, hits = ts.render([], 80, 10)
        self.assertIn("No other tabs", "\n".join(screen_from_frame(frame, 80, 10)))
        self.assertEqual(hits, [])

    def test_many_tabs_small_window(self):
        tiles = [ts.Tile(f"t{i}", f"tab {i}", pane(f"s{i}", 80, 24, ["x"])) for i in range(30)]
        frame, hits = ts.render(tiles, 40, 8)
        self.assertIn("too small", "\n".join(screen_from_frame(frame, 40, 8)))


class TabColorTests(unittest.TestCase):
    def test_colored_tab_uses_its_color_for_border_and_title(self):
        tile = ts.Tile("t", "Agent", pane("s", 80, 24, ["x"]), busy=True, color=(200, 50, 10))
        frame, _ = ts.render([tile], 60, 12)
        self.assertIn("\x1b[0;38;2;200;50;10m┌", frame)      # border
        self.assertIn("\x1b[0;38;2;200;50;10;1m 1 Agent", frame)  # bold title in tab color
        self.assertIn(ts.BUSY_MARKER, frame)                 # activity marker
        self.assertNotIn("\x1b[0;32m┌", frame)              # no green border override

    def test_uncolored_tab_keeps_activity_border(self):
        idle = ts.Tile("a", "Idle", pane("s1", 80, 24, ["x"]))
        busy = ts.Tile("b", "Busy", pane("s2", 80, 24, ["x"]), busy=True)
        frame, _ = ts.render([idle, busy], 100, 12)
        self.assertIn("\x1b[0;90m┌", frame)
        self.assertIn("\x1b[0;32m┌", frame)
        screen = "\n".join(screen_from_frame(frame, 100, 12))
        self.assertIn("● ", screen)
        self.assertEqual(screen.count("●"), 1)


class DistinctColorTests(unittest.TestCase):
    def test_no_used_colors_picks_first_palette_color(self):
        self.assertEqual(ts.pick_distinct_color([]), ts.OVERVIEW_PALETTE[0])

    def test_avoids_used_colors(self):
        used = [ts.OVERVIEW_PALETTE[0], (230, 120, 20), (40, 200, 110)]
        pick = ts.pick_distinct_color(used)
        self.assertTrue(ts.color_is_distinct(pick, used))
        self.assertNotIn(pick, used)

    def test_similar_shade_counts_as_used(self):
        # A slightly different purple must still rule out the palette purple.
        pick = ts.pick_distinct_color([(150, 92, 178)])
        self.assertNotEqual(pick, ts.OVERVIEW_PALETTE[0])

    def test_every_palette_color_used_still_returns_a_palette_color(self):
        pick = ts.pick_distinct_color(list(ts.OVERVIEW_PALETTE))
        self.assertIn(pick, ts.OVERVIEW_PALETTE)

    def test_distance_symmetric_and_zero(self):
        a, b = (10, 20, 30), (200, 100, 0)
        self.assertEqual(ts.color_distance(a, a), 0)
        self.assertAlmostEqual(ts.color_distance(a, b), ts.color_distance(b, a))


class CropTests(unittest.TestCase):
    def test_crop_wide_chars(self):
        text, used = ts.crop_runs([("漢字テスト", "31", 10)], 5)
        self.assertEqual(used, 4)
        self.assertIn("漢字", text)

    def test_fit_adds_ellipsis(self):
        text, used = ts.fit("abcdefgh", 5)
        self.assertEqual((text, used), ("abcd…", 5))


class ColorTests(unittest.TestCase):
    class C:
        def __init__(self, standard=None, rgb=None):
            self.is_standard = standard is not None
            self.standard = standard
            self.is_rgb = rgb is not None
            self.rgb = type("RGB", (), dict(red=rgb[0], green=rgb[1], blue=rgb[2]))() if rgb else None

    def test_colors(self):
        self.assertEqual(ts.color_sgr(self.C(standard=1), True), "31")
        self.assertEqual(ts.color_sgr(self.C(standard=9), True), "91")
        self.assertEqual(ts.color_sgr(self.C(standard=200), False), "48;5;200")
        self.assertEqual(ts.color_sgr(self.C(rgb=(1, 2, 3)), True), "38;2;1;2;3")
        self.assertEqual(ts.color_sgr(None, True), "")


class InputTests(unittest.TestCase):
    def test_click_and_keys(self):
        events, rest = ts.parse_input(b"\x1b[<0;10;5M\x1b[<0;10;5m3q")
        self.assertEqual(events, [("click", 9, 4), ("key", "3"), ("key", "q")])
        self.assertEqual(rest, b"")

    def test_ignores_other_buttons_and_sequences(self):
        events, _ = ts.parse_input(b"\x1b[<64;1;1M\x1b[<2;3;3M\x1b[A\x1b[I")
        self.assertEqual(events, [])

    def test_incomplete_sequence_kept(self):
        events, rest = ts.parse_input(b"\x1b[<0;10")
        self.assertEqual(events, [])
        self.assertEqual(rest, b"\x1b[<0;10")


class ShortcutTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ts.parse_shortcut("cmd+opt+o"), ({"COMMAND", "OPTION"}, "ANSI_O"))
        self.assertEqual(ts.parse_shortcut("ctrl+shift+F5"), ({"CONTROL", "SHIFT"}, "F5"))
        self.assertIsNone(ts.parse_shortcut(None))
        self.assertIsNone(ts.parse_shortcut(""))
        with self.assertRaises(ValueError):
            ts.parse_shortcut("o")
        with self.assertRaises(ValueError):
            ts.parse_shortcut("hyper+o")


class LatestLinesTests(unittest.TestCase):
    class Line:
        def __init__(self, s):
            self.string = s

    class Contents:
        def __init__(self, lines, cursor_y, above=0):
            self._lines = lines
            self.number_of_lines = len(lines)
            self.number_of_lines_above_screen = above
            self.cursor_coord = type("P", (), dict(x=0, y=cursor_y + above))()

        def line(self, i):
            return LatestLinesTests.Line(self._lines[i])

    def test_trailing_blank_lines_skipped(self):
        c = self.Contents(["a", "b", "c", "", "", ""], cursor_y=2)
        self.assertEqual(list(ts.latest_line_range(c, 2)), [1, 2])

    def test_cursor_below_text_counts(self):
        c = self.Contents(["a", "", "", ""], cursor_y=3, above=100)
        self.assertEqual(list(ts.latest_line_range(c, 10)), [0, 1, 2, 3])


class ConfigTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"shortcut": "ctrl+opt+t", "refresh_seconds": 0.01, "bogus": 1}')
        try:
            cfg = ts.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(cfg["shortcut"], "ctrl+opt+t")
        self.assertEqual(cfg["refresh_seconds"], 0.25)
        self.assertNotIn("bogus", cfg)
        self.assertEqual(ts.load_config("/nonexistent/x.json"), ts.load_config("/nonexistent/y.json"))


if __name__ == "__main__":
    unittest.main()
