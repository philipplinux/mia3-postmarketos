#!/usr/bin/env python3
# Minimal ath10k board-2.bin encoder (format: drivers/net/wireless/ath/ath10k/hw.h)
import struct, sys

MAGIC = b"QCA-ATH10K-BOARD\0"
IE_BOARD, IE_NAME, IE_DATA = 0, 0, 1

def pad4(b): return b + b"\0" * (-len(b) % 4)
def ie(i, data): return pad4(struct.pack("<II", i, len(data)) + data)

def encode(entries):
    out = pad4(MAGIC)
    for names, data in entries:
        out += ie(IE_BOARD, b"".join(ie(IE_NAME, n.encode()) for n in names) + ie(IE_DATA, data))
    return out

def decode(blob):
    assert blob.startswith(MAGIC)
    off, res = len(pad4(MAGIC)), []
    while off < len(blob):
        i, l = struct.unpack_from("<II", blob, off); body = blob[off+8:off+8+l]; off += 8 + l + (-l % 4)
        names, data, o = [], None, 0
        while o < len(body):
            si, sl = struct.unpack_from("<II", body, o); v = body[o+8:o+8+sl]; o += 8 + sl + (-sl % 4)
            if si == IE_NAME: names.append(v.decode())
            else: data = v
        res.append((names, data))
    return res

def board_id(path):
    # icnss naming: bdwlan.bin = board id 0xff (default), bdwlan.bXX = 0xXX, bdwlan.XXX = 0xXXX
    ext = path.rsplit(".", 1)[1]
    return 0xff if ext == "bin" else int(ext[1:] if ext[0] == "b" and len(ext) == 3 else ext, 16)

if __name__ == "__main__":
    *srcs, dst = sys.argv[1:]   # every stock bdwlan.* file: a unit with another board id still finds its data
    entries = [([f"bus=snoc,qmi-board-id={board_id(f):x},qmi-chip-id=120", f"bus=snoc,qmi-board-id={board_id(f):x}"],
                open(f, "rb").read()) for f in sorted(srcs)]
    assert len({e[0][0] for e in entries}) == len(entries), "two files map to one board id"
    assert board_id("bdwlan.b04") == 0x4 and board_id("bdwlan.102") == 0x102 and board_id("bdwlan.bin") == 0xff
    blob = encode(entries)
    assert decode(blob) == entries  # round-trip self-check
    open(dst, "wb").write(blob)
    print(f"{dst}: {len(blob)} bytes, {len(entries)} boards")
