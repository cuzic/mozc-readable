"""CI helper: a plain Win32 window with an EDIT child that sends IME toggle keys.

The TIP is loaded into *this* process, so the shared memory block
Local\\mozc-readable-ime-<tid> is created by this process' UI thread. The thread id
is written to the file given as argv[1]; reader.py (a separate process) reads it.
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
u32.GetForegroundWindow.restype = wt.HWND
u32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
u32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
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
    tid_file = sys.argv[1]
    vks = [int(x, 16) for x in sys.argv[2].split(",")]  # keys to send, one per step
    wndproc = WNDPROC(lambda h, m, w, l: u32.DefWindowProcW(h, m, w, l))
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.lpszClassName = "ReadableCiHost"
    u32.RegisterClassW(ctypes.byref(wc))
    hwnd = u32.CreateWindowExW(0, "ReadableCiHost", "readable-ci", 0x10CF0000, 100, 100, 500, 200,
                               None, None, wc.hInstance, None)
    edit = u32.CreateWindowExW(0x200, "EDIT", "", 0x50010000 | 0x0080, 10, 10, 460, 120, hwnd,
                               None, wc.hInstance, None)
    u32.ShowWindow(hwnd, 5)
    u32.SetForegroundWindow(hwnd)
    u32.SetFocus(edit)
    tid = k32.GetCurrentThreadId()
    with open(tid_file, "w") as f:
        f.write(str(tid))
    print(f"ui_host: tid={tid} hwnd={(hwnd or 0):#x} fg={u32.GetForegroundWindow() == hwnd}", flush=True)
    pump(6)  # let the TIP load and publish its initial state
    for n, vk in enumerate(vks):
        u32.SetForegroundWindow(hwnd)
        u32.SetFocus(edit)
        print(f"ui_host: send vk={vk:#x} t_ns={time.time_ns()}", flush=True)
        send_vk(vk)
        # The first key in a fresh process took ~3.1 s to take effect in every CI run (a new Mozc
        # session is created); wait it out so the next key does not arrive mid-activation and get lost.
        pump(7 if n == 0 else 3)
    pump(2)


main()
