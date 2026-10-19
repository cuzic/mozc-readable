"""CI experiment (ADR-267 D11), Chrome side: do keys leak right after the IME turned on, and what do key_seq /
key_eaten_seq / the page text say? (The EDIT in key_leak_probe.py never leaked; Chrome is where BUG-002 was seen.)

usage: chrome_leak_probe.py <window class>
The page (verify.ps1, section keyleak-chrome) mirrors input.value into document.title as `v:<value>`, so the text that is
really in the input (preedit included, Chrome reports the composition text in the input event) is read with
GetWindowTextW of the Chrome window. The shared block of the foreground thread is read cross-process (88 bytes).

One line per scenario: `probe: scenario <name>: sent=.. dkey=.. deaten=.. leak_est=dkey-deaten title=.. plen=.. eo=.. srv=..`.
Nothing here is a HARD check; `probe: SOFT` lines become warnings.
"""
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
u32.GetForegroundWindow.restype = wt.HWND
u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
u32.GetWindowThreadProcessId.restype = wt.DWORD
u32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u32.IsWindowVisible.argtypes = [wt.HWND]
u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
k32.OpenFileMappingW.restype = wt.HANDLE
k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

FMT = "<10IQ10I"  # 88 bytes (experiment layout)
NAMES = ("magic version seq eo em to tm tid pid active tick phase cat plen pcur key_seq sfm srv cks eaten pad").split()
assert struct.calcsize(FMT) == 88 and len(NAMES) == 21
VK_F2, VK_KANJI, VK_ESCAPE, VK_CONTROL, VK_A, VK_DELETE = 0xF2, 0x19, 0x1B, 0x11, 0x41, 0x2E
K, A_, I_ = 0x4B, 0x41, 0x49


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def key_event(vk, up):
    i = INPUT(type=1)
    i.ki = KEYBDINPUT(vk, u32.MapVirtualKeyW(vk, 0), 2 if up else 0, 0, 0)
    u32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def send_vk(vk, gap=0.03):
    key_event(vk, False)
    time.sleep(gap)
    key_event(vk, True)
    time.sleep(gap)


def cls_of(h):
    b = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(h, b, 256)
    return b.value


def tid_pid(h):
    pid = wt.DWORD()
    return u32.GetWindowThreadProcessId(h, ctypes.byref(pid)), pid.value


def find_window(cls):
    found = []

    def cb(h, _):
        if u32.IsWindowVisible(h) and cls_of(h) == cls:
            found.append(h)
        return True
    u32.EnumWindows(ENUMPROC(cb), 0)
    return found[0] if found else None


def force_foreground(hwnd):
    fg = u32.GetForegroundWindow()
    cur = k32.GetCurrentThreadId()
    fg_tid = tid_pid(fg)[0] if fg else 0
    if fg_tid and fg_tid != cur:
        u32.AttachThreadInput(cur, fg_tid, True)
    u32.ShowWindow(hwnd, 9)
    u32.SetForegroundWindow(hwnd)
    if fg_tid and fg_tid != cur:
        u32.AttachThreadInput(cur, fg_tid, False)


def click_page(hwnd):
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    u32.SetCursorPos((r.left + r.right) // 2, (r.top + r.bottom) // 2 + 40)
    u32.mouse_event(2, 0, 0, 0, 0)
    u32.mouse_event(4, 0, 0, 0, 0)


_views = {}


def read(tid):
    v = _views.get(tid)
    if v is None:
        h = k32.OpenFileMappingW(4, False, f"Local\\mozc-readable-ime-{tid}")
        if not h:
            return None
        v = k32.MapViewOfFile(h, 4, 0, 0, 0)
        if not v:
            return None
        _views[tid] = v
    for _ in range(100):
        a = struct.unpack(FMT, ctypes.string_at(v, 88))
        if a[2] % 2 == 0 and a[2] == struct.unpack("<I", ctypes.string_at(v + 8, 4))[0]:
            return dict(zip(NAMES, a))
    return None


def i32(x):
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


def title_value(hwnd):
    b = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, b, 512)
    t = b.value
    i, j = t.find("v:"), t.rfind("|")
    return t[i + 2:j] if i >= 0 and j > i else None  # the page sets `v:<value>|`; None until the first input event


def wait_for(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        s = pred()
        if s:
            return s
        time.sleep(0.02)
    return pred()


class Ctx:
    def __init__(self, hwnd, tid):
        self.hwnd, self.tid = hwnd, tid

    def st(self):
        return read(self.tid)

    def fg_ok(self):
        return u32.GetForegroundWindow() == self.hwnd

    def clear(self):
        """Drop any composition and empty the input (Esc, Ctrl+A, Delete)."""
        send_vk(VK_ESCAPE)
        key_event(VK_CONTROL, False)
        send_vk(VK_A)
        key_event(VK_CONTROL, True)
        send_vk(VK_DELETE)
        time.sleep(0.4)

    def scenario(self, name, vks, gap=0.002, settle=1.5, prelude=None):
        self.clear()
        s0 = self.st()  # before the prelude: the prelude's own key is counted (extra)
        extra = 0
        if prelude:
            prelude()
            extra = 1
        fg_before = self.fg_ok()
        t0 = time.time()
        for vk in vks:
            send_vk(vk, gap=gap)
        time.sleep(settle)
        s1 = self.st()
        val = title_value(self.hwnd)
        dkey = i32(s1["key_seq"] - s0["key_seq"])
        deaten = i32(s1["eaten"] - s0["eaten"])
        print(f"probe: scenario {name}: sent={len(vks) + extra} dkey={dkey} deaten={deaten} leak_est={dkey - deaten} "
              f"title_value={ascii(val)} plen={s1['plen']} phase={s1['phase']} eo={s0['eo']}->{s1['eo']} "
              f"srv={s0['srv']}->{s1['srv']} fg_ok={fg_before} took={time.time() - t0:.2f}s", flush=True)
        return dict(sent=len(vks) + extra, dkey=dkey, deaten=deaten, val=val, plen=s1["plen"])

    def turn_on(self, label, timeout=10):
        """F2 until open (Chrome often ignores the first one: BUG-002), logging every try."""
        for attempt in range(1, 4):
            s0 = self.st()
            send_vk(VK_F2)
            s = wait_for(lambda: (lambda x: x if x and x["eo"] == 1 else None)(self.st()), timeout)
            print(f"probe: info {label} F2 try={attempt} eo={s['eo'] if s else None} "
                  f"dkey={i32(self.st()['key_seq'] - s0['key_seq'])}", flush=True)
            if s and s["eo"] == 1:
                return True
        return False

    def turn_off(self):
        send_vk(VK_KANJI)
        wait_for(lambda: (lambda x: x if x and x["eo"] == 0 else None)(self.st()), 8)


def result(name, ok, detail=""):
    detail = str(detail).encode("ascii", "backslashreplace").decode()  # the redirected stdout is cp1252
    print(f"probe: SOFT {name} ok={bool(ok)} {detail}", flush=True)


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", wt.LONG), ("tpDeltaPri", wt.LONG), ("dwFlags", wt.DWORD)]


k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.Thread32First.argtypes = [wt.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.Thread32Next.argtypes = [wt.HANDLE, ctypes.POINTER(THREADENTRY32)]


def scan_blocks():
    """{tid: (pid, key_seq, eaten, plen, phase, eo)} for every thread in the session that has a block (any process)."""
    out = {}
    snap = k32.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(THREADENTRY32)
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        h = k32.OpenFileMappingW(4, False, f"Local\\mozc-readable-ime-{te.th32ThreadID}")
        if h:
            v = _views.get(te.th32ThreadID) or k32.MapViewOfFile(h, 4, 0, 0, 0)
            _views[te.th32ThreadID] = v
            a = struct.unpack(FMT, ctypes.string_at(v, 88)) if v else None
            if a and a[1] >= 3:
                out[te.th32ThreadID] = (te.th32OwnerProcessID, a[15], a[19], a[13], a[11], a[3])
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)
    return out


def main():
    target = sys.argv[1]
    hwnd = wait_for(lambda: find_window(target), 30)
    if not hwnd:
        print("probe: info no Chrome window", flush=True)
        sys.exit(2)
    for _ in range(10):
        force_foreground(hwnd)
        if u32.GetForegroundWindow() == hwnd:
            break
        time.sleep(0.5)
    tid, pid = tid_pid(hwnd)
    print(f"probe: info chrome hwnd={hwnd:#x} tid={tid} pid={pid} fg_ok={u32.GetForegroundWindow() == hwnd}", flush=True)
    s = wait_for(lambda: (click_page(hwnd), read(tid))[1], 30)
    if s is None:
        print("probe: info no shared block for the Chrome thread", flush=True)
        sys.exit(3)
    time.sleep(1.0)
    click_page(hwnd)
    time.sleep(0.5)
    c = Ctx(hwnd, tid)
    print(f"probe: info initial eo={s['eo']} srv={s['srv']} key_seq={s['key_seq']} eaten={s['eaten']} version={s['version']}",
          flush=True)

    closed = c.scenario("C0 IME closed, 'ka'", [K, A_])
    # The report: the first keys right after the IME turned on. Repeated: off, idle, on, type at once.
    colds = []
    for n in range(1, 5):
        c.turn_off()
        time.sleep(3.0)  # idle before the cold start

        def on_then_nothing():
            send_vk(VK_F2, gap=0.002)  # the F2 is part of the scenario's prelude: no waiting for eo=1
        colds.append(c.scenario(f"C1.{n} cold: off, idle 3s, F2 then 'ka' at once", [K, A_], gap=0.002, prelude=on_then_nothing))
    c.turn_on("before warm")
    warm = c.scenario("C2 warm, IME on, 'ka' (gap 30ms)", [K, A_], gap=0.03)
    warm4 = c.scenario("C3 warm, IME on, 'kaki' (gap 30ms)", [K, A_, K, I_], gap=0.03)
    pend = c.scenario("C4 warm, IME on, 'k' only", [K])
    c.turn_off()
    time.sleep(10.0)
    c.turn_on("after 10 s idle")
    idle = c.scenario("C5 after idle 10 s with the IME on, 'ka'", [K, A_], gap=0.002)

    # Where do the counters move? Every thread with a block, before and after 'ka' typed with the IME on.
    c.turn_on("scan")
    c.clear()
    before = scan_blocks()
    print(f"probe: info scan: {len(before)} blocks; fg tid={tid}; owners="
          f"{sorted({(t, v[0]) for t, v in before.items()})}", flush=True)
    for vk in (K, A_):
        send_vk(vk, gap=0.03)
    time.sleep(1.5)
    after = scan_blocks()
    moved = []
    for t, v in after.items():
        b = before.get(t)
        if b is None or (v[1], v[2], v[3], v[4]) != (b[1], b[2], b[3], b[4]):
            moved.append((t, v[0], "new" if b is None else f"key_seq {b[1]}->{v[1]} eaten {b[2]}->{v[2]} plen {b[3]}->{v[3]} phase {b[4]}->{v[4]}"))
    print(f"probe: info scan moved after 'ka' (tid, pid, change): {moved}", flush=True)
    result("the counters move on some thread when 'ka' is typed in Chrome", any("key_seq" in m[2] and m[2].split()[1].split("->")[0] != m[2].split()[1].split("->")[1] for m in moved),
           f"moved={moved}")

    result("H1 key_seq counts every key (Chrome, IME closed)", closed["dkey"] == closed["sent"],
           f"sent={closed['sent']} dkey={closed['dkey']}")
    result("H2 closed IME: key_eaten_seq does not advance and the text is literal", closed["deaten"] == 0 and closed["val"] == "ka",
           f"deaten={closed['deaten']} val={ascii(closed['val'])}")
    for n, x in enumerate(colds, 1):
        result(f"H4 cold {n}: nothing leaked (all keys eaten, no literal 'k'/'a' in the page)",
               x["dkey"] == x["deaten"] and x["val"] is not None and "k" not in x["val"] and "a" not in x["val"],
               f"dkey={x['dkey']} deaten={x['deaten']} val={ascii(x['val'])}")
    result("H5 pending 'k' is in the preedit and the page text", pend["plen"] == 1 and pend["val"] in ("k", "ｋ"),
           f"plen={pend['plen']} val={ascii(pend['val'])}")
    result("warm: all keys eaten", warm["dkey"] == warm["deaten"] == warm["sent"], f"{warm}")


if __name__ == "__main__":
    main()
