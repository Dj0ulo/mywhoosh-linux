#!/usr/bin/python3
"""
Patch WindowsConnectivity.dll to bypass the Bluetooth state check.

Without this patch MyWhoosh would crash on launch.

Instead of hardcoding a file offset (which breaks on every MyWhoosh update),
this script parses the PE/CLI metadata and locates
`BluetoothManager.BluetoothProgram::IsBluetoothEnabled` by name, then rewrites
its IL body to `ldc.i4.1; ret` (always return true).
"""

import struct
import sys

TARGET_TYPE = "BluetoothManager.BluetoothProgram"
TARGET_METHOD = "IsBluetoothEnabled"

PATCHED = bytes([0x17, 0x2A])  # ldc.i4.1; ret


class MetadataError(Exception):
    pass


class Assembly:
    """Minimal .NET assembly reader: enough to find a method body offset."""

    # Coded index definitions: (tables, tag_bits)
    TYPE_DEF_OR_REF = ([0x02, 0x01, 0x1B], 2)
    RESOLUTION_SCOPE = ([0x00, 0x1A, 0x23, 0x01], 2)

    def __init__(self, data: bytes):
        self.data = data
        self._parse_pe()
        self._parse_metadata()
        self._parse_tables()

    # -- PE ---------------------------------------------------------------
    def _parse_pe(self):
        data = self.data
        if data[:2] != b"MZ":
            raise MetadataError("not a PE file")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise MetadataError("bad PE signature")

        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        optsz = struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        # DataDirectory follows the optional header windows-specific fields
        datadir = opt + (96 if magic == 0x10B else 112)

        self.sections = []
        secoff = opt + optsz
        for i in range(nsec):
            o = secoff + i * 40
            vsize, va, rsize, raw = struct.unpack_from("<IIII", data, o + 8)
            self.sections.append((va, max(vsize, rsize), raw))

        self.cli_rva = struct.unpack_from("<I", data, datadir + 14 * 8)[0]
        if not self.cli_rva:
            raise MetadataError("no CLI header (not a managed assembly)")

    def rva_to_offset(self, rva: int) -> int:
        for va, size, raw in self.sections:
            if va <= rva < va + size:
                return raw + (rva - va)
        raise MetadataError(f"RVA 0x{rva:x} not mapped to any section")

    # -- Metadata root / streams ------------------------------------------
    def _parse_metadata(self):
        data = self.data
        cli = self.rva_to_offset(self.cli_rva)
        md_rva = struct.unpack_from("<I", data, cli + 8)[0]
        md = self.rva_to_offset(md_rva)
        if data[md:md + 4] != b"BSJB":
            raise MetadataError("bad metadata signature")

        version_len = struct.unpack_from("<I", data, md + 12)[0]
        p = md + 16 + version_len + 2  # +2: flags
        nstreams = struct.unpack_from("<H", data, p)[0]
        p += 2

        self.streams = {}
        for _ in range(nstreams):
            off, size = struct.unpack_from("<II", data, p)
            p += 8
            end = data.index(b"\0", p)
            self.streams[data[p:end].decode()] = (md + off, size)
            p = (end + 1 + 3) & ~3

        for required in ("#~", "#Strings"):
            if required not in self.streams:
                raise MetadataError(f"missing {required} stream")

        self.strings_off = self.streams["#Strings"][0]
        self.blob_off = self.streams.get("#Blob", (0, 0))[0]

    # -- Table stream ------------------------------------------------------
    def _parse_tables(self):
        data = self.data
        tbl = self.streams["#~"][0]
        heapsizes = data[tbl + 6]
        valid = struct.unpack_from("<Q", data, tbl + 8)[0]

        p = tbl + 24
        self.rows = {}
        for i in range(64):
            if valid >> i & 1:
                self.rows[i] = struct.unpack_from("<I", data, p)[0]
                p += 4

        self.sidx = 4 if heapsizes & 1 else 2
        self.gidx = 4 if heapsizes & 2 else 2
        self.bidx = 4 if heapsizes & 4 else 2

        # Only tables 0x00..0x06 need sizing to locate TypeDef and MethodDef.
        self.table_start = {}
        for t in sorted(self.rows):
            if t > 0x06:
                break
            self.table_start[t] = p
            p += self._row_size(t) * self.rows[t]

    def _ridx(self, table: int) -> int:
        return 4 if self.rows.get(table, 0) >= 0x10000 else 2

    def _coded(self, spec) -> int:
        tables, bits = spec
        biggest = max(self.rows.get(t, 0) for t in tables)
        return 4 if biggest >= (1 << (16 - bits)) else 2

    def _row_size(self, t: int) -> int:
        s, g, b = self.sidx, self.gidx, self.bidx
        if t == 0x00:  # Module
            return 2 + s + 3 * g
        if t == 0x01:  # TypeRef
            return self._coded(self.RESOLUTION_SCOPE) + 2 * s
        if t == 0x02:  # TypeDef
            return (4 + 2 * s + self._coded(self.TYPE_DEF_OR_REF)
                    + self._ridx(0x04) + self._ridx(0x06))
        if t == 0x03:  # FieldPtr
            return self._ridx(0x04)
        if t == 0x04:  # Field
            return 2 + s + b
        if t == 0x05:  # MethodPtr
            return self._ridx(0x06)
        if t == 0x06:  # MethodDef
            return 4 + 2 + 2 + s + b + self._ridx(0x08)
        raise MetadataError(f"unsupported table 0x{t:02x}")

    def _read(self, off: int, size: int) -> int:
        return struct.unpack_from("<I" if size == 4 else "<H", self.data, off)[0]

    def _string(self, index: int) -> str:
        start = self.strings_off + index
        return self.data[start:self.data.index(b"\0", start)].decode("utf-8")

    # -- Lookup ------------------------------------------------------------
    def find_method(self, type_full_name: str, method_name: str):
        """Return (method_rva, signature_blob_index) or None."""
        td_size = self._row_size(0x02)
        md_size = self._row_size(0x06)
        td_start = self.table_start.get(0x02)
        md_start = self.table_start.get(0x06)
        if td_start is None or md_start is None:
            raise MetadataError("assembly has no TypeDef/MethodDef tables")

        n_types = self.rows[0x02]
        n_methods = self.rows[0x06]
        method_list_off = 4 + 2 * self.sidx + \
            self._coded(self.TYPE_DEF_OR_REF) + self._ridx(0x04)

        for i in range(n_types):
            o = td_start + i * td_size
            name = self._string(self._read(o + 4, self.sidx))
            ns = self._string(self._read(o + 4 + self.sidx, self.sidx))
            full = f"{ns}.{name}" if ns else name
            if full != type_full_name:
                continue

            first = self._read(o + method_list_off, self._ridx(0x06))
            if i + 1 < n_types:
                nxt = td_start + (i + 1) * td_size
                last = self._read(nxt + method_list_off, self._ridx(0x06))
            else:
                last = n_methods + 1

            for m in range(first, last):  # 1-based
                mo = md_start + (m - 1) * md_size
                if self._string(self._read(mo + 8, self.sidx)) != method_name:
                    continue
                rva = struct.unpack_from("<I", self.data, mo)[0]
                sig = self._read(mo + 8 + self.sidx, self.bidx)
                return rva, sig
        return None

    def body_offset(self, rva: int) -> int:
        """File offset of the IL instructions of a method body."""
        off = self.rva_to_offset(rva)
        header = self.data[off]
        if header & 0x03 == 0x02:  # tiny header
            return off + 1
        header_size = (struct.unpack_from("<H", self.data, off)[0] >> 12) * 4
        return off + header_size

    def returns_bool(self, sig_index: int) -> bool:
        if not self.blob_off:
            return True  # cannot verify, assume ok
        p = self.blob_off + sig_index
        length = self.data[p]
        if length & 0x80:  # compressed length, 2 or 4 bytes
            p += 2 if length & 0xC0 == 0x80 else 4
        else:
            p += 1
        p += 1  # calling convention
        # (generic param count would follow for generic methods; not our case)
        p += 1  # param count
        return self.data[p] == 0x02  # ELEMENT_TYPE_BOOLEAN


def patch_windows_connectivity_dll(dll_path: str) -> int:
    print("Patching WindowsConnectivity.dll to bypass Bluetooth state check...")
    with open(dll_path, "rb") as f:
        data = bytearray(f.read())

    try:
        asm = Assembly(bytes(data))
        found = asm.find_method(TARGET_TYPE, TARGET_METHOD)
    except MetadataError as e:
        print(f"WARNING: could not read assembly metadata ({e}) - skipping patch.")
        return 1

    if found is None:
        print(f"WARNING: {TARGET_TYPE}::{TARGET_METHOD} not found - skipping patch.")
        return 1

    rva, sig = found
    if rva == 0:
        print(f"WARNING: {TARGET_METHOD} has no body - skipping patch.")
        return 1

    if not asm.returns_bool(sig):
        print(f"WARNING: {TARGET_METHOD} does not return bool - skipping patch.")
        return 1

    off = asm.body_offset(rva)
    if bytes(data[off:off + 2]) == PATCHED:
        print("Already patched, skipping.")
        return 0

    data[off:off + 2] = PATCHED
    with open(dll_path, "wb") as f:
        f.write(data)
    print(f"Patched {TARGET_TYPE}::{TARGET_METHOD} at 0x{off:x} "
          "(now always returns true).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python patch_windows_connectivity_dll.py "
              "<WindowsConnectivity.dll path>")
        exit(1)
    # Always exit 0: a failed patch must not abort the Lutris install script,
    # the warning above is enough to diagnose it.
    patch_windows_connectivity_dll(sys.argv[1])
