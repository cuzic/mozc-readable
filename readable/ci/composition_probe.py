"""CI helper: exercises the version 3 fields of the shared memory (ADR-267, stage 1) from inside a window.

usage: composition_probe.py <tid_file>
The TIP is loaded into this process (a plain window with EDIT controls, like ui_host.py), so the block
Local\\mozc-readable-ime-<tid> belongs to this thread. Keys are sent with SendInput; the block is read back
from the same process (a different process reads it in reader.py / drive_foreground.py).

Every result is one line: `probe: HARD|SOFT <name> ok=True|False <detail>`; verify.ps1 turns HARD failures into
check failures and SOFT ones into warnings. Lines starting with `probe: info` are observations only (what appears,
which the ADR asks to find out: Tab/arrow behaviour is NOT exercised here).
With MOZC_CI_DEACTIVATE=1 the layout is switched at the end (global to the session: run this in its own job).
"""
import ctypes
import ctypes.wintypes as wt
import faulthandler
import os
import struct
import sys
import threading
import time

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
u32.CreateWindowExW.restype = wt.HWND
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.SetFocus.argtypes = [wt.HWND]
u32.SetFocus.restype = wt.HWND
u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
u32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.DefWindowProcW.restype = ctypes.c_ssize_t
k32.OpenFileMappingW.restype = wt.HANDLE
k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
k32.OpenEventW.restype = wt.HANDLE
k32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

FMT = "<10IQ16I"  # TipSharedState version 4, 112 bytes (offsets 0..79 are version 3)
NAMES = ("magic version seq eo em to tm tid pid active tick phase cat plen pcur key_seq sfm srv cks "
         "csc sub_ee sub_term revert sub_other esf flags ctx").split()
assert struct.calcsize(FMT) == 112 and len(NAMES) == 27
V4_COUNTERS = ("csc", "sub_ee", "sub_term", "revert", "sub_other", "esf")

VK_F2, VK_KANJI, VK_SPACE, VK_RETURN, VK_BACK, VK_ESCAPE = 0xF2, 0x19, 0x20, 0x0D, 0x08, 0x1B


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def send_vk(vk, gap=0.03):
    scan = u32.MapVirtualKeyW(vk, 0)
    for flags in (0, 2):
        i = INPUT(type=1)
        i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        u32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
        time.sleep(gap)


def pump(seconds):
    end = time.time() + seconds
    msg = wt.MSG()
    while time.time() < end:
        while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.003)


def i32(x):
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


_view = None
_view_size = 0


def read(tid):
    """A consistent snapshot of this thread's block as a dict, or None. Maps the whole section (size 0)."""
    global _view, _view_size
    if _view is None:
        h = k32.OpenFileMappingW(4, False, f"Local\\mozc-readable-ime-{tid}")
        if not h:
            return None
        _view = k32.MapViewOfFile(h, 4, 0, 0, 0)
    if not _view:
        return None
    for _ in range(100):
        a = struct.unpack(FMT, ctypes.string_at(_view, 112))
        if a[2] % 2:
            continue
        if a[2] == struct.unpack("<I", ctypes.string_at(_view + 8, 4))[0]:
            d = dict(zip(NAMES, a))
            d["_v3"] = d["version"] >= 3
            return d
    return None


class Probe:
    def __init__(self, tid, hwnd, edit):
        self.tid, self.hwnd, self.edit = tid, hwnd, edit
        self.pulses = 0
        self._stop = False

    def start_pulse_counter(self):
        ev = k32.OpenEventW(0x00100000, False, "Local\\mozc-readable-ime-changed")
        if not ev:
            print("probe: info change event not found", flush=True)
            return

        def run():
            while not self._stop:
                if k32.WaitForSingleObject(ev, 20) == 0:
                    self.pulses += 1
        threading.Thread(target=run, daemon=True).start()

    def focus(self, edit=None):
        u32.SetForegroundWindow(self.hwnd)
        u32.SetFocus(edit or self.edit)

    def st(self):
        return read(self.tid)

    def wait(self, pred, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            s = self.st()
            if s is not None and pred(s):
                return s
            pump(0.01)
        return self.st()

    def press(self, vk, label=None, timeout=5.0):
        """Send a key and wait until key_seq moved. Returns (state_after, key_seq_delta)."""
        before = self.st()
        k0 = before["key_seq"] if before else 0
        self.focus()
        send_vk(vk)
        s = self.wait(lambda s: i32(s["key_seq"] - k0) > 0, timeout)
        pump(0.05)
        delta = i32(s["key_seq"] - k0) if s else None
        return s, delta

    def settle(self, k_after, cond, timeout=5.0):
        """Wait until the composition fields reflect key `k_after` (composition_key_seq >= it) and `cond` holds."""
        return self.wait(lambda s: i32(s["cks"] - k_after) >= 0 and cond(s), timeout)


def result(kind, name, ok, detail=""):
    print(f"probe: {kind} {name} ok={bool(ok)} {detail}", flush=True)


def fmt(s):
    if s is None:
        return "None"
    return " ".join(f"{k}={s[k]}" for k in ("version", "active", "eo", "em", "phase", "cat", "plen", "pcur",
                                              "key_seq", "sfm", "srv", "cks"))


def main():
    faulthandler.enable()
    tid_file = sys.argv[1]
    wndproc = WNDPROC(lambda h, m, w, l: u32.DefWindowProcW(h, m, w, l))
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.lpszClassName = "ReadableCiProbe"
    u32.RegisterClassW(ctypes.byref(wc))
    hwnd = u32.CreateWindowExW(0, "ReadableCiProbe", "readable-probe", 0x10CF0000, 100, 100, 600, 400,
                               None, None, wc.hInstance, None)
    edit = u32.CreateWindowExW(0x200, "EDIT", "", 0x50010000 | 0x0080, 10, 10, 560, 100, hwnd,
                               None, wc.hInstance, None)
    pw = u32.CreateWindowExW(0x200, "EDIT", "", 0x50010000 | 0x0080 | 0x0020, 10, 120, 560, 40, hwnd,  # ES_PASSWORD
                             None, wc.hInstance, None)
    num = u32.CreateWindowExW(0x200, "EDIT", "", 0x50010000 | 0x0080 | 0x2000, 10, 170, 560, 40, hwnd,  # ES_NUMBER
                              None, wc.hInstance, None)
    u32.ShowWindow(hwnd, 5)
    u32.SetForegroundWindow(hwnd)
    u32.SetFocus(edit)
    tid = k32.GetCurrentThreadId()
    with open(tid_file, "w") as f:
        f.write(str(tid))
    print(f"probe: info tid={tid} hwnd={(hwnd or 0):#x}", flush=True)
    p = Probe(tid, hwnd, edit)
    p.start_pulse_counter()
    pump(3)  # the TIP loads and publishes its initial state (cold on a fresh runner: no warm-up before this)

    s = p.st()
    print(f"probe: info initial {fmt(s)}", flush=True)
    result("HARD", "block is version 4 (112 bytes) from this thread", s is not None and s["version"] == 4, fmt(s))
    if s is None or s["version"] < 3:
        sys.exit(1)

    # ---- server_ready (D10) and the first key: nothing is asserted about the key being lost, only logged.
    print(f"probe: info server_ready before the first key = {s['srv']} active={s['active']}", flush=True)
    result("SOFT", "server_ready is 0 before any key was sent to the server", s["srv"] == 0, f"srv={s['srv']}")
    t_first = time.time()
    on_ok = False
    for attempt in range(1, 4):
        s, delta = p.press(VK_F2)
        srv_after_key = s["srv"] if s else None
        s = p.wait(lambda s: s["eo"] == 1, 8)
        on_ok = bool(s and s["eo"] == 1)
        print(f"probe: info IME-on try={attempt} t={time.time() - t_first:.2f}s key_seq_delta={delta} "
              f"srv_right_after_key={srv_after_key} {fmt(s)}", flush=True)
        if on_ok:
            break
    result("HARD", "IME turned on (open=1)", on_ok, fmt(s))
    result("HARD", "active=1 while the TIP is live", s and s["active"] == 1, fmt(s))
    result("SOFT", "server_ready is 1 after the server answered a key", s and s["srv"] == 1, f"srv={s['srv'] if s else None}")
    if not on_ok:
        sys.exit(1)

    # ---- key_seq: advances for a key that changes nothing (F2 again: hiragana is idempotent), without a pulse.
    pump(0.3)
    pulses0 = p.pulses
    s, delta = p.press(VK_F2)
    pump(0.3)
    result("HARD", "key_seq advances by exactly 1 for a key-down that changes no state (F2 again)", delta == 1,
           f"delta={delta} pulses_during={p.pulses - pulses0}")
    result("SOFT", "no pulse for a key that changes no state", p.pulses - pulses0 == 0, f"pulses={p.pulses - pulses0}")

    # ---- phase / candidate_category / preedit: k -> ka -> Space -> Enter
    c0 = p.st()
    s, d = p.press(0x4B)  # K
    k_after = s["key_seq"]
    s = p.settle(k_after, lambda s: s["phase"] == 1)
    result("HARD", "phase=1 right after 'k' (composing)", s and s["phase"] == 1, f"delta={d} {fmt(s)}")
    result("HARD", "composition_start_count +1 when 'k' starts a composition (version 4)",
           s and i32(s["csc"] - c0["csc"]) == 1, f"before={c0['csc']} after={s['csc'] if s else None}")
    print(f"probe: info after k: plen={s['plen'] if s else None} pcur={s['pcur'] if s else None} cat={s['cat'] if s else None}", flush=True)
    result("SOFT", "preedit_utf16_len=1 and cursor=1 after 'k'", s and s["plen"] == 1 and s["pcur"] == 1, fmt(s))

    s, d = p.press(0x41)  # A -> か
    k_after = s["key_seq"]
    s = p.settle(k_after, lambda s: s["phase"] == 1 and s["plen"] == 1)
    result("SOFT", "after 'ka': phase=1, preedit_utf16_len=1 (one kana), cursor=1", s and s["phase"] == 1 and s["plen"] == 1 and s["pcur"] == 1, fmt(s))
    result("SOFT", "after 'ka': candidate_category is 0 or SUGGESTION(1)", s and s["cat"] in (0, 1), fmt(s))

    pulses0 = p.pulses
    s, d = p.press(VK_SPACE)
    k_after = s["key_seq"]
    s = p.settle(k_after, lambda s: s["phase"] == 2)
    result("HARD", "phase=2 after Space (converting)", s and s["phase"] == 2, f"delta={d} {fmt(s)}")
    print(f"probe: info candidate_category after the 1st Space: {s['cat'] if s else None}", flush=True)
    for n in (2, 3):  # the candidate list may appear only on a later Space press: observe
        if s and s["cat"] == 3:
            break
        s, d = p.press(VK_SPACE)
        s = p.settle(s["key_seq"], lambda s: True)
        print(f"probe: info candidate_category after Space #{n}: {s['cat'] if s else None} phase={s['phase'] if s else None}", flush=True)
    result("HARD", "composition_start_count did not move for 'a'/Space (the composition was already running)",
           s and i32(s["csc"] - c0["csc"]) == 1, f"before={c0['csc']} now={s['csc'] if s else None}")
    result("SOFT", "candidate_category=CONVERSION(3) within 3 Space presses", s and s["cat"] == 3, fmt(s))

    s, d = p.press(VK_BACK)  # BackSpace while converting: observation (R2-M3)
    k_after = s["key_seq"]
    s = p.settle(k_after, lambda s: True)
    print(f"probe: info BackSpace while converting: delta={d} {fmt(s)}", flush=True)

    s, d = p.press(VK_RETURN)
    k_after = s["key_seq"]
    s = p.settle(k_after, lambda s: s["phase"] == 0)
    result("HARD", "phase=0 and preedit 0 after Enter", s and s["phase"] == 0 and s["plen"] == 0, fmt(s))
    result("SOFT", "candidate_category=0 after Enter", s and s["cat"] == 0, fmt(s))
    print(f"probe: info pulses over k,a,Space,BS,Enter = {p.pulses - pulses0}", flush=True)

    # ---- fast typing: invariants of composition_key_seq (R3-I1). Sampled every ~2 ms while "kakikukeko" is typed.
    s = p.st()
    k0 = s["key_seq"]
    pulses0 = p.pulses
    violations = []
    samples = 0
    changes = 0
    last = None
    p.focus()
    letters = [0x4B, 0x41, 0x4B, 0x49, 0x4B, 0x55, 0x4B, 0x45, 0x4B, 0x4F]  # ka ki ku ke ko
    typed = 0
    for vk in letters:
        send_vk(vk, gap=0.002)
        typed += 1
        for _ in range(3):
            pump(0.002)
            s = p.st()
            if not s:
                continue
            samples += 1
            if i32(s["cks"] - s["key_seq"]) > 0:
                violations.append(("cks ahead of key_seq", fmt(s)))
            if s["plen"] > typed:
                violations.append(("plen more than the keys typed", fmt(s)))
            key = (s["phase"], s["cat"])
            if key != last:
                changes += 1
                last = key
    s = p.settle(p.st()["key_seq"], lambda s: s["plen"] == 5, 5)
    result("SOFT", "fast typing: composition_key_seq never ahead of key_seq, preedit never longer than keys typed",
           not violations, f"samples={samples} violations={violations[:3]}")
    result("SOFT", "fast typing: key_seq moved by the number of letters typed", s and i32(s["key_seq"] - k0) == len(letters),
           f"typed={len(letters)} delta={i32(s['key_seq'] - k0) if s else None} (duplicates/misses are the finding)")
    print(f"probe: info fast typing: pulses={p.pulses - pulses0} (phase,cat) changes seen by sampling={changes} final {fmt(s)}",
          flush=True)
    result("SOFT", "fast typing: fewer pulses than keys (preedit/key_seq changes do not pulse)", p.pulses - pulses0 < len(letters),
           f"pulses={p.pulses - pulses0} keys={len(letters)}")
    # Context change in the middle of a composition: focus the other EDIT and look (D9, observation).
    p.focus(num)
    pump(1.0)
    print(f"probe: info after moving focus to another EDIT mid-composition: {fmt(p.st())}", flush=True)
    p.focus(edit)
    pump(0.5)
    s, d = p.press(VK_ESCAPE)
    s = p.settle(s["key_seq"], lambda s: s["phase"] == 0, 5)
    print(f"probe: info after Esc: {fmt(s)}", flush=True)
    s, d = p.press(VK_ESCAPE)
    s = p.settle(s["key_seq"], lambda s: s["phase"] == 0, 5)
    result("SOFT", "back to phase=0 after Esc", s and s["phase"] == 0, fmt(s))

    # ---- version 4 diagnostics: a plain EDIT never submits by itself, never fails an edit session.
    s = p.st()
    print(f"probe: info version 4 counters: { {k: s[k] for k in V4_COUNTERS} } flags={s['flags']:#x} ctx={s['ctx']:#x}", flush=True)
    result("SOFT", "no SUBMIT/REVERT was requested and no edit session failed in a plain EDIT (counters 0)",
           s["sub_ee"] == 0 and s["sub_term"] == 0 and s["revert"] == 0 and s["esf"] == 0,
           " ".join(f"{k}={s[k]}" for k in V4_COUNTERS))
    result("SOFT", "update_context_id is set and last_output_is_test / use_async_lock flags are 0",
           s["ctx"] != 0 and s["flags"] & 3 == 0, f"flags={s['flags']:#x} ctx={s['ctx']:#x}")
    result("HARD", "the version 4 counters never went backwards since the start of the composition checks",
           all(i32(s[k] - c0[k]) >= 0 for k in V4_COUNTERS), " ".join(f"{k}:{c0[k]}->{s[k]}" for k in V4_COUNTERS))

    # ---- scope_forced_mode (D8): observation of what the EDIT input scopes produce.
    print(f"probe: info scope_forced_mode in the plain EDIT: {p.st()['sfm']}", flush=True)
    result("SOFT", "scope_forced_mode=0 in a plain EDIT", p.st()["sfm"] == 0, f"sfm={p.st()['sfm']}")
    for label, ctl in (("ES_PASSWORD", pw), ("ES_NUMBER", num)):
        p.focus(ctl)
        pump(1.5)
        s = p.st()
        print(f"probe: info focus in {label} EDIT: scope_forced_mode={s['sfm']} eo={s['eo']} tsf_open={s['to']} "
              f"eff_mode={s['em']} tsf_mode={s['tm']}", flush=True)
        if s["sfm"] != 0:
            # open the IME there: does scope_forced_mode stay?
            send_vk(VK_F2)
            pump(1.5)
            s2 = p.st()
            print(f"probe: info {label} after F2 (IME open requested): scope_forced_mode={s2['sfm']} eo={s2['eo']} tsf_open={s2['to']}",
                  flush=True)
    p.focus(edit)
    pump(1.0)
    print(f"probe: info back in the plain EDIT: sfm={p.st()['sfm']}", flush=True)

    # ---- key_seq while the IME is closed: the key is counted although the TIP does not eat it.
    s, d = p.press(VK_KANJI)  # toggle off (the MS-IME keymap: Kanji = on/off)
    s = p.wait(lambda s: s["eo"] == 0, 8)
    print(f"probe: info IME off by Kanji key: delta={d} {fmt(s)}", flush=True)
    s, d = p.press(0x58)  # X typed while closed
    result("HARD", "key_seq advances for a key typed while the IME is closed", d == 1, f"delta={d} {fmt(s)}")

    # ---- pulse count over a burst of state changes: on / off x3 = 6 state changes.
    pump(0.5)
    pulses0 = p.pulses
    for _ in range(3):
        p.press(VK_F2)
        p.wait(lambda s: s["eo"] == 1, 8)
        p.press(VK_KANJI)
        p.wait(lambda s: s["eo"] == 0, 8)
    pump(0.3)
    print(f"probe: info pulses for 6 state changes (3x on/off) = {p.pulses - pulses0}", flush=True)
    result("SOFT", "pulses match the number of state changes (6) within the 20 ms wait granularity", p.pulses - pulses0 == 6,
           f"pulses={p.pulses - pulses0}")

    # ---- deactivation (last: global to the session).
    if os.environ.get("MOZC_CI_DEACTIVATE") == "1":
        p.press(VK_F2)
        p.wait(lambda s: s["eo"] == 1, 8)
        p.press(0x4B)  # leave a composition behind
        s = p.settle(p.st()["key_seq"], lambda s: s["phase"] == 1, 5)
        before = s
        print(f"probe: info before deactivate: {fmt(before)}", flush=True)
        u32.LoadKeyboardLayoutW.restype = ctypes.c_void_p
        u32.ActivateKeyboardLayout.argtypes = [ctypes.c_void_p, wt.UINT]
        hkl = u32.LoadKeyboardLayoutW("00000409", 1)
        u32.ActivateKeyboardLayout(hkl, 0)
        s = p.wait(lambda s: s["active"] == 0, 8)
        print(f"probe: info after deactivate: {fmt(s)}", flush=True)
        result("SOFT", "after Deactivate the composition fields and scope_forced_mode are 0 and key_seq is kept",
               s and s["active"] == 0 and s["phase"] == 0 and s["cat"] == 0 and s["plen"] == 0 and s["pcur"] == 0
               and s["sfm"] == 0 and s["cks"] == 0 and before and s["key_seq"] == before["key_seq"], fmt(s))
    p._stop = True
    pump(0.2)


try:
    main()
except SystemExit:
    raise
except BaseException:
    import traceback
    traceback.print_exc()
    sys.exit(1)
