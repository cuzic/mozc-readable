"""CI helper: write a Mozc-format config1.db that gives F8 a meaning the default keymap does not have.

usage: config_make.py <path to config1.db>

The file holds two fields of Mozc's `Config` message (src/protocol/config.proto):
  field 41  session_keymap       = CUSTOM (0)       (varint)
  field 42  custom_keymap_table  = a TSV keymap     (length-delimited bytes)
The keymap: F8 turns the IME on while it is closed (DirectInput) and off while it is open (Precomposition).
The default keymap (MS-IME) has no F8 binding in either state, so a state change on F8 proves that the running
server read this file, from this path.

awase has a regression test with the same bytes (crates/awase-gji-config/src/lib.rs,
`a_mozc_config_with_a_custom_f8_toggle_is_read_as_a_toggle_key`); keep the two in sync.
"""
import os
import sys

TABLE = ("status\tkey\tcommand\n"
         "DirectInput\tF8\tIMEOn\n"
         "Precomposition\tF8\tIMEOff\n")


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def tag(field: int, wire_type: int) -> bytes:
    return varint((field << 3) | wire_type)


def build() -> bytes:
    table = TABLE.encode("ascii")
    return (tag(41, 0) + varint(0)                       # session_keymap = CUSTOM
            + tag(42, 2) + varint(len(table)) + table)   # custom_keymap_table


def main() -> None:
    path = sys.argv[1]
    data = build()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print(f"config_make: wrote {len(data)} bytes to {path}")
    print("config_make: hex " + data.hex())


if __name__ == "__main__":
    main()
