#!/usr/bin/env python3
"""Builds a small XDVDFS image (XGD2 layout by default) to test xdvdfs.py.
usage: make_test_iso.py out.iso [base_offset]"""
import struct, sys
SECTOR = 0x800
MAGIC = b'MICROSOFT*XBOX*MEDIA'

def build(tree, base):
    data = {}                      # sector -> bytes
    next_sec = [34]
    def alloc(n):
        s = next_sec[0]; next_sec[0] += max(1, (n + SECTOR - 1) // SECTOR); return s
    def write_dir(items):
        names = sorted(items)
        # balanced binary tree over sorted names
        recs, offsets = [], {}
        def place(lo, hi):
            if lo > hi: return None
            mid = (lo + hi) // 2
            idx = len(recs); recs.append([names[mid], None, None]); 
            recs[idx][1] = place(lo, mid - 1); recs[idx][2] = place(mid + 1, hi)
            return idx
        place(0, len(names) - 1)
        # compute offsets (4-byte aligned)
        off = 0
        for r in recs:
            offsets[id(r)] = off
            off += (14 + len(r[0]) + 3) & ~3
        size = max(off, 1)
        out = bytearray(b'\xff' * ((size + SECTOR - 1) // SECTOR * SECTOR))
        sec = alloc(size)
        for r in recs:
            name, l, rt = r
            v = items[name]
            if isinstance(v, dict):
                s2, z2 = write_dir(v); attr = 0x10
            else:
                z2 = len(v); s2 = alloc(z2); data[s2] = v; attr = 0x20
            lo = offsets[id(recs[l])] // 4 if l is not None else 0
            ro = offsets[id(recs[rt])] // 4 if rt is not None else 0
            o = offsets[id(r)]
            out[o:o + 14 + len(name)] = struct.pack('<HHIIBB', lo, ro, s2, z2, attr, len(name)) + name.encode()
        data[sec] = bytes(out)
        return sec, size
    root_sec, root_size = write_dir(tree)
    end = max(s + (len(b) + SECTOR - 1) // SECTOR for s, b in data.items())
    img = bytearray(base + end * SECTOR)
    hdr = bytearray(SECTOR)
    hdr[:20] = MAGIC; hdr[20:28] = struct.pack('<II', root_sec, root_size); hdr[0x7EC:0x800] = MAGIC
    img[base + 32 * SECTOR: base + 33 * SECTOR] = hdr
    for s, b in data.items():
        img[base + s * SECTOR: base + s * SECTOR + len(b)] = b
    return img

tree = {'default.xex': b'XEX2' + b'\0' * 100, 'media': {'a.bin': b'A' * 5000, 'b.bin': b'B' * 10,
        'sub': {'deep.txt': b'hello'}}, 'zz.txt': b'zz', 'aa.txt': b'aa' * 3000, 'mm.dat': b'm'}
base = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0xFDA0000
open(sys.argv[1], 'wb').write(build(tree, base))
