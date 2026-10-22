#!/usr/bin/env python3
"""
vma-salvage - dump disks out of proxmox .vma backups, also truncated ones.

  vma_salvage.py --list backup.vma
  vma_salvage.py backup.vma out/
  zstd -dc --long=31 backup.vma.zst | vma_salvage.py - out/

format: pve-qemu vma.h (everything big endian except blob string length)
"""

import argparse
import os
import struct
import sys

__version__ = "0.1.0"

# vma.h
CLUSTER = 65536
BLOCK = 4096
HEADER_MIN = 8192
EXTENT_HEADER = 512
BLOCKINFO_OFF = 40
BLOCKINFO_COUNT = 59
CONFIG_NAMES_OFF = 2044
CONFIG_DATA_OFF = 3068
DEV_INFO_OFF = 4096


def read_exact(fh, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = fh.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


class Header:
    def __init__(self, raw):
        self.raw = raw
        self.version = struct.unpack_from(">I", raw, 4)[0]
        self.ctime = struct.unpack_from(">Q", raw, 24)[0]
        self.blob_off = struct.unpack_from(">I", raw, 48)[0]
        self.blob_size = struct.unpack_from(">I", raw, 52)[0]
        self.size = struct.unpack_from(">I", raw, 56)[0]
        self.blob = raw[self.blob_off : self.blob_off + self.blob_size]

    def blob_bytes(self, ptr):
        # ptr is relative to blob_buffer_offset, 0 = not set
        if ptr == 0 or ptr >= len(self.blob):
            return None
        ln = struct.unpack_from("<H", self.blob, ptr)[0]
        return self.blob[ptr + 2 : ptr + 2 + ln]

    def blob_str(self, ptr):
        data = self.blob_bytes(ptr)
        if data is None:
            return None
        return data.rstrip(b"\x00").decode("utf-8", "replace")

    def devices(self):
        devs = {}
        for i in range(256):
            name_ptr, _res, size = struct.unpack_from(">IIQ", self.raw, DEV_INFO_OFF + i * 16)
            if name_ptr and size:
                devs[i] = (self.blob_str(name_ptr) or "dev%d" % i, size)
        return devs

    def configs(self):
        out = []
        for i in range(256):
            name_ptr = struct.unpack_from(">I", self.raw, CONFIG_NAMES_OFF + i * 4)[0]
            data_ptr = struct.unpack_from(">I", self.raw, CONFIG_DATA_OFF + i * 4)[0]
            if name_ptr and data_ptr:
                name = self.blob_str(name_ptr)
                data = self.blob_bytes(data_ptr)
                if name and data is not None:
                    out.append((name, data.rstrip(b"\x00")))
        return out


def read_header(src):
    head = read_exact(src, HEADER_MIN)
    if len(head) < HEADER_MIN or head[:4] != b"VMA\x00":
        raise ValueError("not a VMA stream (magic=%r)" % head[:4])
    header_size = struct.unpack_from(">I", head, 56)[0]
    if header_size > HEADER_MIN:
        rest = read_exact(src, header_size - HEADER_MIN)
        if len(rest) < header_size - HEADER_MIN:
            raise ValueError("stream ends inside the header")
        head += rest
    return Header(head)


def safe_name(name):
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return keep.lstrip(".") or "unnamed"


def extract(src, outdir, log=print):
    hdr = read_header(src)
    devs = hdr.devices()
    if not devs:
        raise ValueError("no disks in header")
    os.makedirs(outdir, exist_ok=True)

    for name, data in hdr.configs():
        path = os.path.join(outdir, safe_name(name))
        with open(path, "wb") as fh:
            fh.write(data)
        log("config: %s" % path)

    handles = {}
    for dev_id, (name, size) in devs.items():
        path = os.path.join(outdir, "disk-%s.raw" % safe_name(name))
        fh = open(path, "wb")
        fh.truncate(size)
        handles[dev_id] = fh
        log("disk %d: %s  %.1f GiB -> %s" % (dev_id, name, size / 2**30, path))

    stats = {"extents": 0, "bytes": 0, "complete": False, "reason": "", "unmapped": 0}
    only = next(iter(handles.values())) if len(handles) == 1 else None
    try:
        while True:
            eh = read_exact(src, EXTENT_HEADER)
            if not eh:
                stats["complete"] = True
                stats["reason"] = "end of stream"
                break
            if len(eh) < EXTENT_HEADER:
                stats["reason"] = "stream ends inside an extent header"
                break
            if eh[:4] != b"VMAE":
                stats["reason"] = "bad extent magic %r" % eh[:4]
                break
            for idx in range(BLOCKINFO_COUNT):
                bi = struct.unpack_from(">Q", eh, BLOCKINFO_OFF + idx * 8)[0]
                if bi == 0:
                    continue
                mask = (bi >> 48) & 0xFFFF
                dev = (bi >> 32) & 0xFF
                cluster = bi & 0xFFFFFFFF
                # had a backup where blockinfo said dev 1 but the header had
                # the disk at index 2. single disk -> just write there.
                # always consume the blocks, otherwise the stream is off
                fh = handles.get(dev, only)
                if fh is None:
                    stats["unmapped"] += 1
                base = cluster * CLUSTER
                for b in range(16):
                    if not mask & (1 << b):
                        continue
                    data = read_exact(src, BLOCK)
                    if len(data) < BLOCK:
                        stats["reason"] = "stream ends inside a data block"
                        return stats
                    if fh is not None:
                        fh.seek(base + b * BLOCK)
                        fh.write(data)
                        stats["bytes"] += BLOCK
            stats["extents"] += 1
            if stats["extents"] % 2000 == 0:
                log("  %d extents, %.1f GiB written" % (stats["extents"], stats["bytes"] / 2**30))
    finally:
        for fh in handles.values():
            fh.close()
    return stats


def list_header(src, out=print):
    hdr = read_header(src)
    out("VMA version %d, header %d bytes, ctime %d" % (hdr.version, hdr.size, hdr.ctime))
    for dev_id, (name, size) in hdr.devices().items():
        out("disk %d: %s  %d bytes (%.1f GiB)" % (dev_id, name, size, size / 2**30))
    for name, data in hdr.configs():
        out("config: %s (%d bytes)" % (name, len(data)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract disks from (truncated) Proxmox .vma backups.")
    ap.add_argument("input", help=".vma file, or - for stdin (e.g. from zstd -dc --long=31)")
    ap.add_argument("outdir", nargs="?", help="output directory for disk-*.raw and config files")
    ap.add_argument(
        "--list", action="store_true", help="only print disks and configs from the header"
    )
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = ap.parse_args(argv)

    src = sys.stdin.buffer if args.input == "-" else open(args.input, "rb")
    try:
        if args.list:
            list_header(src)
            return 0
        if not args.outdir:
            ap.error("outdir is required unless --list is given")
        stats = extract(src, args.outdir, log=lambda m: print(m, flush=True))
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    finally:
        if src is not sys.stdin.buffer:
            src.close()

    print("%d extents, %.2f GiB of data written" % (stats["extents"], stats["bytes"] / 2**30))
    if stats["unmapped"]:
        print("warning: skipped %d block groups with unknown disk id" % stats["unmapped"])
    if stats["complete"]:
        print("backup read to the end")
        return 0
    print("backup incomplete: %s (kept everything up to there)" % stats["reason"])
    print("fsck a copy of the image, not the original")
    return 1


if __name__ == "__main__":
    sys.exit(main())
