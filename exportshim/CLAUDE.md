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
  Newer runtimes do not help — wine-mono 10.0.0 and 11.1.0 both carry it.
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
CLR's out-marshalling does — whoever calls owns them. Measured over a multi-
minute run: several hundred polls, as many distinct addresses, not one reused.
A freed 64-byte block would come straight back from the allocator, so MyWhoosh
does not free them. That is a leak it has on Windows too, where the CLR
allocates the same way, and not one this adds: at the observed 2 Hz and ~400
bytes a poll, a few megabytes an hour. Reusing one buffer per export would
remove even that, but only by betting on the game never freeing — and if the bet
is wrong it is a use-after-free rather than a slow leak, so it is not taken.

The struct layout is mono's own: `Marshal.SizeOf` reports 64 bytes for
`DeviceInformationStruct` under wine-mono (two enums, six `LPWStr`, six `bool`
as `I1`, padded), and the game confirmed it by reading a device name and UUID
back out of our block and connecting to it.

## How it is started

`../bleshim/src/Loader.cs` calls `Install()` from a static constructor of the
assembly the game loads for Bluetooth, which runs inside the game's process,
on the game's thread, after `WindowsConnectivity.dll` is loaded and before the
first device-list poll — every condition `Install()` needs, and no native code
anywhere.

`Install` is a static void with no arguments, which is deliberate: it is the one
shape that needs nothing marshalled, so it stays callable from anywhere,
including through libmono's embedding API if a future loader ever has to.
`MYWHOOSH_SHIM_DLL` overrides the path it is loaded from; set empty, it disables
the shim, which is how the original failure is reproduced.
