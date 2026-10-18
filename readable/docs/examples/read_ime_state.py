"""mozc-readable の共有メモリから、いまの IME の開閉・入力モードを読んで表示する最小例。

仕様: readable/docs/shared-memory.md
動作: Windows のみ。標準ライブラリだけ(ctypes)。mozc-readable をインストールして、
      Mozc Readable を入力方式にした窓へ入力してから動かす。
      実行: uv run read_ime_state.py   (停止は Ctrl+C)

**未検証**: この例は Linux 上で構文だけ確認したもので、Windows 実機では動かしていない。
AppContainer(UWP など)の窓は読めない(`NtOpenSection` が要る。仕様書 4-4 章)。version 2 以降のビルドが前提で、
version 3(80 バイト)なら組成の状態(phase など)も表示する。
動かして食い違いがあれば、仕様書(と実装)が正。
"""
import ctypes
import struct
import sys
import time
from ctypes import wintypes

MAGIC = 0x5A52444D
STRUCT = struct.Struct("<10IQ")  # u32 x10 + u64 = 48 バイト(TipSharedState、version 2 の部分)
STRUCT3 = struct.Struct("<10IQ8I")  # version 3 は末尾に u32 x8 を足した 80 バイト
assert STRUCT.size == 48 and STRUCT3.size == 80
PHASES = {0: "組成なし", 1: "組成中", 2: "変換中"}
CATEGORIES = {0: "なし", 1: "サジェスト", 2: "予測", 3: "変換"}  # Mozc の Category の数値とは違う。仕様書 2 章の表のとおり


def i32(x):
    """u32 の差を符号付きで読む。ラップしても大小を正しく比べられる(仕様書: 鮮度)。"""
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x & 0x80000000 else x


def composition_is_fresh(composition_key_seq, my_key_seq):
    """組成の項目(phase など)が、自分が送った打鍵 my_key_seq まで反映済みか。この例はキーを送らないので main では使わない。
    使い方: キーを送る前に読んだ key_seq に、送った数を足したものを my_key_seq にする(目安。仕様書 2 章)。"""
    return i32(composition_key_seq - my_key_seq) >= 0

MODES = {0: "直接入力", 1: "ひらがな", 2: "全角カタカナ", 3: "半角英数", 4: "全角英数", 5: "半角カタカナ"}

FILE_MAP_READ = 0x0004
SYNCHRONIZE = 0x00100000

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.OpenFileMappingW.restype = wintypes.HANDLE
k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.OpenEventW.restype = wintypes.HANDLE
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.GetWindowThreadProcessId.restype = wintypes.DWORD


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def focus_thread():
    """キーボードフォーカスを持つ窓のスレッド ID とプロセス ID。前面窓のスレッドとは限らない。"""
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if not u32.GetGUIThreadInfo(0, ctypes.byref(info)) or not info.hwndFocus:
        return None
    pid = wintypes.DWORD(0)
    tid = u32.GetWindowThreadProcessId(info.hwndFocus, ctypes.byref(pid))
    return (tid, pid.value) if tid else None


def read_state(tid, pid):
    """条件を満たしたときだけ辞書を返す。読めない・信用できないときは None(= 「不明」として扱う)。"""
    mapping = k32.OpenFileMappingW(FILE_MAP_READ, False, f"Local\\mozc-readable-ime-{tid}")
    if not mapping:
        return None  # その窓のスレッドでは mozc-readable が動いていない(または名前空間が違う)
    # サイズ 0 = セクション全体を map する。80 固定だと、48 バイトの旧ブロックの開閉・モードまで読めなくなる(仕様書 4-1)。
    view = k32.MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0)
    try:
        if not view:
            return None
        for _ in range(8):  # seqlock: 奇数は書き込み中。前後の seq が一致したときだけ採る
            seq1 = struct.unpack("<I", ctypes.string_at(view + 8, 4))[0]
            if seq1 % 2:
                continue
            raw = ctypes.string_at(view, STRUCT3.size)
            seq2 = struct.unpack("<I", ctypes.string_at(view + 8, 4))[0]
            if seq1 != seq2:
                continue
            magic, version, seq, eo, em, to, tm, btid, bpid, active, tick = STRUCT.unpack(raw[:STRUCT.size])
            if magic != MAGIC or seq == 0:  # seq==0: 作った直後でまだ書かれていない
                return None
            if bpid != pid:  # スレッド ID は再利用される。別プロセスの古いブロックを読まない
                return None
            if version < 2 or not active:  # v1 は「いま使われているか」が分からない。active=0 は他の IME に切り替え済み
                return None
            st = {"version": version, "open": bool(to), "mode": MODES.get(tm, f"?{tm}"),
                  "effective_open": bool(eo), "effective_mode": MODES.get(em, f"?{em}")}
            if version >= 3:  # active は上で確かめ済み。0 のときは version 3 の項目を含め何も採らない
                (_, _, _, _, _, _, _, _, _, _, _,
                 phase, cat, plen, pcur, key_seq, sfm, srv, cks) = STRUCT3.unpack(raw)
                st.update({"phase": PHASES.get(phase, f"?{phase}"), "candidate": CATEGORIES.get(cat, f"?{cat}"),
                           "preedit_utf16_len": plen, "preedit_cursor_utf32": pcur, "key_seq": key_seq,
                           # 1 は「IME を閉じる」指定(effective_mode は変わらない)。2 以降はモード番号 + 1
                           "scope_forced_mode": sfm, "server_ready": srv, "composition_key_seq": cks})
            return st
        return None
    finally:
        if view:
            k32.UnmapViewOfFile(view)
        k32.CloseHandle(mapping)  # 持ち続けない: 書き出し側が終わっても古いブロックが残る


def main():
    # 変化の合図(pulse)。取りこぼすので、起きるたびに読み直す。無くても 1 秒ごとに読む。
    ev = k32.OpenEventW(SYNCHRONIZE, False, "Local\\mozc-readable-ime-changed")
    last = None
    while True:
        ft = focus_thread()
        st = read_state(*ft) if ft else None
        if st is None:
            line = "不明(この窓では読めない)"
        else:
            line = f"{'開' if st['open'] else '閉'} / {st['mode']}"
            if st["version"] >= 3:
                line += f" / {st['phase']} 候補={st['candidate']} 長さ={st['preedit_utf16_len']}"
                # QMK へ送るなら、ここで HID のレポートを組む(phase / candidate / scope_forced_mode を 1 バイトずつ。
                # スケッチのまま・実機未検証。仕様書の「キーボードへ送る」参照)
        if line != last:  # key_seq は打鍵のたびに変わるので、表示する内容が変わったときだけ出す
            last = line
            print(line)
        if not ev:  # TIP がまだ一度も書いていないと開けない。開けるまで毎回やり直す
            ev = k32.OpenEventW(SYNCHRONIZE, False, "Local\\mozc-readable-ime-changed")
        if ev:
            k32.WaitForSingleObject(ev, 1000)
        else:
            time.sleep(0.2)


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("Windows のみ")
    main()
