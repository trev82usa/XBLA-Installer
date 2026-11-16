#!/usr/bin/env python3
"""Reader for Xbox / Xbox 360 disc images (XDVDFS: original Xbox, XGD2 and XGD3 ISOs).
Usage: xdvdfs.py <image.iso> <outdir> [--list]

Finds the game partition (plain, XGD1, XGD2 or XGD3 layout), walks the directory trees and
copies every file out. No decryption involved: XDVDFS game partitions are plain data.
"""
import os
import struct
import sys

SECTOR = 0x800
MAGIC = b'MICROSOFT*XBOX*MEDIA'
# Where the game partition starts for each disc layout.
LAYOUTS = (('plain image', 0x0), ('XGD3 (Xbox 360)', 0x2080000), ('XGD2 (Xbox 360)', 0xFDA0000),
           ('XGD1 (original Xbox)', 0x18300000))


class XDVDFS:
    def __init__(self, path):
        self.f = open(path, 'rb')
        self.size = os.path.getsize(path)
        for name, off in LAYOUTS:
            if off + 0x10000 + 0x800 > self.size:
                continue
            self.f.seek(off + 32 * SECTOR)
            h = self.f.read(SECTOR)
            if h[:20] == MAGIC and h[0x7EC:0x800] == MAGIC:
                self.layout, self.base = name, off
                self.root_sector, self.root_size = struct.unpack('<II', h[20:28])
                return
        raise ValueError("not an Xbox disc image (no XDVDFS volume found)")

    def read(self, sector, size, offset=0):
        self.f.seek(self.base + sector * SECTOR + offset)
        return self.f.read(size)

    def entries(self):
        """Yield (path, is_dir, sector, size) for every file and folder."""
        stack = [(self.root_sector, self.root_size, '')]
        seen = set()
        while stack:
            sector, size, prefix = stack.pop()
            if not size or (sector, size) in seen:
                continue
            seen.add((sector, size))
            table = self.read(sector, size)
            todo = [0]
            visited = set()
            while todo:
                off = todo.pop()
                if off in visited or off + 14 > len(table):
                    continue
                visited.add(off)
                left, right, sec, sz, attr, nlen = struct.unpack('<HHIIBB', table[off:off + 14])
                if left == 0xFFFF and right == 0xFFFF and sec == 0xFFFFFFFF:
                    continue                      # padding
                name = table[off + 14:off + 14 + nlen].decode('latin-1')
                if left:
                    todo.append(left * 4)
                if right:
                    todo.append(right * 4)
                if not name:
                    continue
                path = prefix + name
                if attr & 0x10:
                    yield path, True, sec, sz
                    stack.append((sec, sz, path + '/'))
                else:
                    yield path, False, sec, sz


def extract(path, out, log=print, progress=None):
    x = XDVDFS(path)
    ents = list(x.entries())
    total = sum(e[3] for e in ents if not e[1]) or 1
    done = 0
    for p, is_dir, sec, sz in ents:
        dest = os.path.join(out, *p.split('/'))
        if is_dir:
            os.makedirs(dest, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as fo:
            x.f.seek(x.base + sec * SECTOR)
            left = sz
            while left:
                chunk = x.f.read(min(left, 1 << 20))
                if not chunk:
                    raise IOError(f"image ends early while reading {p}")
                fo.write(chunk)
                left -= len(chunk)
        done += sz
        if progress:
            progress(p, done * 100 // total)
    return x, ents


if __name__ == '__main__':
    x = XDVDFS(sys.argv[1])
    print(f"{x.layout}, root sector {x.root_sector}")
    if '--list' in sys.argv:
        for p, d, s, z in x.entries():
            print(f"{'D' if d else 'F'} {z:>11} {p}")
    else:
        extract(sys.argv[1], sys.argv[2], progress=lambda p, pc: print(f"{pc:3}% {p}"))
