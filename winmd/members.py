#!/usr/bin/python3
"""List everything WindowsConnectivity.dll needs from the assemblies stubbed here.

The stubs only have to satisfy metadata, so what matters is the exact set of
types and members the game's DLL references -- which its own metadata states.
This reads it out, so the stubs are checked against the game rather than guessed
from WinRT documentation.

    ./members.py <WindowsConnectivity.dll>            # what is needed
    ./members.py <WindowsConnectivity.dll> --check    # ... and whether build/ has it

`--check` exits non-zero if anything referenced is missing from build/, which is
what makes it worth running after a MyWhoosh update: a new build that touches
more of WinRT shows up here instead of as a crash.
"""

import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "patch"))

from patch_windows_connectivity_dll import Assembly, MetadataError  # noqa: E402
from rename_assembly import Renamer  # noqa: E402

# The assemblies ../winmd stubs.  Anything referenced from elsewhere is the
# real BCL's problem, not ours.
STUBBED = ("Windows", "System.Runtime.WindowsRuntime")

STUBS = ("Windows.dll", "System.Runtime.WindowsRuntime.dll")

TABLE_TYPE_REF = 0x01
TABLE_MEMBER_REF = 0x0A
TABLE_ASSEMBLY_REF = 0x23


def assembly_ref_names(asm):
    """Map an AssemblyRef row index (1-based) to its name.

    Sized by hand because AssemblyRef sits past the tables Renamer measures;
    it only needs the rows before it, which Renamer already walked.
    """
    start = asm.table_start.get(TABLE_ASSEMBLY_REF)
    if start is None:
        # Renamer stops at Assembly (0x20); walk the rest from there.
        raise MetadataError("AssemblyRef table not located")
    names = {}
    row = 4 * 2 + 4 + asm.bidx  # Version(4*2) Flags(4) PublicKeyOrToken(blob)
    size = row + 2 * asm.sidx + asm.bidx
    for i in range(asm.rows[TABLE_ASSEMBLY_REF]):
        names[i + 1] = asm._string(asm._read(start + i * size + row, asm.sidx))
    return names


class Reader(Renamer):
    """Renamer, extended to reach AssemblyRef."""

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

        self.table_start = {}
        for t in sorted(self.rows):
            if t > TABLE_ASSEMBLY_REF:
                break
            self.table_start[t] = p
            if t == TABLE_ASSEMBLY_REF:
                break
            p += self._row_size(t) * self.rows[t]

    def _row_size(self, t):
        if t == 0x20:  # Assembly
            return 4 + 8 + 4 + self.bidx + 2 * self.sidx
        if t == 0x21:  # AssemblyProcessor
            return 4
        if t == 0x22:  # AssemblyOS
            return 4 * 3
        return super()._row_size(t)


def required(dll_path):
    """{type full name: {member names}} for every reference into a stubbed assembly."""
    with open(dll_path, "rb") as f:
        asm = Reader(f.read())

    ref_names = assembly_ref_names(asm)
    scope_width = asm._coded(asm.RESOLUTION_SCOPE)
    tr = asm.table_start[TABLE_TYPE_REF]
    tr_size = asm._row_size(TABLE_TYPE_REF)

    def typeref(row):  # 1-based
        o = tr + (row - 1) * tr_size
        scope = asm._read(o, scope_width)
        name = asm._string(asm._read(o + scope_width, asm.sidx))
        ns = asm._string(asm._read(o + scope_width + asm.sidx, asm.sidx))
        # ResolutionScope tag 2 is AssemblyRef.
        origin = ref_names.get(scope >> 2) if scope & 3 == 2 else None
        return origin, (f"{ns}.{name}" if ns else name)

    out = {}
    for row in range(1, asm.rows[TABLE_TYPE_REF] + 1):
        origin, name = typeref(row)
        if origin in STUBBED:
            out.setdefault(name, set())

    mr = asm.table_start[TABLE_MEMBER_REF]
    mr_size = asm._row_size(TABLE_MEMBER_REF)
    parent_width = asm._coded(asm.MEMBER_REF_PARENT)
    for i in range(asm.rows[TABLE_MEMBER_REF]):
        o = mr + i * mr_size
        parent = asm._read(o, parent_width)
        if parent & 7 != 1:  # MemberRefParent tag 1 is TypeRef
            continue
        origin, name = typeref(parent >> 3)
        if origin not in STUBBED:
            continue
        out.setdefault(name, set()).add(
            asm._string(asm._read(o + parent_width, asm.sidx)))
    return out


def provided(directory):
    """{type full name: {method names}} across the built stubs."""
    out = {}
    for stub in STUBS:
        path = os.path.join(directory, stub)
        if not os.path.exists(path):
            raise MetadataError(f"{path} not built - run ./build.sh")
        with open(path, "rb") as f:
            asm = Assembly(f.read())
        td = asm.table_start[0x02]
        td_size = asm._row_size(0x02)
        md = asm.table_start[0x06]
        md_size = asm._row_size(0x06)
        list_off = (4 + 2 * asm.sidx + asm._coded(asm.TYPE_DEF_OR_REF)
                    + asm._ridx(0x04))
        n = asm.rows[0x02]
        for i in range(n):
            o = td + i * td_size
            name = asm._string(asm._read(o + 4, asm.sidx))
            ns = asm._string(asm._read(o + 4 + asm.sidx, asm.sidx))
            full = f"{ns}.{name}" if ns else name
            first = asm._read(o + list_off, asm._ridx(0x06))
            if i + 1 < n:
                last = asm._read(td + (i + 1) * td_size + list_off,
                                 asm._ridx(0x06))
            else:
                last = asm.rows[0x06] + 1
            members = {
                asm._string(asm._read(
                    md + (asm._resolve_method(m) - 1) * md_size + 8, asm.sidx))
                for m in range(first, last)
            }
            out.setdefault(full, set()).update(members)
    return out


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    need = required(argv[1])
    check = "--check" in argv[2:]

    if not check:
        total = 0
        for t in sorted(need):
            print(t)
            for m in sorted(need[t]):
                print(f"    {m}")
                total += 1
        print(f"\n{len(need)} types, {total} members")
        return 0

    have = provided(os.path.join(HERE, "build"))
    missing = False
    for t in sorted(need):
        if t not in have:
            print(f"MISSING TYPE    {t}")
            missing = True
            continue
        for m in sorted(need[t] - have[t]):
            print(f"MISSING MEMBER  {t}::{m}")
            missing = True
    if missing:
        print("\nthe stubs do not cover this WindowsConnectivity.dll")
        return 1
    print(f"all {sum(len(v) for v in need.values())} referenced members "
          f"across {len(need)} types are covered")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
