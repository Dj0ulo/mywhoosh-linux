#!/usr/bin/python3
"""List everything WindowsConnectivity.dll needs from the assemblies stubbed here.

The stubs only have to satisfy metadata, so what matters is the exact set of
types and members the game's DLL references -- and their exact signatures,
which its own metadata states.  This reads them out, so the stubs are checked
against the game rather than guessed from WinRT documentation.

    ./members.py <WindowsConnectivity.dll>            # what is needed
    ./members.py <WindowsConnectivity.dll> --check    # ... and whether build/ has it
    ./members.py <WindowsConnectivity.dll> --check --build-dir ../bleshim/build

`--check` exits non-zero if anything referenced is missing from build/ or is
there under a different signature, which is what makes it worth running after a
MyWhoosh update: a new build that touches more of WinRT shows up here instead of
as a crash.

Signatures matter as much as names.  `BluetoothLEAdvertisement.ServiceUuids`
returning `IReadOnlyList<Guid>` where the game's metadata says `IList<Guid>`
compiles, and then throws `MissingMethodException` on every advertisement --
indistinguishable, from outside, from a scan that found nothing.
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


# -- Signature decoding ---------------------------------------------------
#
# A member with the right name and the wrong signature satisfies a name-only
# check and then throws MissingMethodException at run time, which surfaces as
# the feature simply not working.  So the blobs get decoded on both sides and
# compared: ECMA-335 II.23.2 for the grammar, II.23.1.16 for the element types.

# ELEMENT_TYPE_* that stand for a type on their own.  C# spellings, because
# both sides encode the same byte and the output is for a human.
PRIMITIVE = {
    0x01: "void", 0x02: "bool", 0x03: "char", 0x04: "sbyte", 0x05: "byte",
    0x06: "short", 0x07: "ushort", 0x08: "int", 0x09: "uint", 0x0A: "long",
    0x0B: "ulong", 0x0C: "float", 0x0D: "double", 0x0E: "string",
    0x16: "typedref", 0x18: "IntPtr", 0x19: "UIntPtr", 0x1C: "object",
}

ELEMENT_PTR = 0x0F
ELEMENT_BYREF = 0x10
ELEMENT_VALUETYPE = 0x11
ELEMENT_CLASS = 0x12
ELEMENT_VAR = 0x13
ELEMENT_ARRAY = 0x14
ELEMENT_GENERICINST = 0x15
ELEMENT_FNPTR = 0x1B
ELEMENT_SZARRAY = 0x1D
ELEMENT_MVAR = 0x1E
CMOD_REQD = 0x1F
CMOD_OPT = 0x20
SENTINEL = 0x41

CALLCONV_KIND = 0x0F
CALLCONV_FIELD = 0x06
CALLCONV_HASTHIS = 0x20
CALLCONV_GENERIC = 0x10


class Signatures:
    """Decode a member's signature blob into a comparable, printable form.

    Mixed into the metadata reader.  Types are named the way the *declaring*
    metadata names them, with no assembly qualifier: the BCL splits types
    across different assemblies on wine-mono than on the host (see
    ../bleshim/CLAUDE.md), and an assembly-qualified comparison would report
    that split as a mismatch.  A name-and-shape comparison is what the runtime
    itself does when it binds a MemberRef.
    """

    def _skip_mods(self, p):
        while self.data[p] in (CMOD_REQD, CMOD_OPT):
            p += 1
            _, p = self._uncompress(p)   # TypeDefOrRef of the modifier
        return p

    def _typedef_name(self, row):
        o = self.table_start[0x02] + (row - 1) * self._row_size(0x02)
        name = self._string(self._read(o + 4, self.sidx))
        ns = self._string(self._read(o + 4 + self.sidx, self.sidx))
        return f"{ns}.{name}" if ns else name

    def _typeref_name(self, row):
        w = self._coded(self.RESOLUTION_SCOPE)
        size = self._row_size(TABLE_TYPE_REF)
        o = self.table_start[TABLE_TYPE_REF] + (row - 1) * size
        name = self._string(self._read(o + w, self.sidx))
        ns = self._string(self._read(o + w + self.sidx, self.sidx))
        return f"{ns}.{name}" if ns else name

    def _typespec_name(self, row):
        o = self.table_start[0x1B] + (row - 1) * self.bidx
        p = self.blob_off + self._read(o, self.bidx)
        _, p = self._uncompress(p)       # blob length
        return self._type(p)[0]

    def typespec_base(self, row):
        """The TypeDefOrRef token a TypeSpec's generic instantiation is built on.

        A member reached through `TypedEventHandler<A, B>` has a TypeSpec for a
        parent, not a TypeRef, so this is how such a reference is traced back to
        the assembly that has to declare the type.  None for a TypeSpec that is
        not a generic instantiation (an array or a pointer), which cannot be the
        parent of a member reference.
        """
        o = self.table_start[0x1B] + (row - 1) * self.bidx
        p = self.blob_off + self._read(o, self.bidx)
        _, p = self._uncompress(p)       # blob length
        if self.data[p] != ELEMENT_GENERICINST:
            return None
        return self._uncompress(p + 2)[0]  # +2: past GENERICINST and CLASS

    def _type_token(self, p):
        """A TypeDefOrRef coded index inside a signature (ECMA-335 II.23.2.8)."""
        tok, p = self._uncompress(p)
        tag, row = tok & 3, tok >> 2
        if tag == 0:
            return self._typedef_name(row), p
        if tag == 1:
            return self._typeref_name(row), p
        if tag == 2:
            return self._typespec_name(row), p
        raise MetadataError(f"bad TypeDefOrRef tag {tag}")

    def _type(self, p):
        """One Type (II.23.2.12); returns (rendered, offset past it)."""
        et = self.data[p]
        p += 1
        if et in PRIMITIVE:
            return PRIMITIVE[et], p
        if et in (ELEMENT_VALUETYPE, ELEMENT_CLASS):
            return self._type_token(p)
        if et == ELEMENT_BYREF:
            t, p = self._type(p)
            return t + "&", p
        if et == ELEMENT_PTR:
            p = self._skip_mods(p)
            t, p = self._type(p)
            return t + "*", p
        if et == ELEMENT_SZARRAY:
            p = self._skip_mods(p)
            t, p = self._type(p)
            return t + "[]", p
        if et == ELEMENT_ARRAY:
            t, p = self._type(p)
            rank, p = self._uncompress(p)
            for _ in range(2):           # sizes, then lower bounds
                n, p = self._uncompress(p)
                for _ in range(n):
                    _, p = self._uncompress(p)
            return t + "[" + "," * (rank - 1) + "]", p
        if et == ELEMENT_GENERICINST:
            p += 1                       # CLASS or VALUETYPE, already implied
            base, p = self._type_token(p)
            n, p = self._uncompress(p)
            args = []
            for _ in range(n):
                a, p = self._type(p)
                args.append(a)
            # Drop the arity tick: `1<Guid> reads worse than <Guid> and the
            # argument list already says it.
            return base.split("`")[0] + "<" + ", ".join(args) + ">", p
        if et == ELEMENT_VAR:
            n, p = self._uncompress(p)
            return f"!{n}", p
        if et == ELEMENT_MVAR:
            n, p = self._uncompress(p)
            return f"!!{n}", p
        if et == ELEMENT_FNPTR:
            raise MetadataError("function pointer in a signature")
        raise MetadataError(f"unsupported element type 0x{et:02x}")

    def signature(self, blob_index):
        """(declaration, arguments) for a member's signature blob.

        Split in two so the member's name can be printed between them, and
        hashable so the two sides can be compared as sets.
        """
        p = self.blob_off + blob_index
        _, p = self._uncompress(p)       # blob length
        conv = self.data[p]
        p += 1

        if conv & CALLCONV_KIND == CALLCONV_FIELD:
            p = self._skip_mods(p)
            return ("field " + self._type(p)[0], "")

        generic = 0
        if conv & CALLCONV_GENERIC:
            generic, p = self._uncompress(p)
        count, p = self._uncompress(p)

        p = self._skip_mods(p)
        ret, p = self._type(p)

        params = []
        for _ in range(count):
            if self.data[p] == SENTINEL:  # vararg: everything after is optional
                p += 1
                params.append("...")
            p = self._skip_mods(p)
            t, p = self._type(p)
            params.append(t)

        decl = ("instance " if conv & CALLCONV_HASTHIS else "") + ret
        if generic:
            decl += f" <{generic} generic parameters>"
        return (decl, "(" + ", ".join(params) + ")")


def render(name, sig):
    """`instance IList<Guid> get_ServiceUuids()` - one member, as declared."""
    decl, args = sig
    return f"{decl} {name}{args}"


class Reader(Signatures, Renamer):
    """Renamer, extended to reach AssemblyRef and to decode signatures."""

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

    def _resolve_field(self, index):
        """Map a 1-based FieldList index through FieldPtr, if present."""
        fp = self.table_start.get(0x03)
        if fp is None:
            return index
        o = fp + (index - 1) * self._row_size(0x03)
        return self._read(o, self._ridx(0x04))

    def _row_size(self, t):
        if t == 0x20:  # Assembly
            return 4 + 8 + 4 + self.bidx + 2 * self.sidx
        if t == 0x21:  # AssemblyProcessor
            return 4
        if t == 0x22:  # AssemblyOS
            return 4 * 3
        return super()._row_size(t)


def required(dll_path):
    """{type full name: {member name: {signature}}} for every reference into a
    stubbed assembly.  A type with no members is one referenced only as a type,
    which still has to exist."""
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
            out.setdefault(name, {})

    mr = asm.table_start[TABLE_MEMBER_REF]
    mr_size = asm._row_size(TABLE_MEMBER_REF)
    parent_width = asm._coded(asm.MEMBER_REF_PARENT)
    for i in range(asm.rows[TABLE_MEMBER_REF]):
        o = mr + i * mr_size
        parent = asm._read(o, parent_width)
        tag = parent & 7
        if tag == 1:                     # MemberRefParent tag 1 is TypeRef
            token = parent >> 3
        elif tag == 4:                   # ... tag 4 is TypeSpec
            # `TypedEventHandler<Watcher, ReceivedEventArgs>::.ctor` is a
            # reference into a stubbed assembly like any other; only the route
            # to the type differs.  Check the generic definition it names.
            base = asm.typespec_base(parent >> 3)
            if base is None or base & 3 != 1:  # TypeDefOrRef tag 1 is TypeRef
                continue
            token = base >> 2
        else:
            continue
        origin, name = typeref(token)
        if origin not in STUBBED:
            continue
        member = asm._string(asm._read(o + parent_width, asm.sidx))
        sig = asm.signature(asm._read(o + parent_width + asm.sidx, asm.bidx))
        out.setdefault(name, {}).setdefault(member, set()).add(sig)
    return out


def provided(directory):
    """{type full name: {member name: {signature}}} across the built stubs."""
    out = {}
    for stub in STUBS:
        path = os.path.join(directory, stub)
        if not os.path.exists(path):
            raise MetadataError(f"{path} not built - run ./build.sh")
        with open(path, "rb") as f:
            asm = Reader(f.read())

        td = asm.table_start[0x02]
        td_size = asm._row_size(0x02)
        n = asm.rows[0x02]
        fields_off = 4 + 2 * asm.sidx + asm._coded(asm.TYPE_DEF_OR_REF)
        methods_off = fields_off + asm._ridx(0x04)

        # Field (0x04): Flags(2) Name(string) Signature(blob)
        # MethodDef (0x06): RVA(4) ImplFlags(2) Flags(2) Name(string)
        #                   Signature(blob)
        tables = (
            (0x04, fields_off, 2, asm._resolve_field),
            (0x06, methods_off, 8, asm._resolve_method),
        )

        for i in range(n):
            o = td + i * td_size
            name = asm._string(asm._read(o + 4, asm.sidx))
            ns = asm._string(asm._read(o + 4 + asm.sidx, asm.sidx))
            members = out.setdefault(f"{ns}.{name}" if ns else name, {})

            for table, off, name_col, resolve in tables:
                start = asm.table_start.get(table)
                if start is None:
                    continue
                size = asm._row_size(table)
                width = asm._ridx(table)
                first = asm._read(o + off, width)
                if i + 1 < n:
                    last = asm._read(td + (i + 1) * td_size + off, width)
                else:
                    last = asm.rows[table] + 1
                for m in range(first, last):
                    mo = start + (resolve(m) - 1) * size
                    member = asm._string(asm._read(mo + name_col, asm.sidx))
                    blob = asm._read(mo + name_col + asm.sidx, asm.bidx)
                    sig = asm.signature(blob)
                    members.setdefault(member, set()).add(sig)
    return out


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    need = required(argv[1])
    check = "--check" in argv[2:]
    # ../bleshim builds the same two assemblies from its own sources, and the
    # coverage check is worth exactly as much there.
    build = os.path.join(HERE, "build")
    if "--build-dir" in argv[2:]:
        build = argv[argv.index("--build-dir") + 1]

    total = sum(len(sigs) for t in need.values() for sigs in t.values())

    if not check:
        for t in sorted(need):
            print(t)
            for m in sorted(need[t]):
                for sig in sorted(need[t][m]):
                    print(f"    {render(m, sig)}")
        print(f"\n{len(need)} types, {total} members")
        return 0

    have = provided(build)
    missing = False
    for t in sorted(need):
        if t not in have:
            print(f"MISSING TYPE    {t}")
            missing = True
            continue
        for m in sorted(need[t]):
            if m not in have[t]:
                for sig in sorted(need[t][m]):
                    print(f"MISSING MEMBER  {t}::{render(m, sig)}")
                missing = True
                continue
            for sig in sorted(need[t][m] - have[t][m]):
                # The name is there, so this is an overload the game wants and
                # the stub spells differently -- the failure that costs an
                # evening, because it only shows at run time.
                print(f"WRONG SIGNATURE {t}::{m}")
                print(f"      game wants  {render(m, sig)}")
                for got in sorted(have[t][m]):
                    print(f"      stub has    {render(m, got)}")
                missing = True
    if missing:
        print("\nthe stubs do not cover this WindowsConnectivity.dll")
        return 1
    print(f"all {total} referenced members across {len(need)} types are "
          "covered, with matching signatures")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
