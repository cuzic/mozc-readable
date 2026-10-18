"""CI helper: start ui_host.py inside an AppContainer (the sandbox UWP apps run in).

usage: ac_launch.py <work dir> <idle seconds>
Creates (or reuses) the AppContainer profile "ReadableCiAC", grants it read access to the Python
install and this directory, and starts `python -I -B ui_host.py <tid file> "" <idle>` with the
AppContainer token. stdout/stderr of the child go to <work dir>\\ac_ui_host.out. Returns right away.
"""
import ctypes
import ctypes.wintypes as wt
import msvcrt
import os
import subprocess
import sys

NAME = "ReadableCiAC"
userenv = ctypes.WinDLL("userenv")
adv = ctypes.WinDLL("advapi32")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.LPVOID, wt.LPVOID, wt.BOOL, wt.DWORD, wt.LPVOID,
                               wt.LPCWSTR, wt.LPVOID, wt.LPVOID]
k32.UpdateProcThreadAttribute.argtypes = [wt.LPVOID, wt.DWORD, ctypes.c_size_t, wt.LPVOID, ctypes.c_size_t,
                                          wt.LPVOID, wt.LPVOID]
k32.InitializeProcThreadAttributeList.argtypes = [wt.LPVOID, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.c_void_p),
                ("CapabilityCount", wt.DWORD), ("Reserved", wt.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD),
                ("dwThreadId", wt.DWORD)]


def main():
    workdir = os.path.abspath(sys.argv[1])
    idle = sys.argv[2] if len(sys.argv) > 2 else "60"
    os.makedirs(workdir, exist_ok=True)

    psid = ctypes.c_void_p()
    hr = userenv.CreateAppContainerProfile(NAME, NAME, NAME, None, 0, ctypes.byref(psid))
    if hr & 0xFFFFFFFF == 0x800700B7:  # already exists
        hr = userenv.DeriveAppContainerSidFromAppContainerName(NAME, ctypes.byref(psid))
    if hr != 0:
        print(f"ac_launch: cannot create/derive the AppContainer profile (HRESULT {hr & 0xFFFFFFFF:#x})")
        sys.exit(1)
    s = wt.LPWSTR()
    adv.ConvertSidToStringSidW(psid, ctypes.byref(s))
    sid = s.value
    print(f"ac_launch: AppContainer SID {sid}", flush=True)

    here = os.path.dirname(os.path.abspath(__file__))
    py_dir = os.path.dirname(os.path.abspath(sys.executable))
    for path, perm in ((py_dir, "RX"), (sys.base_prefix, "RX"), (here, "RX"), (workdir, "M")):
        r = subprocess.run(["icacls", path, "/grant", f"*{sid}:(OI)(CI){perm}", "/T", "/C", "/Q"],
                           capture_output=True, text=True)
        print(f"ac_launch: icacls grant {perm} on {path}: exit {r.returncode}", flush=True)

    out = open(os.path.join(workdir, "ac_ui_host.out"), "wb")
    hout = msvcrt.get_osfhandle(out.fileno())
    ctypes.windll.kernel32.SetHandleInformation(hout, 1, 1)

    caps = SECURITY_CAPABILITIES(psid, None, 0, 0)
    size = ctypes.c_size_t()
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attr = ctypes.create_string_buffer(size.value)
    if not k32.InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(size)):
        print("ac_launch: InitializeProcThreadAttributeList failed", ctypes.get_last_error())
        sys.exit(1)
    if not k32.UpdateProcThreadAttribute(attr, 0, 0x20009, ctypes.byref(caps), ctypes.sizeof(caps), None, None):
        print("ac_launch: UpdateProcThreadAttribute failed", ctypes.get_last_error())
        sys.exit(1)

    si = STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
    si.StartupInfo.hStdOutput = hout
    si.StartupInfo.hStdError = hout
    si.lpAttributeList = ctypes.cast(attr, ctypes.c_void_p)
    pi = PROCESS_INFORMATION()
    cmd = f'"{sys.executable}" -I -B "{os.path.join(here, "ui_host.py")}" "{os.path.join(workdir, "ac_tid.txt")}" "" {idle}'
    # EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW (a python.exe console window would take the foreground)
    ok = k32.CreateProcessW(None, cmd, None, None, True, 0x80000 | 0x08000000, None, workdir,
                            ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        print(f"ac_launch: CreateProcessW failed, GetLastError={ctypes.get_last_error()}")
        sys.exit(1)
    print(f"ac_launch: started pid={pi.dwProcessId} in the AppContainer", flush=True)


main()
