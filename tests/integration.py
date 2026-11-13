"""Live integration test for Tabspace. Requires iTerm2 with the Python API on
and the Tabspace service running.

It creates its OWN test window with fake agents (tests/agent_sim.zsh) in
multiple tabs/split panes, exercises the overview, and closes the window at the
end. It never touches other windows' sessions (it only reads the overview).

Run: python tests/integration.py
"""
import asyncio
import json
import os
import shlex
import sys
import tempfile

import iterm2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import tabspace as ts  # noqa: E402
from test_tabspace import screen_from_frame  # noqa: E402

INPUT_LOG = os.path.join(tempfile.gettempdir(), "tabspace-test-input.log")
SIM = os.path.join(HERE, "agent_sim.zsh")
results = []


def check(name, ok, detail=""):
    import time as _t
    print(_t.strftime("%H:%M:%S"), end=" ")
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))


def agent_cmd(name):
    return f"/usr/bin/env TABSPACE_INPUT_LOG={shlex.quote(INPUT_LOG)} /bin/zsh {shlex.quote(SIM)} {name}"


def agent_profile(name):
    p = iterm2.LocalWriteOnlyProfile()
    p.set_use_custom_command("Yes")
    p.set_command(agent_cmd(name))
    return p


async def overview_text(app):
    for w in app.windows:
        for t in w.tabs:
            for s in t.sessions:
                if await s.async_get_variable(ts.ROLE_VARIABLE) == ts.ROLE_OVERVIEW:
                    sc = await s.async_get_screen_contents()
                    return s, "\n".join(sc.line(i).string for i in range(sc.number_of_lines))
    return None, ""


async def wait_for_text(app, predicate, timeout=6.0):
    """Poll the overview until predicate(text) is true (or timeout)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    text = ""
    while loop.time() < end:
        _, text = await overview_text(app)
        if predicate(text):
            return True, text
        await asyncio.sleep(0.25)
    return False, text


async def overview_sessions(app):
    found = []
    for w in app.windows:
        for t in w.tabs:
            for s in t.sessions:
                if await s.async_get_variable(ts.ROLE_VARIABLE) == ts.ROLE_OVERVIEW:
                    found.append(s.session_id)
    return found


async def last_step(session):
    sc = await session.async_get_screen_contents()
    for i in range(sc.number_of_lines - 1, -1, -1):
        s = sc.line(i).string
        if "step" in s:
            return int(s.rsplit(" ", 1)[-1])
    return 0


async def main(connection):
    if os.path.exists(INPUT_LOG):
        os.unlink(INPUT_LOG)
    app = await iterm2.async_get_app(connection)
    before = set(await overview_sessions(app))
    if before:
        print("An Overview tab is already open. Close it (press q in it) before running the "
              "integration test, so the test never touches your own tabs.")
        sys.exit(2)

    # --- build the test window: 3 tabs, 1/2/3 panes -------------------------
    win = await iterm2.Window.async_create(connection, command=agent_cmd("Alpha1"))
    alpha = win.current_tab
    await alpha.current_session.async_split_pane(vertical=True, profile_customizations=agent_profile("Alpha2"))
    beta = await win.async_create_tab(command=agent_cmd("Beta1"))
    b2 = await beta.current_session.async_split_pane(vertical=True, profile_customizations=agent_profile("Beta2"))
    await b2.async_split_pane(vertical=False, profile_customizations=agent_profile("Beta3"))
    gamma = await win.async_create_tab(command=agent_cmd("Gamma1"))
    for tab, title in ((alpha, "T-Alpha"), (beta, "T-Beta"), (gamma, "T-Gamma")):
        await tab.async_set_title(title)
    agents = [s for t in (alpha, beta, gamma) for s in t.sessions]
    check("test window has 3 tabs / 6 panes", len(win.tabs) == 3 and len(agents) == 6)
    await alpha.async_activate()
    await asyncio.sleep(1.5)
    steps_before = [await last_step(s) for s in agents]

    try:
        # --- open overview via the script function ---------------------------
        await iterm2.async_invoke_function(connection, "tabspace_toggle()")
        await asyncio.sleep(3)
        ov, text = await overview_text(app)
        check("overview tab created", ov is not None)
        new_ov = set(await overview_sessions(app)) - before
        check("exactly one new overview session", len(new_ov) == 1, str(new_ov))
        win_now = app.get_window_by_id(win.window_id)
        check("overview created in the focused (test) window",
              ov is not None and any(ov.session_id == s.session_id for t in win_now.tabs for s in t.sessions))
        check("overview is selected", win_now.current_tab.current_session.session_id == (ov.session_id if ov else None))
        for title in ("T-Alpha", "T-Beta", "T-Gamma"):
            check(f"tile shown: {title}", title in text)
        check("overview excludes itself", f" {ts.OVERVIEW_TITLE} " not in text.split("\n", 1)[-1])
        ok, text = await wait_for_text(app, lambda t: "Alpha2" in t and "Beta3" in t)
        check("pane output visible (Alpha2, Beta3)", ok,
              " | ".join(l.strip()[:60] for l in text.splitlines() if "Alpha" in l or "Beta" in l)[:600])
        check("header present", "Tabspace ·" in text)

        # --- tab color: border/title of a colored tab use that color --------------
        tab_rgb = (230, 120, 20)
        lp = iterm2.LocalWriteOnlyProfile()
        col = iterm2.Color(*tab_rgb)
        for setter in (lp.set_tab_color, lp.set_tab_color_light, lp.set_tab_color_dark):
            setter(col)
        for setter in (lp.set_use_tab_color, lp.set_use_tab_color_light, lp.set_use_tab_color_dark):
            setter(True)
        for sess in alpha.sessions:
            await sess.async_set_profile_properties(lp)
        found_rgb = None
        for _ in range(16):
            await asyncio.sleep(0.5)
            sc = await ov.async_get_screen_contents()
            for i in range(sc.number_of_lines):
                line = sc.line(i)
                x = line.string.find("T-Alpha")
                if x >= 0:
                    st = line.style_at(x)
                    if st and st.fg_color and st.fg_color.is_rgb:
                        rgb = st.fg_color.rgb
                        found_rgb = (rgb.red, rgb.green, rgb.blue)
                    break
            if found_rgb == tab_rgb:
                break
        check("colored tab's title/border use its tab color", found_rgb == tab_rgb, str(found_rgb))

        # --- live updates -------------------------------------------------------
        await asyncio.sleep(2.5)
        _, text2 = await overview_text(app)
        check("previews update over time", text2 != text)

        # --- rename / open / close / reorder -------------------------------------
        await gamma.async_set_title("T-Gamma-Renamed")
        delta = await win.async_create_tab(command=agent_cmd("Delta1"), select=False)
        await delta.async_set_title("T-Delta")
        ok, text = await wait_for_text(app, lambda t: "T-Gamma-Renamed" in t and "T-Delta" in t)
        check("rename reflected", "T-Gamma-Renamed" in text)
        check("new tab appears", "T-Delta" in text)
        check("overview still selected after background tab open",
              app.get_window_by_id(win.window_id).current_tab.current_session.session_id == ov.session_id)

        await beta.async_close(force=True)
        ok, text = await wait_for_text(app, lambda t: "T-Beta" not in t)
        check("closed tab disappears", ok)

        w = app.get_window_by_id(win.window_id)
        order = [t for t in w.tabs if t.tab_id != alpha.tab_id]
        order.insert(len(order) - 1, app.get_tab_by_id(alpha.tab_id))  # move Alpha just before overview
        await w.async_set_tabs(order)
        await asyncio.sleep(2.5)
        _, text = await overview_text(app)
        i_g, i_a = text.find("T-Gamma-Renamed"), text.find("T-Alpha")
        check("reorder reflected (Gamma before Alpha now)", 0 <= i_g < i_a or (i_g >= 0 and i_a >= 0 and
              text[:i_a].count("\n") > text[:i_g].count("\n")), f"g={i_g} a={i_a}")

        # --- clicking: a second (fake) viewer clicks a pane -----------------------
        reader, writer = await asyncio.open_unix_connection(ts.socket_path(), limit=64 * 1024 * 1024)
        writer.write((json.dumps({"type": "hello", "cols": 160, "rows": 45}) + "\n").encode())
        await writer.drain()
        frame = json.loads(await asyncio.wait_for(reader.readline(), 5))["data"]
        screen = screen_from_frame(frame, 160, 45)
        target = None
        for y, row in enumerate(screen):
            x = row.find("T-Delta")
            if x >= 0:
                target = (x + 2, y + 3)  # inside the tile, below the title
                break
        check("fake viewer received a frame with T-Delta", target is not None)
        if target:
            writer.write((json.dumps({"type": "click", "x": target[0], "y": target[1]}) + "\n").encode())
            await writer.drain()
            await asyncio.sleep(1.0)
            cur = app.get_window_by_id(win.window_id).current_tab
            check("click switched to the clicked tab", cur.tab_id == delta.tab_id)

        # --- shortcut again from another tab: reuses the same overview -------------
        await iterm2.async_invoke_function(connection, "tabspace_toggle()")
        await asyncio.sleep(1.5)
        cur = app.get_window_by_id(win.window_id).current_tab
        check("toggle reuses existing overview", cur.current_session.session_id == ov.session_id)
        check("still exactly one overview", len(set(await overview_sessions(app)) - before) == 1)

        # --- toggle while on overview returns to previous tab -----------------------
        await iterm2.async_invoke_function(connection, "tabspace_toggle()")
        await asyncio.sleep(1.0)
        cur = app.get_window_by_id(win.window_id).current_tab
        check("toggle on overview returns to previous tab", cur.tab_id == delta.tab_id)

        # --- keyboard 1-9 navigation via fake viewer (press the T-Gamma tile's number)
        await ov.async_activate()
        await asyncio.sleep(1.5)
        _, text = await overview_text(app)
        import re as _re
        m = _re.search(r"(\d) (?:W\d+ · )?T-Gamma-Renamed", text)
        check("tile number shown for T-Gamma", m is not None)
        if m:
            writer.write((json.dumps({"type": "key", "key": m.group(1)}) + "\n").encode())
            await writer.drain()
            await asyncio.sleep(1.0)
            cur = app.get_window_by_id(win.window_id).current_tab
            check(f"key {m.group(1)} switches to T-Gamma", cur.tab_id == gamma.tab_id)
        await ov.async_activate()
        await asyncio.sleep(2.0)
        shot = os.environ.get("TABSPACE_SHOT")
        if shot:
            with open(shot, "wb") as f:
                f.write(await ov.async_screenshot())
        writer.close()

        # --- agents never interrupted and never received input -------------------------
        live = [s for s in agents if app.get_session_by_id(s.session_id) is not None]
        steps_after = [await last_step(s) for s in live]
        before_live = [b for s, b in zip(agents, steps_before) if app.get_session_by_id(s.session_id)]
        check("all surviving agents kept running", all(a > b for a, b in zip(steps_after, before_live)),
              f"{before_live} -> {steps_after}")
        got = open(INPUT_LOG).read() if os.path.exists(INPUT_LOG) else ""
        check("no agent received any input", got == "", got[:200])
    finally:
        await win.async_close(force=True)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        sys.exit(1)


iterm2.run_until_complete(main)
