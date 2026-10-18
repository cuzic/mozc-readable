"""CI helper: pairs each key send (ui_host) with the first state change the reader saw afterwards.

usage: summarize.py <ui_host.out> <reader.out>   -> prints latency per key; exit 1 if a key
produced no change (except when its state did not change on purpose) or latency > 1000 ms.
"""
import re
import sys

sends = [(m.group(1), int(m.group(2))) for l in open(sys.argv[1], errors="replace")
         if (m := re.search(r"send vk=(\S+) t_ns=(\d+)", l))]
changes = [(int(m.group(1)), int(m.group(2)), int(m.group(3))) for l in open(sys.argv[2], errors="replace")
           if (m := re.search(r"eff_open=(\d) eff_mode=(\d).*t_ns=(\d+)", l))]
changes = [(t, o, md) for o, md, t in changes]
# keep only changes of the effective (open, mode) pair; tsf-only changes are not what a key press means
dedup, prev = [], None
for t, o, md in changes:
    if (o, md) != prev:
        dedup.append((t, o, md))
        prev = (o, md)
changes = dedup
bad = 0
lat = []
for vk, t0 in sends:
    after = [c for c in changes if c[0] >= t0]
    if not after:
        print(f"vk={vk}: no state change observed")
        bad += 1
        continue
    t1, o, md = after[0]
    ms = (t1 - t0) / 1e6
    lat.append(ms)
    print(f"vk={vk}: key sent -> reader saw open={o} mode={md} after {ms:.1f} ms")
if lat:
    print(f"latency ms: min={min(lat):.1f} max={max(lat):.1f}")
    if max(lat) > 1000:
        bad += 1
sys.exit(1 if bad else 0)
