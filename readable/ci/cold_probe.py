"""CI experiment (ADR-267 D10 / the cold question): can a "cold" Mozc be reproduced on a GitHub Windows runner by waiting
(or by killing mozc_server, or by moving the focus away) before typing, in Chrome?

usage: cold_probe.py <window class> <case>
case:
  on:<N>    IME on and warm, no key for N seconds, then 'ka' at once (no F2 in between)
  off:<N>   IME turned off, idle N seconds, then F2 + 'ka' at once
  away:<N>  focus moved to the taskbar for N seconds, back to Chrome, then 'ka' at once (IME on)
  kill      mozc_server.exe killed, then 'ka' at once (IME on); then again with off -> kill -> F2 + 'ka'
Per scenario: `probe: scenario <case> <step>: ...` with the time from the first key to the first text in the page
(first_ms), the literal leak (ascii k/a in the page), key_seq/eaten deltas and srv (server_ready). A baseline warm
scenario (W0) runs first so a harness problem (focus lost, Chrome ignoring keys) shows up as W0 failing, not as cold.
`probe: SOFT cold <case> <step> ok=..` lines become warnings. Nothing here is a HARD check.
"""
import subprocess
import sys
import time

import chrome_leak_probe as L

u32, K, A_ = L.u32, L.K, L.A_


def fresh(c):
    """Per-step: clear the input and read the counters; returns the state before."""
    c.clear()
    return c.st()


def typed(c, case, step, s0, t0, fg_ok):
    """Poll the page for up to 5 s after the first key; log timing, leak and counters."""
    first_ms = None
    val = None
    end = time.time() + 5.0
    while time.time() < end:
        val = L.title_value(c.hwnd)
        if val:
            first_ms = int((time.time() - t0) * 1000)
            break
        time.sleep(0.01)
    time.sleep(1.0)
    val = L.title_value(c.hwnd)
    s1 = c.st()
    dkey = L.i32(s1["key_seq"] - s0["key_seq"])
    deaten = L.i32(s1["eaten"] - s0["eaten"])
    lit = bool(val) and any(ch in val for ch in "kaKA")
    print(f"probe: scenario {case} {step}: first_ms={first_ms} dkey={dkey} deaten={deaten} leak_est={dkey - deaten} "
          f"val={ascii(val)} literal={lit} plen={s1['plen']} eo={s0['eo']}->{s1['eo']} srv={s0['srv']}->{s1['srv']} "
          f"fg_ok={fg_ok}", flush=True)
    ok = fg_ok and bool(val) and not lit and (first_ms is not None and first_ms < 1000)
    L.result(f"cold {case} {step}: first text within 1 s and no literal", ok, f"first_ms={first_ms} val={ascii(val)} literal={lit}")
    return ok


def send_ka(gap=0.002):
    for vk in (K, A_):
        L.send_vk(vk, gap=gap)


def step_on(c, case, step, wait_s):
    s0 = fresh(c)
    if wait_s:
        print(f"probe: info {case} {step}: idle {wait_s}s, IME on", flush=True)
        time.sleep(wait_s)
    fg = c.fg_ok()
    t0 = time.time()
    send_ka()
    return typed(c, case, step, s0, t0, fg)


def step_off(c, case, step, wait_s):
    c.turn_off()
    s0 = fresh(c)
    print(f"probe: info {case} {step}: idle {wait_s}s, IME off", flush=True)
    time.sleep(wait_s)
    fg = c.fg_ok()
    t0 = time.time()
    L.send_vk(L.VK_F2, gap=0.002)
    send_ka()
    return typed(c, case, step, s0, t0, fg)


def step_away(c, case, step, wait_s):
    s0 = fresh(c)
    tray = None
    for cls in ("Shell_TrayWnd",):
        h = L.find_window(cls)
        if h:
            tray = h
    if tray:
        L.force_foreground(tray)
    print(f"probe: info {case} {step}: focus on the taskbar ({tray}) for {wait_s}s", flush=True)
    time.sleep(wait_s)
    L.force_foreground(c.hwnd)
    L.click_page(c.hwnd)
    t0 = time.time()
    fg = c.fg_ok()
    send_ka()
    return typed(c, case, step, s0, t0, fg)


def kill_server():
    r = subprocess.run(["taskkill", "/F", "/IM", "mozc_server.exe"], capture_output=True, text=True)
    print(f"probe: info taskkill mozc_server: rc={r.returncode} {r.stdout.strip()[:80]} {r.stderr.strip()[:80]}", flush=True)


def main():
    cls, case = sys.argv[1], sys.argv[2]
    kind, _, arg = case.partition(":")
    hwnd = L.wait_for(lambda: L.find_window(cls), 30)
    if not hwnd:
        print("probe: info no Chrome window", flush=True)
        sys.exit(2)
    for _ in range(10):
        L.force_foreground(hwnd)
        if u32.GetForegroundWindow() == hwnd:
            break
        time.sleep(0.5)
    tid, pid = L.tid_pid(hwnd)
    s = L.wait_for(lambda: (L.click_page(hwnd), L.read(tid))[1], 30)
    if s is None:
        print("probe: info no shared block for the Chrome thread", flush=True)
        sys.exit(3)
    time.sleep(1.0)
    L.click_page(hwnd)
    c = L.Ctx(hwnd, tid)
    print(f"probe: info chrome tid={tid} pid={pid} initial eo={s['eo']} srv={s['srv']} version={s['version']}", flush=True)
    if not c.turn_on("setup"):
        print("probe: info could not turn the IME on", flush=True)
        sys.exit(4)
    step_on(c, case, "W0 warm baseline", 0)
    n = int(arg) if arg else 0
    if kind == "on":
        step_on(c, case, f"C1 idle {n}s", n)
        step_on(c, case, "C2 right after", 0)
    elif kind == "off":
        step_off(c, case, f"C1 idle {n}s", n)
        step_on(c, case, "C2 right after", 0)
    elif kind == "away":
        step_away(c, case, f"C1 away {n}s", n)
        step_on(c, case, "C2 right after", 0)
    elif kind == "kill":
        kill_server()
        step_on(c, case, "C1 after kill, IME on", 0)
        c.turn_off()
        kill_server()
        s0 = fresh(c)
        t0, fg = time.time(), c.fg_ok()
        L.send_vk(L.VK_F2, gap=0.002)
        send_ka()
        typed(c, case, "C2 off, kill, F2 + ka", s0, t0, fg)
    else:
        print(f"probe: info unknown case {case}", flush=True)
        sys.exit(5)


main()
