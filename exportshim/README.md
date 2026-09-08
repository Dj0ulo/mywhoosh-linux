# Export shim — serving the four byref-array exports ourselves

`WindowsConnectivity.dll` has 98 unmanaged exports. Four of them are the device-
list pollers the game's UI lives on, and all four pass the list by reference:

```
int WD_GetScannedDevicesList  (out DeviceInformationStruct[] devices)
int WD_GetConnectedDevicesList(out DeviceInformationStruct[] devices)
int BT_GetScannedDevicesList  (out DeviceInformationStruct[] devices)
int BT_GetConnectedDevicesList(out DeviceInformationStruct[] devices)
```

wine-mono will not marshal that shape from native code, so the game's first poll
was a fatal unhandled exception:

```
System.Runtime.InteropServices.MarshalDirectiveException:
  Byref array marshalling to managed code is not implemented.
```

This directory replaces those four exports with managed implementations of our
own, by rewriting the function pointers the game jumps through — in memory, with
no game file touched. With it installed the game discovers, resolves, connects
and polls a Dircon sensor entirely by itself; see "What this bought" below.

```sh
./build.sh
WINEPREFIX=~/Games/mywhoosh ./install.sh
```

It is loaded and started from `../fakesensor/fakebonjour.c`, so that DLL has to
be the current build too.

## Why the runtime cannot be talked into doing it

The message comes from mono's own array marshaller,
`mono/metadata/marshal-ilgen.c`, `emit_marshal_array_ilgen`:

```c
case MARSHAL_ACTION_MANAGED_CONV_IN: {
        if (t->byref) {
                char *msg = g_strdup ("Byref array marshalling to managed code is not implemented.");
                mono_mb_emit_exception_marshal_directive (mb, msg);
```

Three things follow from reading it rather than guessing:

- **It is native runtime code, not a managed BCL gap.** Unlike
  `ComAwareEventInfo` (blocker 1), which is a throw-only stub in `System.Core.dll`
  that `../winemono/` rewrites with Cecil, this lives in `libmono`. Confirmed in
  the prefix: the string is in `mono-2.0/bin/libmono-2.0-x86_64.dll` and in no
  managed assembly anywhere in the tree. A newer runtime is not the fix either —
  10.0.0 and 11.1.0 both carry it.
- **The refusal is compiled into the wrapper.**
  `mono_mb_emit_exception_marshal_directive` emits IL that *throws*, so the
  native-to-managed wrapper builds fine and the exception arrives on the first
  call, from a frame where nothing can catch it. That is why `TestDircon` and the
  probes never saw it: they call the managed methods directly.
- **Deleting the byref check would not be enough.** Two more walls stand behind
  it in the same function: the array needs a `[MarshalAs]` to fix a native shape
  (`"[MarshalAs] attribute required to marshal arrays to managed code."`) and
  then a `SizeConst` or `SizeParamIndex` to know how many elements to read. The
  game's metadata has neither — measured with `../tools/SigDump.exe`, the
  parameter carries `[Out]` and nothing else. So the out-direction would have to
  be written from scratch inside `libmono`, which means building and shipping a
  self-built wine-mono. That is the expensive option this directory avoids.

## Where the interception happens

The exports are not ordinary functions. Each is a 12-byte stub:

```
48 A1 <abs64>    mov rax, [slot]
FF E0            jmp rax
```

and every `slot` is one of 98 consecutive pointers in `.sdata` belonging to the
CLI header's **VTableFixups** array (RVA `0x36000`, type
`COR_VTABLE_64BIT | COR_VTABLE_FROM_UNMANAGED`). On disk the slots hold MethodDef
tokens `0x06000001`…; at load `mscoree` overwrites each with mono's
native-to-managed thunk for that method.

Two properties make this the right seam:

- **The game re-reads the slot on every call.** It resolves the exports with
  `GetProcAddress` once, early, and caches the pointers — but those point at the
  `mov`/`jmp` stub, not at mono's thunk. Writing the slot redirects calls the
  game had already looked up, so there is no window to be late for.
- **Nothing but our own memory changes.** `.sdata` is already writable
  (`0xC0000040`), the write happens long after the game's hash check on the file,
  and no file is touched. That is the constraint everything here works under:
  MyWhoosh hashes `WindowsConnectivity.dll` and silently declines to load it if a
  single byte differs — see `../dircon/README.md`.

`ExportShim.cs` decodes each stub to find its slot rather than trusting the
RVA above, so a MyWhoosh update that shifts the layout still lands correctly; if
the stub shape ever changes it logs the bytes it found and hooks nothing, rather
than corrupting a pointer.

## What the replacement does

The hard part disappears once the code is managed: the signature mono cannot
marshal does not get marshalled at all on our side. Each hooked export is a
delegate taking the one thing mono is happy to hand over, an `IntPtr`:

1. call the managed method directly by reflection, `out` array and all;
2. allocate `count` native elements with `Marshal.AllocCoTaskMem`;
3. `Marshal.StructureToPtr` each element;
4. store the block's address through the pointer, return the count.

That is what the CLR does on Windows for an `out` array, so the game gets the
contract it was built against. The layout is mono's own — `Marshal.SizeOf`
reports 64 bytes for `DeviceInformationStruct` under wine-mono (two enums, six
`LPWStr`, six `bool` as `I1`, padded), and the game proved it right by reading a
device name and UUID back out of our block and connecting to it.

Two details that are deliberate, not incidental:

- **Nothing may escape.** These run as native-to-managed thunks, where an
  exception is a crash and not an error, so every call is wrapped: on failure the
  shim writes a null pointer, returns 0 and logs.
- **The delegates are rooted.** `GetFunctionPointerForDelegate` does not keep the
  delegate alive; a collected one leaves the slot pointing at freed trampoline
  code, which would fail minutes later and look like anything but this.

The blocks are freshly allocated per call and never freed by us, exactly as the
CLR's out-marshalling does — whoever calls owns them. Measured over a 4-minute
run: 303 polls, 303 distinct block addresses, not one reused. A freed 64-byte
block would come straight back from the allocator, so MyWhoosh does not free
them — a leak it has on Windows too, where the CLR allocates the same way, and
not one this adds. At the observed 2 Hz and ~400 bytes a poll (the struct plus
six strings) that is around 3 MB an hour. Reusing one buffer per export would
remove even that, but only by betting on the game never freeing — and if the
bet is wrong it is a use-after-free rather than a slow leak, so it is not
taken.

## How it gets started

It needs our managed code running inside the game process, after
`WindowsConnectivity.dll` is loaded and before the first poll.
`../fakesensor/fakebonjour.c` is already there at exactly that moment — the
game's own connectivity init is what creates the Bonjour objects, which is what
loads that DLL (measured: `fakebonjour.dll` loads well before the crash). So its
first `CreateInstance` calls `shim_kick()`, which uses mono's embedding API to
load `MyWhooshShim.dll` and invoke `MyWhoosh.ExportShim::Install` — a static void
with no arguments, the one shape that needs nothing marshalled.

`shim_kick` finds the assembly next to libmono's own module path, so it and
`install.sh` agree without either being told where the prefix is.
`MYWHOOSH_SHIM_DLL` overrides the path, and set empty it disables the shim, which
is how the failure is reproduced.

**The consequence worth knowing:** the shim installs only when the Bonjour path
activates. In practice that is in time — the game never called either `BT_*`
poller in any run measured; BLE is gated natively long before. If a MyWhoosh
update ever polls a `BT_*` list first, the fix is a trigger earlier than
fakebonjour, not a change here.

## What this bought

One launch of `~/Games/mywhoosh-6.1.2-old-patch`, no GUI interaction at all, the
game's own threads throughout:

```
[exportshim] WD_GetScannedDevicesList: slot 0x180036200 0x3ada8240 -> 0x3ada92e0
             ... hooked 4/4 exports
[exportshim] WD_GetConnectedDevicesList: first call, ppDevices=0xb9c248
[fakebonjour] Browse(flags=0, ifIndex=0, "_wahoo-fitness-tnp._tcp.") on the main
[fakebonjour] ServiceFound("FakeTrainer")           -> 0x00000000
[fakebonjour] ServiceResolved(... at FakeTrainer.local.:36866) -> 0x00000000
[exportshim] WD_GetScannedDevicesList -> 1 device(s)
[fakebonjour] dircon: client connected
[fakebonjour] dircon: notifications power=1 hr=1
[exportshim] WD_GetConnectedDevicesList -> 1 device(s)      (every 500 ms)
```

Discovery, resolution, connection and a live power + heart-rate subscription,
driven by the game rather than by a probe. Where the previous run had
`FATAL UNHANDLED EXCEPTION` there is now nothing at all.

Confirmed in the UI on that same run: the Device Connection screen lists
`FakeTrainer` and shows its watts updating. Real hardware does not appear, which
is expected rather than a gap here -- `../fakesensor/` serves one hard-coded
sensor, and reaching an actual trainer is `blebridge.py`'s job (BLE itself stays
inert, since WinRT is what `../winmd/` stubs out).

## Files

| File | What it is |
|---|---|
| `ExportShim.cs` | The shim: finds the slots, replaces them, marshals the arrays |
| `build.sh` | `mcs` → `build/MyWhooshShim.dll` |
| `install.sh` | Copies it into the prefix's wine-mono tree (`--restore` removes it) |

`../tools/SigDump.exe` and `../tools/AllSigs.exe` are what measured the
marshalling surface: the exact parameter attributes and struct layout, and that
these four exports are the only ones with a byref array — the other byref
parameters are all structs, which mono's `emit_marshal_vtype_ilgen` does
implement for managed conversion.
