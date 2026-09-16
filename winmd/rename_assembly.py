#!/usr/bin/python3
"""Truncate an assembly's name in its metadata, in place.

`mcs` refuses to emit an assembly called `System.Runtime.WindowsRuntime`
(CS0281: mscorlib grants that name friend access, and we cannot sign with
Microsoft's key).  That is purely a compiler-side check - the runtime only
cares that the AssemblyRef and the Assembly name match - so the stub is built
under a name with one extra character and the character is then overwritten
with the string terminator here.

Only the Assembly row's Name is touched.  The #Strings heap is a set of
NUL-terminated strings addressed by offset, so writing a NUL one byte early
shortens exactly this one; the module name is a separate entry and is
unaffected.  Nothing moves, so every other offset in the file stays valid.

    ./rename_assembly.py <assembly.dll> <wanted name>
"""

import struct
import sys

import os

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "patch"))
from patch_windows_connectivity_dll import Assembly, MetadataError  # noqa: E402

TABLE_ASSEMBLY = 0x20


class Renamer(Assembly):
    """Assembly, extended far enough to reach the Assembly table's Name column."""

    # Assembly (0x20) sits past the tables the patcher sizes, so every table
    # between MethodDef and it has to be measured to find where its row starts.
    HAS_CONSTANT = ([0x04, 0x08, 0x17], 2)
    HAS_CUSTOM_ATTRIBUTE = ([0x06, 0x04, 0x01, 0x02, 0x08, 0x09, 0x0A, 0x00,
                             0x0E, 0x17, 0x14, 0x11, 0x1A, 0x1B, 0x20, 0x23,
                             0x26, 0x27, 0x28], 5)
    HAS_FIELD_MARSHAL = ([0x04, 0x08], 1)
    HAS_DECL_SECURITY = ([0x02, 0x06, 0x20], 2)
    MEMBER_REF_PARENT = ([0x02, 0x01, 0x1A, 0x06, 0x1B], 3)
    HAS_SEMANTICS = ([0x14, 0x17], 1)
    METHOD_DEF_OR_REF = ([0x06, 0x0A], 1)
    MEMBER_FORWARDED = ([0x04, 0x06], 1)
    IMPLEMENTATION = ([0x26, 0x23, 0x27], 2)
    CUSTOM_ATTRIBUTE_TYPE = ([0x00, 0x00, 0x06, 0x0A, 0x00], 3)

    def _parse_tables(self):
        data = self.data
        tbl = self.streams["#~"][0]
        heapsizes = data[tbl + 6]
        valid = struct.unpack_from("<Q", data, tbl + 8)[0]
        sorted_ = struct.unpack_from("<Q", data, tbl + 16)[0]  # noqa: F841

        p = tbl + 24
        self.rows = {}
        for i in range(64):
            if valid >> i & 1:
                self.rows[i] = struct.unpack_from("<I", data, p)[0]
                p += 4

        self.sidx = 4 if heapsizes & 1 else 2
        self.gidx = 4 if heapsizes & 2 else 2
        self.bidx = 4 if heapsizes & 4 else 2

        self.table_start = {}
        for t in sorted(self.rows):
            if t > TABLE_ASSEMBLY:
                break
            self.table_start[t] = p
            if t == TABLE_ASSEMBLY:
                break  # the target row itself; its size is never needed
            p += self._row_size(t) * self.rows[t]

    def _row_size(self, t: int) -> int:
        s, g, b = self.sidx, self.gidx, self.bidx
        sizes = {
            0x08: 2 + 2 + s,                                        # Param
            0x09: self._ridx(0x02) + self._coded(self.TYPE_DEF_OR_REF),
            0x0A: self._coded(self.MEMBER_REF_PARENT) + s + b,      # MemberRef
            0x0B: 1 + 1 + self._coded(self.HAS_CONSTANT) + b,       # Constant
            0x0C: self._coded(self.HAS_CUSTOM_ATTRIBUTE)
                  + self._coded(self.CUSTOM_ATTRIBUTE_TYPE) + b,
            0x0D: b + self._coded(self.HAS_FIELD_MARSHAL),          # FieldMarshal
            0x0E: 2 + self._coded(self.HAS_DECL_SECURITY) + b,      # DeclSecurity
            0x0F: 2 + 4 + self._ridx(0x02),                         # ClassLayout
            0x10: 4 + self._ridx(0x04),                             # FieldLayout
            0x11: b,                                                # StandAloneSig
            0x12: self._ridx(0x02) + self._ridx(0x14),              # EventMap
            0x14: 2 + s + self._coded(self.TYPE_DEF_OR_REF),        # Event
            0x15: self._ridx(0x02) + self._ridx(0x17),              # PropertyMap
            0x17: 2 + s + b,                                        # Property
            0x18: 2 + self._ridx(0x06) + self._coded(self.HAS_SEMANTICS),
            0x19: self._ridx(0x02) + 2 * self._coded(self.METHOD_DEF_OR_REF),
            0x1A: s,                                                # ModuleRef
            0x1B: b,                                                # TypeSpec
            0x1C: 2 + self._coded(self.MEMBER_FORWARDED) + s + self._ridx(0x1A),
            0x1D: 4 + self._ridx(0x04),                             # FieldRVA
            0x1E: 0,                                                # EncLog
            0x1F: 0,                                                # EncMap
        }
        if t in sizes:
            return sizes[t]
        return super()._row_size(t)

    def assembly_name_offset(self) -> int:
        """File offset of the Assembly row's Name string."""
        start = self.table_start.get(TABLE_ASSEMBLY)
        if start is None or not self.rows.get(TABLE_ASSEMBLY):
            raise MetadataError("no Assembly table (not an assembly manifest)")
        # Assembly row: HashAlgId(4) MajorVersion..RevisionNumber(4*2)
        #               Flags(4) PublicKey(blob) Name(string) Culture(string)
        name_col = 4 + 8 + 4 + self.bidx
        return self.strings_off + self._read(start + name_col, self.sidx)


def main(path: str, wanted: str) -> int:
    with open(path, "rb") as f:
        data = bytearray(f.read())

    asm = Renamer(bytes(data))
    off = asm.assembly_name_offset()
    end = data.index(b"\0", off)
    current = data[off:end].decode()

    if current == wanted:
        print(f"{path}: already named {wanted!r}")
        return 0
    if not current.startswith(wanted):
        print(f"ERROR: {path} is named {current!r}, which is not {wanted!r} "
              "plus a suffix - refusing to rewrite.", file=sys.stderr)
        return 1

    data[off + len(wanted)] = 0
    with open(path, "wb") as f:
        f.write(data)
    print(f"{path}: {current!r} -> {wanted!r}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    sys.exit(main(sys.argv[1], sys.argv[2]))
