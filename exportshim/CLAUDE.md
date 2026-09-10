# exportshim — engineering notes

`README.md` is the orientation. This is the reasoning behind it.

## Why the runtime cannot be talked into doing it

The refusal comes from mono's own array marshaller,
`mono/metadata/marshal-ilgen.c`, `emit_marshal_array_ilgen`:

```c
case MARSHAL_ACTION_MANAGED_CONV_IN: {
        if (t->byref) {
                char *msg = g_strdup ("Byref array marshalling to managed code is not implemented.");
                mono_mb_emit_exception_marshal_directive (mb, msg);
```

Three consequences, from reading it rather than guessing:

- **It is native runtime code, not a managed BCL gap**, so no Cecil-style
  rewrite of an assembly reaches it. Confirmed in the prefix: the string is in
  `mono-2.0/bin/libmono-2.0-x86_64.dll` and in no managed assembly anywhere.
  Newer runtimes do not help — 10.0.0 and 11.1.0 both carry it.
- **The refusal is compiled into the wrapper**, which is why probes that call
  the managed methods directly never see it: the native-to-managed wrapper
  builds fine and throws on the first call, from a frame where nothing can
  catch it.
- **Deleting the byref check would not be enough.** Two more walls stand behind
  it in the same function: the array needs a `[MarshalAs]` to fix a native
  shape, then a `SizeConst` or `SizeParamIndex` to know how many elements to
  read. The game's metadata has neither — measured with `../tools/SigDump.exe`,
  the parameter carries `[Out]` and nothing else. Supporting it would mean
  writing the out-direction inside `libmono` and shipping a self-built
  wine-mono.

## Two details that are deliberate

**Nothing may escape.** These run as native-to-managed thunks, where an
exception is a crash and not an error. Every call is wrapped: on failure the
shim writes a null pointer, returns 0, and logs.

**The delegates are rooted.** `GetFunctionPointerForDelegate` does not keep the
delegate alive; a collected one leaves the slot pointing at freed trampoline
code, which would fail minutes later and look like anything but this.

## Who owns the memory

Blocks are freshly allocated per call and never freed by us, exactly as the
CLR's out-marshalling does — whoever calls owns them. Measured over a 4-minute
run: 303 polls, 303 distinct addresses, not one reused. A freed 64-byte block
would come straight back from the allocator, so MyWhoosh does not free them.
That is a leak it has on Windows too, where the CLR allocates the same way, and
not one this adds: at the observed 2 Hz and ~400 bytes a poll, about 3 MB an
hour. Reusing one buffer per export would remove even that, but only by betting
on the game never freeing — and if the bet is wrong it is a use-after-free
rather than a slow leak, so it is not taken.

The struct layout is mono's own: `Marshal.SizeOf` reports 64 bytes for
`DeviceInformationStruct` under wine-mono (two enums, six `LPWStr`, six `bool`
as `I1`, padded), and the game confirmed it by reading a device name and UUID
back out of our block and connecting to it.

## The scan rescue — LAN sensors only

`ScanRescue` and `DeviceCaps` in `ExportShim.cs` exist for the Wahoo Direct
Connect path on the `dev` branch, and do nothing on this one. Keeping them costs
nothing and keeps the two branches' shim identical, but do not take them as a
model for Bluetooth work.

The problem they solve: `GetAllScannedDevices` reports `scannedList` — the LAN
sensors that were discovered over mDNS — only when `scanDeviceType` is
`E_PowerSource` (1) or `E_SecondaryPower` (8) and `scanMechanism` is 0. Ask it
for `E_HeartRate` (4) and it walks `pairedList` instead, so a heart-rate strap
announced over Direct Connect is browsed, resolved, written into `scannedList`
and then never offered. On Windows that is no gap, because straps arrive over
Bluetooth — which on this branch is exactly the path that now works.

`ScanRescue` answers by calling the game's method twice: once as it stands, once
with `scanDeviceType` forced to `E_PowerSource`, rewriting each returned struct
to the slot actually requested. The forced field is restored in a `finally`
because other threads read it. `DeviceCaps` then hides a sensor from a slot its
capabilities cannot fill, reading `C:\fakesensor-table`, which the `dev`
branch's `fakebonjour.c` writes at load time.

None of that is needed here: on the Bluetooth path the game fills its own slots
from `SensorBase::features` after connecting, and each sensor is its own device
with its own connection — no shared serial, no single-connection limit, no
side-channel file.

## How it used to be started

On the `dev` branch the loader is `fakesensor/fakebonjour.c`, native code that
reaches `MyWhoosh.ExportShim::Install` through libmono's embedding API
(`mono_domain_assembly_open`, `mono_class_from_name`, `mono_runtime_invoke`) —
`Install` is a static void with no arguments precisely because that is the one
shape needing nothing marshalled. It fires on the game's first Bonjour
`CreateInstance`, which happens during the game's own connectivity init.

On this branch `../bleshim/src/Loader.cs` does it from managed code instead, and
`MYWHOOSH_SHIM_DLL` still overrides the path (set empty, it disables the shim,
which is how the original failure is reproduced).

The consequence worth knowing on `dev`: the shim installs only when the Bonjour
path activates. That is in time in practice, but if a game update ever polled a
`BT_*` list first, the fix would be an earlier trigger, not a change here. On
this branch that concern is gone — the loader runs when the Bluetooth assembly
is first touched, which is before any poll.
