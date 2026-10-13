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

if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    data = open(src, "rb").read()
    names = ["bus=snoc,qmi-board-id=ff,qmi-chip-id=120", "bus=snoc,qmi-board-id=ff"]
    blob = encode([(names, data)])
    assert decode(blob) == [(names, data)]  # round-trip self-check
    open(dst, "wb").write(blob)
    print(f"{dst}: {len(blob)} bytes, names={names}")
