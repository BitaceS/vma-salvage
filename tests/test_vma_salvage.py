# tiny fake .vma files, same layout as pve-qemu vma-writer.c
import io
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import vma_salvage as vs  # noqa: E402

HEADER_SIZE = 12288
BLOB_OFF = 8192


def blob_add(blob, data):
    # pos 0 = unset, so the table starts at 1 (same as vma-writer)
    if not blob:
        blob.append(0)
    pos = len(blob)
    blob += struct.pack("<H", len(data)) + data
    return pos


def make_vma(disks, configs, data_by_dev):
    # disks {dev: (name, size)}, data_by_dev {dev: {block_no: 4k bytes}}
    blob = bytearray()
    head = bytearray(HEADER_SIZE)
    head[0:4] = b"VMA\x00"
    struct.pack_into(">I", head, 4, 1)
    struct.pack_into(">Q", head, 24, 1700000000)
    for i, (name, data) in enumerate(configs):
        struct.pack_into(
            ">I", head, vs.CONFIG_NAMES_OFF + i * 4, blob_add(blob, name.encode() + b"\x00")
        )
        struct.pack_into(">I", head, vs.CONFIG_DATA_OFF + i * 4, blob_add(blob, data))
    for dev_id, (name, size) in disks.items():
        ptr = blob_add(blob, name.encode() + b"\x00")
        struct.pack_into(">IIQ", head, vs.DEV_INFO_OFF + dev_id * 16, ptr, 0, size)
    struct.pack_into(">I", head, 48, BLOB_OFF)
    struct.pack_into(">I", head, 52, len(blob))
    struct.pack_into(">I", head, 56, HEADER_SIZE)
    head[BLOB_OFF : BLOB_OFF + len(blob)] = blob

    # lazy: one extent per cluster
    out = bytearray(head)
    for dev_id, blocks in data_by_dev.items():
        clusters = {}
        for blk, data in blocks.items():
            clusters.setdefault(blk // 16, {})[blk % 16] = data
        for cluster, members in sorted(clusters.items()):
            mask = 0
            body = bytearray()
            for b in range(16):
                if b in members:
                    mask |= 1 << b
                    body += members[b]
            eh = bytearray(vs.EXTENT_HEADER)
            eh[0:4] = b"VMAE"
            struct.pack_into(">H", eh, 6, bin(mask).count("1"))
            bi = (mask << 48) | (dev_id << 32) | cluster
            struct.pack_into(">Q", eh, vs.BLOCKINFO_OFF, bi)
            out += eh + body
    return bytes(out)


def block(ch):
    return bytes([ch]) * vs.BLOCK


class VmaSalvageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.disks = {1: ("drive-scsi0", 4 * vs.CLUSTER)}
        self.blocks = {0: block(1), 5: block(2), 17: block(3), 40: block(4)}
        self.vma = make_vma(
            self.disks, [("qemu-server.conf", b"cores: 2\nmemory: 4096\n")], {1: self.blocks}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def read_disk(self):
        with open(os.path.join(self.out, "disk-drive-scsi0.raw"), "rb") as fh:
            return fh.read()

    def test_full_backup(self):
        stats = vs.extract(io.BytesIO(self.vma), self.out, log=lambda m: None)
        self.assertTrue(stats["complete"])
        disk = self.read_disk()
        self.assertEqual(len(disk), 4 * vs.CLUSTER)
        for blk, data in self.blocks.items():
            self.assertEqual(disk[blk * vs.BLOCK : (blk + 1) * vs.BLOCK], data)
        self.assertEqual(disk[vs.BLOCK : 2 * vs.BLOCK], bytes(vs.BLOCK))  # hole
        with open(os.path.join(self.out, "qemu-server.conf"), "rb") as fh:
            self.assertEqual(fh.read(), b"cores: 2\nmemory: 4096\n")

    def test_truncated_keeps_everything_before_the_cut(self):
        # cut inside block 40
        cut = self.vma[: len(self.vma) - vs.BLOCK // 2]
        stats = vs.extract(io.BytesIO(cut), self.out, log=lambda m: None)
        self.assertFalse(stats["complete"])
        self.assertIn("data block", stats["reason"])
        disk = self.read_disk()
        for blk in (0, 5, 17):
            self.assertEqual(disk[blk * vs.BLOCK : (blk + 1) * vs.BLOCK], self.blocks[blk])
        self.assertEqual(disk[40 * vs.BLOCK : 41 * vs.BLOCK], bytes(vs.BLOCK))

    def test_mismatched_dev_id_with_single_disk(self):
        vma = make_vma(self.disks, [], {2: self.blocks})
        stats = vs.extract(io.BytesIO(vma), self.out, log=lambda m: None)
        self.assertTrue(stats["complete"])
        disk = self.read_disk()
        self.assertEqual(disk[17 * vs.BLOCK : 18 * vs.BLOCK], self.blocks[17])

    def test_two_disks(self):
        disks = {1: ("drive-scsi0", vs.CLUSTER), 2: ("drive-scsi1", vs.CLUSTER)}
        vma = make_vma(disks, [], {1: {3: block(7)}, 2: {3: block(9)}})
        vs.extract(io.BytesIO(vma), self.out, log=lambda m: None)
        for name, ch in (("drive-scsi0", 7), ("drive-scsi1", 9)):
            with open(os.path.join(self.out, "disk-%s.raw" % name), "rb") as fh:
                self.assertEqual(fh.read()[3 * vs.BLOCK : 4 * vs.BLOCK], block(ch))

    def test_list_and_bad_magic(self):
        lines = []
        vs.list_header(io.BytesIO(self.vma), out=lines.append)
        self.assertTrue(any("drive-scsi0" in l for l in lines))
        self.assertTrue(any("qemu-server.conf" in l for l in lines))
        with self.assertRaises(ValueError):
            vs.extract(io.BytesIO(b"nope" * 4000), self.out, log=lambda m: None)

    def test_cli_exit_codes(self):
        path = os.path.join(self.out, "b.vma")
        with open(path, "wb") as fh:
            fh.write(self.vma[:-100])
        self.assertEqual(vs.main([path, os.path.join(self.out, "x")]), 1)
        with open(path, "wb") as fh:
            fh.write(self.vma)
        self.assertEqual(vs.main([path, os.path.join(self.out, "y")]), 0)


if __name__ == "__main__":
    unittest.main()
