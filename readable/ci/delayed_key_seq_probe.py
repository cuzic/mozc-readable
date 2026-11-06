"""CI helper (EXPERIMENTAL, one-off, awase exp/mozc-chrome-compose-queue, 2026-11-06): checks that
Backspace(1) + a full resend still ends with the correct text when a reader's view of this thread's
shared memory is artificially frozen past awase's compose-check deadline (MOZC_COMPOSE_CHECK_MS, 300ms).

Why: awase's compose-check (ADR-270/284/285) does not arm on Chrome-family windows, because Chrome's
TSF composition context can take longer than 300ms to become usable after a cold start, and a
Backspace sent on a false "Literal" verdict could destroy text that was about to compose correctly
(PR #809 review M3). A design review (2026-11-06, see awase's cross-session research) read Mozc's own
session.cc and concluded Backspace is safe either way (consumed by the composer if one is open, passed
through to the app only if not) -- but that conclusion was never checked against a live TIP, only
against the source. Reproducing Chrome's actual cold-start timing in CI is not guaranteed (a CI runner's
Chrome may not exhibit the same delay as a real desktop), so instead this probe makes *this* TIP itself
lie about its own state for one key: `MOZC_CI_DELAY_AT_KEY_SEQ=<N>` (read fresh on every call, by
TipStatePublisher::CiKeySeqToDelay in tip_state_publisher.cc) makes Flush() withhold every publish from
the moment this thread's key_seq reaches N until the *next* key-down is counted. Real processing (the
server round trip, the edit session, the composition) proceeds immediately and normally underneath --
only what a reader can observe through the shared memory is held back. This reproduces exactly the
symptom awase's compose-check reacts to (key_seq/phase frozen past the deadline) without depending on
Chrome's own timing, and lets this probe drive the *exact* recovery awase's FSM would send
(Backspace x1, no Escape, full resend -- mozc_compose_check_fsm.rs's Literal verdict handling) against
a real TIP + real Mozc server, then check the real resulting text.

N is a specific key_seq value, not just "the first key", because this probe needs a setup key first
(F2, to turn the IME on) whose publish must not be suppressed -- it reads key_seq after F2, sets N to
that value + 1 (the 'k' it is about to send), *then* sends 'k'. The hook is re-read fresh on every
BumpKeySeq, not cached at process start, specifically so it can be set this late.

This is a plain EDIT control, not an actual Chrome window (see the module docstring above for why: the
Backspace/Escape behavior this checks is in Mozc's own session.cc, not in any Chrome-specific TSF text
store code, and the one Chrome-specific risk a prior review raised -- an un-consumed Escape reaching
the browser as a command -- does not apply here, since the real FSM never sends Escape for this verdict
(escape_composition: false) and this probe does not either). If this probe's result changes course,
Chrome itself should still be checked before relying on the conclusion for Chrome specifically.

Usage: delayed_key_seq_probe.py <tid_file>
Exit code 0 iff every HARD check passed.
"""
import ctypes
import ctypes.wintypes as wt
import faulthandler
import os
import struct
import sys
import time

# The CI console's default codepage (cp1252) cannot encode composed kana in the final text check
# below; reconfigure stdout/stderr to UTF-8 (a known pitfall in this repo's CI harnesses).
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

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
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

# TipSharedState version 5, 160 bytes -- same layout as composition_probe.py. This probe's CI hook
# (MOZC_CI_DELAY_FIRST_KEY_PUBLISH) does not depend on version 6 (the unrelated, unmerged
# direct-actuation experiment on a different branch): it only changes *when* Flush() writes, not
# what fields exist, so this branch stays on the released version 5 layout.
FMT = "<10IQ16I2I4Q2I"
NAMES = ("magic version seq eo em to tm tid pid active tick phase cat plen pcur key_seq sfm srv cks "
         "csc sub_ee sub_term revert sub_other esf flags ctx prefer_kana toggle_enabled "
         "ks_focus ks_blur thr_focus first_key eaten passed").split()
assert struct.calcsize(FMT) == 160 and len(NAMES) == 35

VK_F2, VK_BACK = 0xF2, 0x08
MOZC_COMPOSE_CHECK_MS = 300  # crates/awase-windows-base/src/tuning.rs::MOZC_COMPOSE_CHECK_MS


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
        a = struct.unpack(FMT, ctypes.string_at(_view, 160))
        if a[2] % 2:
            continue
        if a[2] == struct.unpack("<I", ctypes.string_at(_view + 8, 4))[0]:
            return dict(zip(NAMES, a))
    return None


def result(kind, name, ok, detail=""):
    print(f"probe: {kind} {name} ok={bool(ok)} {detail}", flush=True)


def fmt(s):
    if s is None:
        return "None"
    return " ".join(f"{k}={s[k]}" for k in ("version", "active", "phase", "key_seq", "cks", "eaten", "passed"))


def get_text(edit):
    buf = ctypes.create_unicode_buffer(256)
    u32.SendMessageW(edit, 0x000D, 256, ctypes.cast(buf, wt.LPWSTR))  # WM_GETTEXT
    return buf.value


def main():
    faulthandler.enable()
    tid_file = sys.argv[1]
    wndproc = WNDPROC(lambda h, m, w, l: u32.DefWindowProcW(h, m, w, l))
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.lpszClassName = "ReadableCiDelayProbe"
    u32.RegisterClassW(ctypes.byref(wc))
    hwnd = u32.CreateWindowExW(0, "ReadableCiDelayProbe", "readable-delay-probe", 0x10CF0000, 100, 100, 600, 400,
                               None, None, wc.hInstance, None)
    edit = u32.CreateWindowExW(0x200, "EDIT", "", 0x50010000 | 0x0080, 10, 10, 560, 100, hwnd,
                               None, wc.hInstance, None)
    u32.ShowWindow(hwnd, 5)
    u32.SetForegroundWindow(hwnd)
    u32.SetFocus(edit)
    tid = k32.GetCurrentThreadId()
    with open(tid_file, "w") as f:
        f.write(str(tid))
    print(f"probe: info tid={tid} hwnd={(hwnd or 0):#x}", flush=True)
    pump(3)  # the TIP loads and publishes its initial state

    s = read(tid)
    print(f"probe: info initial {fmt(s)}", flush=True)
    if s is None or s["version"] < 5:
        result("HARD", "block is version 5 (160 bytes) from this thread (needed to arm the CI hook)",
                False, fmt(s))
        sys.exit(1)

    # Leave a marker for "text that existed before any of this" in the document, like あ in
    # pseudo_mozc_sweep.rs's run_case (doc = "あ"). ASCII so get_text() comparisons are unambiguous.
    u32.SendMessageW(edit, 0x000C, 0, "PRE")  # WM_SETTEXT
    u32.SendMessageW(edit, 0x00B1, -1, -1)  # EM_SETSEL: caret to the end

    # ---- turn the IME on (F2, same binding composition_probe.py uses) and confirm.
    before = read(tid)
    send_vk(VK_F2)
    s = None
    for _ in range(200):
        s = read(tid)
        if s and s["eo"] == 1:
            break
        pump(0.01)
    result("HARD", "IME turned on (open=1) before the delayed-key experiment", bool(s and s["eo"] == 1), fmt(s))
    if not (s and s["eo"] == 1):
        sys.exit(1)

    # ---- the delayed key: arm the CI hook on the *next* key_seq value (the 'k' about to be sent,
    # not the F2 just sent), then send 'k' and confirm the shared memory stays frozen past the
    # deadline, even though (per session.cc, read in the design review) the server should compose
    # it within ~60ms.
    baseline = read(tid)
    target_key_seq = baseline["key_seq"] + 1
    os.environ["MOZC_CI_DELAY_AT_KEY_SEQ"] = str(target_key_seq)
    print(f"probe: info arming MOZC_CI_DELAY_AT_KEY_SEQ={target_key_seq} (baseline key_seq={baseline['key_seq']})",
          flush=True)
    t0 = time.time()
    send_vk(0x4B)  # 'k'
    frozen = True
    samples = []
    while time.time() - t0 < (MOZC_COMPOSE_CHECK_MS + 20) / 1000.0:
        s = read(tid)
        samples.append((round((time.time() - t0) * 1000), s["key_seq"] if s else None, s["phase"] if s else None))
        if s is None or s["key_seq"] != baseline["key_seq"] or s["phase"] != baseline["phase"]:
            frozen = False
            break
        pump(0.005)
    print(f"probe: info samples over {MOZC_COMPOSE_CHECK_MS + 20}ms (ms, key_seq, phase) = {samples[:3]}..{samples[-3:]}",
          flush=True)
    result("HARD", "the CI hook keeps key_seq/phase frozen past the compose-check deadline", frozen,
           f"baseline={fmt(baseline)}")
    if not frozen:
        # The hook itself did not do what it was meant to; the recovery check below would not be
        # exercising the scenario this probe exists for.
        sys.exit(1)

    # ---- awase's compose-check would now conclude Literal (judge_compose, MOZC_COMPOSE_CHECK_MS elapsed,
    # phase still 0) and fire RawTsfLiteralRecovery{backs: 1, escape_composition: false} (mozc_compose_check_fsm.rs).
    # Mirror that exactly: Backspace once (no Escape), then resend the full original romaji "ko".
    before_recovery = read(tid)
    send_vk(VK_BACK)
    send_vk(0x4B)  # resend 'k'
    send_vk(0x4F)  # resend 'o'

    # Now let suppression's gate lift (the second key-down this thread counts, the Backspace, already
    # did) and wait for the resend to actually compose.
    s = None
    for _ in range(300):
        s = read(tid)
        if s and s["phase"] == 1:
            break
        pump(0.01)
    print(f"probe: info after backspace+resend: {fmt(s)} before_recovery={fmt(before_recovery)}", flush=True)
    result("HARD", "composing resumes (phase=1) after the Backspace+resend", bool(s and s["phase"] == 1), fmt(s))

    # The Backspace (and 'k' itself, and the resent 'k'/'o') must have been consumed by the composer
    # (Session::Backspace), not passed through to the document (Session::Revert / EchoBack) -- that is
    # the whole safety claim under test. `passed` is the sharper check: if the Backspace reached the
    # document as a literal delete, `passed` grows; if the server consumed it, only `eaten` does.
    # (`before_recovery` was read while suppression was still withholding the publish, so it is the
    # same stale snapshot as `baseline` -- compare against `baseline` for the true pre-sequence counts.)
    passed_unchanged = bool(s and baseline and s["passed"] == baseline["passed"])
    eaten_grew = bool(s and baseline and s["eaten"] > baseline["eaten"])
    result("HARD", "none of k / Backspace / resent k,o were passed through to the document (all eaten)",
           passed_unchanged, f"passed {baseline['passed'] if baseline else None} -> {s['passed'] if s else None}")
    result("HARD", "the server did eat something (k / Backspace / resend all occurred)",
           eaten_grew, f"eaten {baseline['eaten'] if baseline else None} -> {s['eaten'] if s else None}")

    # ---- commit and check the final text: the marker must survive, and no literal 'k'/'o' may have leaked.
    send_vk(0x0D)  # Enter: commit
    pump(0.3)
    text = get_text(edit)
    print(f"probe: info final text = {text!r}", flush=True)
    result("HARD", "the pre-existing text ('PRE') survived the recovery", text.startswith("PRE"), f"text={text!r}")
    result("HARD", "no literal k/o leaked into the document", "k" not in text and "o" not in text.lower()[3:],
           f"text={text!r}")


try:
    main()
except SystemExit:
    raise
except BaseException:
    import traceback
    traceback.print_exc()
    sys.exit(1)
