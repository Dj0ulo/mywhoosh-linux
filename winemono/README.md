# Patched wine-mono — `ComAwareEventInfo`

`WahooProgram..ctor` → `WFTNP_Init()` wires MyWhoosh's four Bonjour callbacks
through `System.Runtime.InteropServices.ComAwareEventInfo`, every member of
which is a `NotImplementedException` stub in wine-mono. The exception escapes
the constructor, `dirconManager` stays null, and every later `WD_*` call throws
`NullReferenceException` — blocker 1 of `../dircon/README.md`.

This directory supplies the missing implementation and installs it into a Wine
prefix. **With it in place the whole `WD_*` stack initialises and runs**: the
four events wire, `Advise` returns cookies, and `TestDircon` drives the API from
`WD_InitWahooDirconManager` to `WD_StopScanningAll` without a single failure.
Nothing is discovered yet — blocker 2 (mDNSResponder is deaf under Wine) is
untouched and is what step 2 of the plan replaces.

## What it does

| File | Role |
|---|---|
| `ComEventShim.cs` | `MyWhoosh.ComEventShim.dll` — the real event wiring: `_Event` interface → source dispinterface → `IConnectionPointContainer::Advise` of a generated sink |
| `PatchSystemCore.cs` | Cecil pass that gives `ComAwareEventInfo` an `__inner` field, forwards its reflection members to that `EventInfo`, and forwards `Add`/`RemoveEventHandler` to the shim |
| `ShimProbe.cs` | Checks the patched class standalone: reflection surface plus the managed (non-COM) event path |
| `SinkEmitProbe.cs` | Emits the sink for the game's `Bonjour._IDNSSDEvents` and prints the IID and dispids it resolved |
| `ReflProbe.cs` | Minimal reproducer for the icall assertion below, and a map of which reflection is safe |
| `build.sh` / `install.sh` | Build everything; install the patched runtime into one prefix |

Mono.Cecil comes out of wine-mono's own GAC, and `ilasm`/`ikdasm` ship there
too, so nothing needs downloading to work on the runtime.

## Using it

```sh
./build.sh                                     # shim + patched System.Core.dll + probes
WINEPREFIX=~/Games/dircon-test ./install.sh    # one prefix, hard-linked copy
cd build && WINEPREFIX=~/Games/dircon-test wine ShimProbe.exe
```

`install.sh` copies the runner's wine-mono tree to
`<prefix>/drive_c/windows/mono/mono-2.0` with `cp -al`, then replaces
`System.Core.dll` (GAC and `lib/mono/4.5`) with the patched build and drops the
shim beside it. `mscoree` probes that path before the runner's shared copy, so
the runner tree is never written to and other prefixes are unaffected. Hard
links make the copy cost a few hundred kB rather than 230 MB — which is also why
`install.sh` deletes each file before overwriting it.

To undo: `rm -rf <prefix>/drive_c/windows/mono/mono-2.0`.

Env overrides: `WINE_MONO` (source runtime tree), `CECIL`, `GAME_LIBS`.

## How the shim differs from .NET's

Two constraints from wine-mono shape the design.

**The sink cannot implement the interop interface.** MyWhoosh embeds the Bonjour
interop types, so `_IDNSSDEvents` carries `_VtblGapN_M` placeholder slots that
Mono refuses to lay out in a managed class. The shim emits an equivalent
`[ComImport]` dispinterface instead — same IID, same method names and
signatures, gaps dropped — and the sink implements that. Callers reach it
through `IDispatch::Invoke`, so the missing vtable slots do not matter.
`../dircon/SinkProbe.cs` established this by hand; the shim generates it.

**Nothing may reflect over the source dispinterface's methods.** Those same gaps
make an assertion fail inside Mono's icall layer, and it *aborts the process*
rather than throwing:

```
$ wine build/ReflProbe.exe                     # src.GetMethod("ServiceFound")
* Assertion at .../mono/metadata/icall.c:4348, condition `method->slot < nslots' not met
```

`Type.GetMethod`, `GetMethods` — anything that resolves a method on
`Bonjour._IDNSSDEvents` — kills the runtime. The same reflection on the
`_IDNSSDEvents_Event` interface, whose single gap is harmless, works fine. So
the shim takes signatures from the event interface's delegate types, and dispids
from the type library the interface is registered against
(`HKCR\Interface\{iid}\TypeLib` → `LoadRegTypeLib` → `ITypeInfo::GetIDsOfNames`),
which returns the real values:

```
source IID : 21ae8d7f-d5fe-45cf-b632-cfa2c2c6b498
  slot 0   : ServiceFound dispid=3
  slot 1   : ServiceLost dispid=4
  slot 2   : ServiceResolved dispid=5
  slot 3   : OperationFailed dispid=11
```

When no type library is registered the dispids stay unknown and the emitted
methods carry no `DispIdAttribute` — the sink still Advises, but a late-bound
caller could not find its methods by dispid.

`ComEventsHelper` (mscorlib) is left as the stub it is: nothing on this path
calls it, and its `(rcw, iid, dispid)` signature carries no interface to build a
sink from.

## Status and what is still unproven

- Managed event path: **verified** (`ShimProbe`, 12/12 checks).
- COM path against real Bonjour: **verified** — all four `ComAwareEventInfo`
  hooks Advise successfully (`../dircon/BonjourProbe.exe`), and the game's own
  API comes up (`../dircon/TestDircon.exe`).
- Sink *delivery* — Bonjour calling back into the emitted sink through Mono's
  CCW — is **unverified**, because mDNSResponder under Wine never receives a
  packet, so no event is ever raised. Mono's CCW `IDispatch::Invoke` support is
  the open question; if it turns out to be missing, the COM server of step 2
  should call the sink early-bound through the vtable instead. That is the first
  thing to check when the fake sensor of step 2 exists.
- This is a prototype in the sense the assessment meant it: it settles *where*
  the fix goes and proves the route, but distributing it means shipping a
  patched wine-mono (or moving to the IL-rewrite option). The honest upstream
  fix is two separate patches — `ComAwareEventInfo` in wine-mono's
  `System.Core`, and the `method->slot < nslots` assertion in mono's icall
  layer, which no managed shim can work around.

Both probes and the install were run against a throwaway prefix
(`~/Games/dircon-test`, Bonjour installed from the game's own
`bonjoursdksetup.exe`). `~/Games/mywhoosh` was only read from.
