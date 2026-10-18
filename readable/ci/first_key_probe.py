"""CI helper: does the FIRST IME-on key of a fresh process take effect?  (Works with any Mozc build.)

usage: first_key_probe.py <delay seconds before the key>

A fresh process opens a plain Win32 window with an EDIT, waits <delay> seconds (pumping messages) after the
window is foreground and focused, sends the Hiragana key (VK 0xF2: IMEOn while the IME is closed in Mozc's
default keymap), then 'A' and Enter.  If the key took effect the IME is on, 'a' becomes "あ" and the EDIT
holds "あ"; if it was lost the EDIT holds "a".  The oracle is the text, so it does not depend on any API of
the IME under test (the official Mozc has no shared-memory block).

Prints one line:  RESULT delay=<s> text=<repr> ok=<True|False> t_window_ms=<ms>
"""
import ctypes
import ctypes.wintypes as wt
import sys
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
u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.DefWindowProcW.restype = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


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


def send_vk(vk):
    for flags in (0, 2):
        i = INPUT(type=1)
        i.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
        u32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
        time.sleep(0.03)


def pump(seconds):
    end = time.time() + seconds
    msg = wt.MSG()
    while time.time() < end:
        while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def main():
    delay = float(sys.argv[1])
    t_start = time.time()
    wndproc = WNDPROC(lambda h, m, w, l: u32.DefWindowProcW(h, m, w, l))
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.lpszClassName = "FirstKeyProbe"
    u32.RegisterClassW(ctypes.byref(wc))
    hwnd = u32.CreateWindowExW(0, "FirstKeyProbe", "first-key", 0x10CF0000, 100, 100, 500, 200,
                               None, None, wc.hInstance, None)
    edit = u32.CreateWindowExW(0x200, "EDIT", "", 0x50010000 | 0x0080, 10, 10, 460, 120, hwnd,
                               None, wc.hInstance, None)
    u32.ShowWindow(hwnd, 5)
    u32.SetForegroundWindow(hwnd)
    u32.SetFocus(edit)
    t_window_ms = int((time.time() - t_start) * 1000)
    pump(delay)                        # <- the variable under test: how long the thread lives before the key
    u32.SetForegroundWindow(hwnd)
    u32.SetFocus(edit)
    send_vk(0xF2)                      # Hiragana key: IMEOn while closed
    pump(1.0)
    send_vk(0x41)                      # 'A'
    pump(0.6)
    send_vk(0x0D)                      # Enter: commit the composition
    pump(1.0)
    buf = ctypes.create_unicode_buffer(64)
    u32.GetWindowTextW(edit, buf, 64)
    text = buf.value
    print(f"RESULT delay={delay} text={text!r} ok={text == chr(0x3042)} t_window_ms={t_window_ms}", flush=True)


main()
