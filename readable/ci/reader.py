"""CI helper: reads Local\\mozc-readable-ime-<tid> from a *different* process.

usage: reader.py <tid_file> <seconds>
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
FILE_MAP_READ = 4
FMT = "<IIIIIIIIIIQ"  # TipSharedState, 48 bytes
SIZE = struct.calcsize(FMT)
assert SIZE == 48


def snapshot(view):
    for _ in range(100):
        a = struct.unpack(FMT, ctypes.string_at(view, SIZE))
        if a[2] % 2:
            continue
        b = struct.unpack(FMT, ctypes.string_at(view, SIZE))
        if a[2] == b[2]:
            return a
    return None


def main():
    tid_file, seconds = sys.argv[1], float(sys.argv[2])
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
                view = k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, SIZE)
                print(f"reader: opened mapping for tid={tid}", flush=True)
        if view:
            s = snapshot(view)
            if s:
                state = (s[3], s[4], s[5], s[6])
                if not seen or seen[-1] != state:
                    seen.append(state)
                    print(f"reader: seq={s[2]} eff_open={s[3]} eff_mode={s[4]} "
                          f"tsf_open={s[5]} tsf_mode={s[6]} tid={s[7]} pid={s[8]}", flush=True)
        time.sleep(0.02)
    print(f"reader: distinct states seen: {seen}", flush=True)
    sys.exit(0 if view else 2)


main()
