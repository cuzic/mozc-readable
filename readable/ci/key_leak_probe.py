"""CI experiment (ADR-267 D11): how do key_seq / key_eaten_seq / preedit / the document text relate when keys leak?

usage: key_leak_probe.py <tid_file>
Runs in-process like composition_probe.py (the TIP is loaded into this process; the document is a plain EDIT whose
text is read with GetWindowTextW). The block has 88 bytes here: key_eaten_seq at offset 80 is an experiment field
(branch ci/key-leak-probe only, not part of the spec).

Hypotheses (see docs/adr/267 D11 in awase):
  H1 key_seq advances for every key the TIP is called for, also with the IME closed and while the server is cold.
  H2 a key the TIP does not eat (eaten=FALSE) advances key_seq but not key_eaten_seq, and leaves one literal char.
  H3 a key TSF does not dispatch to the TIP advances neither (not forced here; seen as sent != dkey).
  H4 a cold state (first keys right after the IME turned on, before server_ready=1) loses/leaks keys. Only observed.
  H5 a pending romaji 'k' is in the preedit (preedit_utf16_len=1).
  H6 literal chars in the document = (key_seq delta) - (key_eaten_seq delta) while the IME is on.

Every scenario prints one `probe: scenario ...` line with sent/dkey/deaten/text/plen so the run log is the table.
`probe: SOFT <name> ok=...` lines are turned into warnings by verify.ps1 (nothing here is a HARD check).
"""
import ctypes
import ctypes.wintypes as wt
import faulthandler
import struct
import sys
import time

import composition_probe as cp
from composition_probe import u32, k32, pump, send_vk, i32, VK_F2, VK_KANJI, VK_ESCAPE

FMT = "<10IQ10I"  # 88 bytes
NAMES = ("magic version seq eo em to tm tid pid active tick phase cat plen pcur key_seq sfm srv cks eaten pad").split()
assert struct.calcsize(FMT) == 88 and len(NAMES) == 21

_view = None


def read(tid):
    global _view
    if _view is None:
        h = k32.OpenFileMappingW(4, False, f"Local\\mozc-readable-ime-{tid}")
        if not h:
            return None
        _view = k32.MapViewOfFile(h, 4, 0, 0, 0)
    if not _view:
        return None
    for _ in range(100):
        a = struct.unpack(FMT, ctypes.string_at(_view, 88))
        if a[2] % 2:
            continue
        if a[2] == struct.unpack("<I", ctypes.string_at(_view + 8, 4))[0]:
            return dict(zip(NAMES, a))
    return None


def doc_text(edit):
    n = u32.GetWindowTextLengthW(edit)
    buf = ctypes.create_unicode_buffer(n + 2)
    u32.GetWindowTextW(edit, buf, n + 2)
    return buf.value


def clear(p):
    """Drop any composition and empty the document."""
    p.focus()
    cp.send_vk(VK_ESCAPE)
    pump(0.3)
    u32.SetWindowTextW(p.edit, "")
    pump(0.2)


def scenario(p, name, vks, gap=0.03, settle=1.0, idle=0.0):
    """Type `vks` and report the deltas and the final document/preedit. Waits `idle` seconds first."""
    pump(idle)
    clear(p)
    s0 = read(p.tid)
    p.focus()
    t0 = time.time()
    for vk in vks:
        send_vk(vk, gap=gap)
    pump(settle)
    s1 = read(p.tid)
    text = doc_text(p.edit)
    dkey = i32(s1["key_seq"] - s0["key_seq"])
    deaten = i32(s1["eaten"] - s0["eaten"])
    print(f"probe: scenario {name}: sent={len(vks)} dkey={dkey} deaten={deaten} leak_est={dkey - deaten} "
          f"text={text!r} text_len={len(text)} plen={s1['plen']} phase={s1['phase']} eo={s1['eo']} srv={s1['srv']} "
          f"took={time.time() - t0:.2f}s", flush=True)
    return dict(sent=len(vks), dkey=dkey, deaten=deaten, text=text, plen=s1["plen"], phase=s1["phase"], s1=s1)


def result(name, ok, detail=""):
    print(f"probe: SOFT {name} ok={bool(ok)} {detail}", flush=True)


def main():
    faulthandler.enable()
    tid_file = sys.argv[1]
    wndproc = cp.WNDPROC(lambda h, m, w, l: u32.DefWindowProcW(h, m, w, l))
    wc = cp.WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.lpszClassName = "ReadableKeyLeakProbe"
    u32.RegisterClassW(ctypes.byref(wc))
    hwnd = u32.CreateWindowExW(0, "ReadableKeyLeakProbe", "readable-keyleak", 0x10CF0000, 100, 100, 600, 300,
                               None, None, wc.hInstance, None)
    edit = u32.CreateWindowExW(0x200, "EDIT", "", 0x50010000 | 0x0080, 10, 10, 560, 100, hwnd,
                               None, wc.hInstance, None)
    u32.GetWindowTextLengthW.argtypes = [wt.HWND]
    u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u32.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]
    u32.ShowWindow(hwnd, 5)
    u32.SetForegroundWindow(hwnd)
    u32.SetFocus(edit)
    tid = k32.GetCurrentThreadId()
    with open(tid_file, "w") as f:
        f.write(str(tid))
    p = cp.Probe(tid, hwnd, edit)
    p.st = lambda: read(tid)  # the 88 byte reader, used by Probe.wait
    pump(3)  # cold: the TIP loads, nothing warmed up
    s = read(tid)
    print(f"probe: info initial: {s}", flush=True)
    result("block is version 3 with the 88 byte experiment layout", s is not None and s["version"] == 3,
           f"version={s and s['version']}")
    if s is None:
        sys.exit(1)

    K, A, I = 0x4B, 0x41, 0x49

    scenario(p, "S0 IME closed (fresh), 'ka'", [K, A])
    send_vk(VK_F2)  # IME on, then type at once (cold: server_ready is 0 until the server answers)
    cold = scenario(p, "S1 cold: F2 then 'ka' at once", [K, A], gap=0.002, settle=1.5)
    pump(2.0)
    s = p.wait(lambda s: s["eo"] == 1, 8)
    print(f"probe: info before warm scenarios: eo={s and s['eo']} srv={s and s['srv']}", flush=True)
    scenario(p, "S2 warm, IME on, 'ka'", [K, A])
    w_k = scenario(p, "S3 warm, IME on, 'k' only (pending romaji)", [K])
    w_kaki = scenario(p, "S5 warm, IME on, 'kaki'", [K, A, K, I])
    clear(p)  # drop the composition left by 'kaki', otherwise Kanji does not close the IME (run 39122083119 S6)
    send_vk(VK_KANJI)  # IME off on purpose: every key is a literal
    p.wait(lambda s: s["eo"] == 0, 8)
    closed = scenario(p, "S6 IME closed (Kanji), 'kaki'", [K, A, K, I])
    send_vk(VK_F2)
    p.wait(lambda s: s["eo"] == 1, 8)
    scenario(p, "S7 IME on, idle 10 s, 'ka'", [K, A], idle=10.0)
    scenario(p, "S8 IME on, 'ka' with gap 0", [K, A], gap=0.0)

    result("H1 key_seq counts every key with the IME closed", closed["dkey"] == closed["sent"],
           f"sent={closed['sent']} dkey={closed['dkey']}")
    result("H1 key_seq counts every key in the cold scenario", cold["dkey"] == cold["sent"],
           f"sent={cold['sent']} dkey={cold['dkey']}")
    result("H1 key_seq counts every key when warm", w_kaki["dkey"] == w_kaki["sent"],
           f"sent={w_kaki['sent']} dkey={w_kaki['dkey']}")
    result("H2 closed IME: key_eaten_seq does not advance", closed["deaten"] == 0, f"deaten={closed['deaten']}")
    result("H2 closed IME: one literal per key", len(closed["text"]) == closed["sent"], f"text={closed['text']!r}")
    result("H5 pending 'k' is in the preedit", w_k["plen"] == 1, f"plen={w_k['plen']} phase={w_k['phase']}")
    result("H6 literal chars = dkey - deaten (closed IME)", len(closed["text"]) == closed["dkey"] - closed["deaten"],
           f"text_len={len(closed['text'])} dkey-deaten={closed['dkey'] - closed['deaten']}")
    result("H6 literal chars = dkey - deaten (cold)", len(cold["text"]) == cold["dkey"] - cold["deaten"],
           f"text_len={len(cold['text'])} dkey-deaten={cold['dkey'] - cold['deaten']}")
    p._stop = True


try:
    main()
except SystemExit:
    raise
except BaseException:
    import traceback
    traceback.print_exc()
    sys.exit(1)
