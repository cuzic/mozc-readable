"""CI helper: drive and observe the *foreground application* the way a real consumer would.

usage: drive_foreground.py <window class> <steps> [ready_seconds]
  steps like ui_host.py: "VK=open:mode,..." (hex VK, "*" = any). The state is read from the
  shared block of whatever thread owns the foreground window (cross-process), the key is re-sent
  (up to 3 times) until the state matches.
Exit codes: 0 all steps reached, 2 target window never became foreground, 3 no shared block for the
foreground thread (TIP not publishing there), 4 a step was not reached.
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
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
k32.OpenFileMappingW.restype = wt.HANDLE
k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
FMT = "<IIIIIIIIIIQ"


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def send_vk(vk):
    for flags in (0, 2):
        i = INPUT(type=1)
        i.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
        u32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
        time.sleep(0.03)


def cls_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def tid_pid(hwnd):
    pid = wt.DWORD()
    tid = u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return tid, pid.value


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


_views = {}


def state_of_thread(tid):
    v = _views.get(tid)
    if v is None:
        h = k32.OpenFileMappingW(4, False, f"Local\\mozc-readable-ime-{tid}")
        if not h:
            return None
        v = k32.MapViewOfFile(h, 4, 0, 0, 48)
        if not v:
            return None
        _views[tid] = v
    for _ in range(50):
        a = struct.unpack(FMT, ctypes.string_at(v, 48))
        if a[2] % 2 == 0 and a == struct.unpack(FMT, ctypes.string_at(v, 48)):
            return (a[3], a[4])
    return None


def fg_state():
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return None, None
    tid, _ = tid_pid(hwnd)
    return tid, state_of_thread(tid)


def main():
    target = sys.argv[1]
    steps = []
    for item in sys.argv[2].split(","):
        vk_s, _, exp = item.partition("=")
        eo, _, em = exp.partition(":")
        steps.append((int(vk_s, 16), None if eo in ("", "*") else int(eo), None if em in ("", "*") else int(em)))
    ready = float(sys.argv[3]) if len(sys.argv) > 3 else 25

    hwnd = None
    end = time.time() + ready
    while time.time() < end:
        hwnd = find_window(target)
        if hwnd:
            force_foreground(hwnd)
            if cls_of(u32.GetForegroundWindow()) == target:
                break
        time.sleep(0.5)
    fg = u32.GetForegroundWindow()
    tid, pid = tid_pid(fg) if fg else (0, 0)
    print(f"drive: target={target} found={bool(hwnd)} fg_class={cls_of(fg) if fg else None} fg_tid={tid} fg_pid={pid}",
          flush=True)
    if not hwnd or cls_of(fg) != target:
        sys.exit(2)

    # Give the application time to create its TSF thread manager and load the TIP.
    st = None
    end = time.time() + ready
    while time.time() < end:
        if hwnd:
            # clicking into the client area makes sure a text input has focus
            r = wt.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            u32.SetCursorPos((r.left + r.right) // 2, (r.top + r.bottom) // 2 + 40)
            u32.mouse_event(2, 0, 0, 0, 0)
            u32.mouse_event(4, 0, 0, 0, 0)
        tid, st = fg_state()
        if st is not None:
            break
        time.sleep(1)
    print(f"drive: shared block for fg tid={tid}: {'found, initial state=' + str(st) if st is not None else 'NOT FOUND'}",
          flush=True)
    if st is None:
        sys.exit(3)

    def matches(s, eo, em):
        return s is not None and (eo is None or s[0] == eo) and (em is None or s[1] == em)

    for vk, eo, em in steps:
        for attempt in range(1, 4):
            print(f"drive: {'send' if attempt == 1 else 'again'} vk={vk:#x} t_ns={time.time_ns()} try={attempt}", flush=True)
            send_vk(vk)
            deadline = time.time() + 8
            while time.time() < deadline and not matches(fg_state()[1], eo, em):
                time.sleep(0.02)
            now = fg_state()
            ok = matches(now[1], eo, em)
            print(f"drive: vk={vk:#x} try={attempt} -> fg_tid={now[0]} state={now[1]} expected=({eo},{em}) ok={ok}", flush=True)
            if ok:
                break
        else:
            sys.exit(4)
        time.sleep(1)
    sys.exit(0)


main()
