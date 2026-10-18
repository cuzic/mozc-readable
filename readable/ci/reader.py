"""CI helper: reads Local\\mozc-readable-ime-<tid> from a *different* process.

usage: reader.py <tid_file> <seconds> [event]
With "event", the loop blocks on Local\\mozc-readable-ime-changed (50 ms timeout as a safety net)
instead of polling every 20 ms, and every printed change carries t_ns=time.time_ns().
Prints every distinct (effective_open, effective_mode, tsf_open, tsf_mode) it sees.
Exit code 0 if the mapping was found, 2 otherwise.
"""
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time

k32 = ctypes.windll.kernel32
k32.OpenFileMappingW.restype = wt.HANDLE
k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
k32.OpenEventW.restype = wt.HANDLE
k32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
SYNCHRONIZE = 0x00100000
FILE_MAP_READ = 4
FMT2 = "<IIIIIIIIIIQ"  # TipSharedState version 2, 48 bytes
FMT3 = "<10IQ8I"        # version 3, 80 bytes: phase cat plen pcur key_seq scope_forced_mode server_ready composition_key_seq
SIZE = 80               # the section is mapped whole (size 0); a version 2 block is read as its first 48 bytes
assert struct.calcsize(FMT2) == 48 and struct.calcsize(FMT3) == 80


def unpack(raw):
    a = struct.unpack(FMT2, raw[:48])
    return a + (struct.unpack(FMT3, raw)[11:] if a[1] >= 3 else ())


def snapshot(view):
    for _ in range(100):
        a = unpack(ctypes.string_at(view, SIZE))
        if a[2] % 2:
            continue
        b = unpack(ctypes.string_at(view, SIZE))
        if a[2] == b[2]:
            return a
    return None


def main():
    tid_file, seconds = sys.argv[1], float(sys.argv[2])
    use_event = len(sys.argv) > 3 and sys.argv[3] == "event"
    ev = None
    wakes = 0
    end = time.time() + seconds
    view = None
    seen = []
    tid = None
    while time.time() < end:
        if view is None:
            try:
                tid = int(open(tid_file).read().strip())
            except Exception:
                time.sleep(0.05)
                continue
            h = k32.OpenFileMappingW(FILE_MAP_READ, False, f"Local\\mozc-readable-ime-{tid}")
            if h:
                view = k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
                print(f"reader: opened mapping for tid={tid}", flush=True)
        if use_event and view and ev is None:
            ev = k32.OpenEventW(SYNCHRONIZE, False, "Local\\mozc-readable-ime-changed")
            if ev:
                print("reader: opened change event", flush=True)
        if use_event and ev:
            if k32.WaitForSingleObject(ev, 50) == 0:
                wakes += 1
        if view:
            s = snapshot(view)
            if s:
                state = (s[3], s[4], s[5], s[6])
                if not seen or seen[-1] != state:
                    seen.append(state)
                    print(f"reader: seq={s[2]} eff_open={s[3]} eff_mode={s[4]} "
                          f"tsf_open={s[5]} tsf_mode={s[6]} tid={s[7]} pid={s[8]} active={s[9]} version={s[1]} "
                          + (f"phase={s[11]} cat={s[12]} plen={s[13]} pcur={s[14]} key_seq={s[15]} sfm={s[16]} "
                             f"srv={s[17]} cks={s[18]} " if len(s) > 11 else "")
                          + f"t_ns={time.time_ns()}", flush=True)
        if not (use_event and ev):
            time.sleep(0.02)
    print(f"reader: wakes={wakes}", flush=True)
    print(f"reader: distinct states seen: {seen}", flush=True)
    sys.exit(0 if view else 2)


main()
