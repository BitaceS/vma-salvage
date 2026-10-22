# vma-salvage

Pulls the disks out of a Proxmox `.vma` backup. Works on truncated/broken backups too, where `vma extract` just errors out.

I wrote this when a Proxmox host was gone and the only copy of a Nextcloud VM was a vzdump backup whose upload stopped at 54 of about 65 GB. The official tool gives up on a file like that. This one reads everything up to the point where the file breaks and writes it into a normal raw disk image. After an `fsck` on a copy, the Nextcloud was running again with most of its files.

Single Python file, no dependencies, Python 3.8+.

## Usage

```sh
# what's inside (disks + VM config), reads only the header
python3 vma_salvage.py --list vzdump-qemu-100.vma

# extract
python3 vma_salvage.py vzdump-qemu-100.vma out/

# compressed backups: stream through zstd, no huge temp file needed
zstd -dc --long=31 vzdump-qemu-100.vma.zst | python3 vma_salvage.py - out/

# straight from remote storage (e.g. a Hetzner Storage Box)
ssh -p23 user@box "cat backups/vzdump-qemu-100.vma.zst" \
  | zstd -dc --long=31 | python3 vma_salvage.py - out/
```

For a truncated `.zst`, zstd will complain at the end. Ignore it, everything before that still comes through.

You get:

- `out/disk-<name>.raw`, one sparse raw image per disk
- `out/qemu-server.conf` (and any other config stored in the backup)

Exit code `0` = backup read to the end, `1` = backup incomplete but everything before the break was saved, `2` = not a VMA file.

## After extracting a broken backup

Work on a copy, never on your only image.

```sh
cp --sparse=always out/disk-drive-scsi0.raw work.raw
losetup -fP --show work.raw          # e.g. /dev/loop0
fsck -y /dev/loop0p1                 # repair the copy
mount -o ro /dev/loop0p1 /mnt/rescue
```

Files from the missing part of the disk are gone, but they often land in `lost+found` partly, so look there too.

## Notes

- Disk images are sparse. Make sure the target filesystem supports that, or a 80 GB disk takes 80 GB.
- If the disk id in the data does not match the header (I have seen that once), and the VM has only one disk, the data goes to that disk anyway. With several disks, unmatched blocks are skipped and counted in a warning.
- Tested against synthetic VMA files in `tests/` and in real recoveries. If it fails on your backup, open an issue with the output of `--list`.

```sh
python3 -m unittest discover -s tests
```

## Need help?

If your backup is broken and the data matters, I do this as a service: [sudowl.com/en/services/datenrettung](https://sudowl.com/en/services/datenrettung)

## License

MIT
