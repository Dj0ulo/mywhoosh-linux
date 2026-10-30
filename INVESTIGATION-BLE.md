# Investigation -- MyWhoosh's interface freezes and the Bluetooth chain

Report produced on the `fix/ble-ui-freezes` branch of the `mywhoosh-linux`
repository. It describes the causal chain behind the UI freezes ("Steam app
not responding" on the pairing screen), the evidence gathered from the game's
own code, the fixes implemented in the shim, the before/after measurements,
and the local deployment.

All Bluetooth addresses in this document are partially masked (`XX:…`) except
the dummy addresses of the test bench (`AA:BB:CC:DD:EE:0x`), which do not
exist.

---

## 1. Observed symptom

- The sensor selection screen — and more broadly the game UI — freezes
  regularly ("the application is not responding"), especially when a device
  attempts to connect.
- Correlated in `session.log`: every freeze window coincides with a connection
  in progress (example: iPhone `XX:…` 19:40:47 → 19:41:02, ≈ 15 s of `connect`
  in progress during the freeze).
- The request stream from the engine towards the Bluetooth module runs at
  ≈ 3 requests/s (successive IDs 1205 → 1386 over ~8 minutes), which matches
  exactly the rate at which the blocking state polling can strike.

The freeze is not random: it happens *during* connections. It is a
thread-blocking problem, not a logic bug.

---

## 2. Causal chain (IL evidence)

Everything was established by reading the game's own code, disassembled from
its IL — the IL dumps produced by `tools/ildump.sh`.

### Cause A — every helper operation on a single thread: the 30 s connect freezes everything

The old `blehelper.py` served **every** request (`radios`, `connect`, `read`,
`write`, notifications…) in the same GLib loop, over the same D-Bus
connection. On the BlueZ side, `dev.Connect()` is a **synchronous D-Bus call
bounded by BlueZ's link timeout (≈ 30 s)** — the behaviour of a sleeping
trainer that has to be woken up.

During those seconds, the loop stopped answering anything:

- the shim's `radios` requests went unanswered until the end of the connect;
  every late answer could cost up to the shim's bound, i.e. **40 s** per query
  (`CallTimeoutMs`);
- scans were no longer started or stopped, and notifications no longer flowed.

The engine absorbs this delay at the worst possible spot — cause B.

### Cause B — the UI polling of the Bluetooth state waits synchronously

`FunctionsManager.MyWhoosh::BT_GetModuleState` (`il_funmgr.txt:56-59`) is a
plain forward to `BluetoothProgram::IsBluetoothEnabled`:

```
IL_0000: ldsfld    FunctionsManager.MyWhoosh::program
IL_0005: callvirt  BluetoothManager.BluetoothProgram::IsBluetoothEnabled
IL_000a: ret
```

and `IsBluetoothEnabled` (`il_bluetoothprogram.txt:2-8`) does exactly this:

```
IL_0001: call       ...BluetoothProgram::CheckRadioState      // returns Task<bool>
IL_0008: callvirt   System.Threading.Tasks.Task::Wait         // SYNCHRONOUS wait
IL_000e: callvirt   Task`1::get_Result
```

This is the **only** Bluetooth sync-over-async wait in the whole DLL (checked
against the complete IL: dozens of `await`, a single `Task::Wait`). The engine
calls it several times per second from its UI thread: each time, the UI thread
blocks until the radios answer arrives. Combined with cause A: as soon as a
30 s `Connect()` was running, the UI thread absorbed the series of
`Task::Wait`s — hence "the application is not responding".

### Cause C — the "pre-completed" WinRT operations ran the work on the calling thread

The old shim built every `IAsyncOperation` **already completed**
(`Task.FromResult`) and did the work *beforehand*: `FromBluetoothAddressAsync`
therefore called `Backend.Call("op","connect",…)` — up to 30-40 s —
synchronously, before handing control back to the caller. Even without cause
A, the whole chain discovery → connect → services → characteristics ran on
the game's calling thread.

But the game's chain is **async-void**:
`ConnectivityConstants.ExtensionMethods::Await` (`il_all.txt:8213`) is built
on `AsyncVoidMethodBuilder` — fire-and-forget, no exception surfaced, nothing
to wait on. A 30 s wait therefore went unnoticed there, producing the freeze
described above.

One case forces the exception to this rule — still `IsBluetoothEnabled`: if
`GetRadiosAsync` *actually deferred*, then the `Task.Wait()` of cause B would
block a thread that waits forever for a continuation that this very thread
would have to execute — a permanent deadlock. **Radios is thus the only
operator left inline**, and it stays instant because the helper answers it
from an in-memory cache on its main thread (the mirror of cause D, which
protects the same question on the game side).

### Cause D — hammering BlueZ: the 5 s retry loop

`BluetoothSensor::DidFailToCreateBluetoothDevice`
(`il_bluetoothsensor.txt:48-75`) restarts a connection attempt every
5 seconds, forever:

```
IL_0030: call      BluetoothProgram::IsBluetoothEnabled     // implicit Task::Wait
IL_0043: brtrue.s  IL_0052
IL_0052: ldc.i4    5000
IL_0057: call      System.Threading.Tasks.Task::Delay
IL_005c: ldftn     <DidFailToCreateBluetoothDevice>b__27_0  // → Connect() again
```

This is correct on the game side (a device can wake up), but against BlueZ it
meant: **one `dev.Connect()` attempt every 5 s towards a device that cannot
connect** — for example an iPhone that advertises but never resolves its GATT
services. Effects: hammering of the controller, and repeated pairing prompts
on the phone without anyone ever asking for them.

### Cause E — log pollution and a memory leak during workouts

- `BT_GetScannedDevicesList -> N device(s)` logged 1×/s by the export shim
  (issue #10): megabytes for constant information.
- With every device-list "poll", the export shim allocated a fresh `CoTaskMem`
  block and never freed it — memory left unclaimed every second.

---

## 3. Fixes

### `bleshim/blehelper.py` — two threads with clean-cut roles

- **GLib loop (main thread)**: client socket, instant answer to `radios` from
  an in-memory cache maintained by the BlueZ signals (`InterfacesAdded` /
  `PropertiesChanged`), alternation of scans (`StartDiscovery` /
  `StopDiscovery`), and writing of every answer and event from an outbox.
- **Worker (daemon thread, its own D-Bus connection)**: the potentially slow
  operations (`connect`, `disconnect`, `services`, `chars`, `read`, `write`,
  `notify`) in a strict FIFO queue, never interrupted, wrapped in a catch-all
  (a killed worker would leave every following request unanswered). Its
  answers return to the loop through a queue; it never touches the socket.
- **Single scan state** shared under a lock; a connect that needs to see an
  advert (`wait_for_advert`, 20 s) can start discovery itself — which turns
  "device revealed" into an event instead of a failure — and restores
  discovery to its previous state if it was the requester.
- **Rest ladder (quiescence)**: every failed `connect` puts that address to
  rest 10 s → 20 s → 40 s… capped at 120 s; every success or every
  `disconnect` clears it. The main loop refuses on the spot any request for
  an address at rest, before it reaches the worker: the game's 5 s retry loop
  no longer hammers BlueZ and no longer re-asks for pairing in a loop.

### `bleshim/src/Backend.cs` — differentiated budgets and fallback results

- `InlineTimeoutMs = 2500`: the only operation allowed on the UI thread
  (radios), bounded; on failure, the answer comes from the **last known
  snapshot** rather than nothing — keeps Bluetooth from "blinking" during a
  long connect.
- `CallTimeoutMs = 40000`: the generic budget for the operations now off the
  UI thread.
- `ConnectTimeoutMs = 75000`: the connect budget, aligned with the real work
  (20 s advert wait + BlueZ link timeout ~30 s + GATT resolution).
- `CallBounded(timeoutMs, …)`: the same machinery with a configurable budget;
  `Call` no longer serves the connect.
- `Run<T>(work, fallback)`: tasks are produced through
  `Task.Factory.StartNew` (a primitive the prefix's BCL actually has;
  `TaskCompletionSource` was deliberately avoided), and no exception ever
  propagates from the game's async-void chains — every failure takes a
  defined **failure shape** (null device, `GattCommunicationStatus.Unreachable`,
  empty list).
- The `helper refused …` message is dampened (one change, otherwise 10 s of
  silence) now that the ladder answers "refused" from the first failure.

### `bleshim/src/Windows.cs` — inline **only** when mandatory

- `Radio.GetRadiosAsync`: stays inline (cause B), documented, bounded.
- `FromBluetoothAddressAsync`, `GetGattServicesAsync`,
  `GetCharacteristicsAsync`, `ReadValueAsync`, `WriteValueAsync`,
  `WriteValueWithResultAsync`,
  `WriteClientCharacteristicConfigurationDescriptorAsync`, `Dispose`:
  genuinely defer through `Backend.Run`; the calling thread hands back
  control in milliseconds while the task continues on the thread pool.
- The watcher (scan Start/Stop) stays inline but **bounded to 5 s** — the
  helper answers it in milliseconds; the bound protects the UI from a frozen
  helper.
- `BluetoothLEDevice.Dispose`: the `ConnectionStatusChanged` signal is
  delivered by the shim itself (the object leaves the `Live` registry, the
  D-Bus event would no longer find it), then the real teardown of the link
  runs as a task.

### `exportshim/ExportShim.cs` — lean logging and double buffering

- Between polls, the poller logs only on a *change* of the device count (plus
  one housekeeping line per minute at constant count: the silence proves the
  poll is running).
- Two alternating `CoTaskMem` blocks, grown in place — no more
  one-allocation-never-freed per second; no more leak as the queries pile up.

---

## 4. Tests and measurements

The `bleshim/TestBle.cs` harness drives the shim exactly as the game does
(the same WinRT calls in the same order). The automated scenarios run without
any hardware (`fakehelper.py` = a helper that matches the protocol, two
virtual sensors, injected slowness). Exit code 0 only if every assertion
holds. Re-runnable: `./bleshim/regression.sh` (host mono) and
`./bleshim/winetest.sh` (wine-mono, real prefix).

### `--stub` — 6 s connect: the caller stays free

| Assertion | Measurement |
|---|---|
| `FromBluetoothAddressAsync` hands back control | **3 ms** (the task takes 6 s) |
| Connect completed | 6 035 ms (the helper kept its end) |
| Radios queries *during* the connect | **0-1 ms** each (30 queries) |
| `ReadValueAsync` declared (the helper holds for 350 ms) | 0 ms |
| `FromBluetoothAddressAsync` for the 2nd sensor | returned instantly |
| Disconnect event after `Dispose` | received |
| Power notifications flowing | arrived (≥ 3) |
| FTMS write + indication of the response | success |

### `--stub --slow-radios` — the immediacy bound protects the UI

A helper that takes **9 s** to answer `radios`; the shim cuts off at its own
bound:

| | |
|---|---|
| Poll cut off | **2 518 ms** (2500 ms budget) instead of 9 s |
| Answer | honest empty list ("Bluetooth off"), no blocking |

### `--quiescence` — ladder against the real helper, an address that goes nowhere

Address `02:00:00:00:00:AB` (nowhere, never):

| Attempt | Before (old code) | After |
|---|---|---|
| 1st | ~20 s then a new try 5 s later, indefinitely | **20 225 ms** (20 s advert wait), then refusal |
| 2nd (immediate) | ~20 s again | **26 ms** ("is being retried too soon (rest 9s)") |

### `--thin` under wine-mono — the prefix's BCL

GE-Proton wine, real prefix, `MONO_PATH` in Lutris's exact format (`Z:/…`):
the same assertions. **All hold** — connect declared in 3 ms, completed at
3 010 ms, radios 40 ms, GATT 2 services. This is the test that validates the
`Task.Factory.StartNew` / `Task.Wait` path where it has to work (wine-mono
11.3.0 shipped inside GE-Proton).

---

## 5. Local deployment (existing prefix, no reinstall)

The scripts live in `scripts/`:

```sh
./scripts/deploy.sh                       # backs up, then installs
./scripts/restore.sh                      # restores the most recent backup
```

What they do and do not do:

- **Backup** of the four active files of `~/Games/mywhoosh/bleshim/`
  (`Windows.dll`, `System.Runtime.WindowsRuntime.dll`, `MyWhooshShim.dll`,
  `blehelper.py`) into a timestamped folder next to them.
- **Install** of the new builds (`bleshim/build/`, `exportshim/build/`,
  `blehelper.py`) in their place; `mywhoosh-ble.sh` and the Lutris
  configuration are not touched (loading still happens through
  `MONO_PATH=Z:$GAMEDIR/bleshim` — no mono tree in the prefix, same as for
  the game).
- **Checks**: SHA-256 fingerprint of every installed file compared against
  the source build; fingerprint of the **game directory**'s
  `WindowsConnectivity.dll` compared before/after (must **never** change:
  the game refuses any modification, a single byte disables it); helper life
  cycle verified (start → port 27019 → stop → no residual process).

---

## 6. What remains to validate (real hardware — participation welcome)

The harness covers the mechanics; it cannot simulate BlueZ facing a real
TacX Flux 2 or a phone bridge. The field test remains, with the prefix
deployed:

1. Launch through Lutris; check `session.log` (helper lending a hand, no
   `Task.Wait` locking the UI during a connect, message);
2. Tacx discovery then connection on the pairing screen **while the UI stays
   responsive**;
3. ERG rides (writes into the FTMS control point) for ≥ 15 min;
4. HR sensor connected in addition to the Tacx, values received;
5. clean disconnect and reconnection.

No hardware was called upon during this work: no command was sent to the
Tacx during the automated tests — the harness only talks to the fake helper
over loopback.

---

## 7. Appendix — reproduction and references

```sh
# IL dumps of the game (basis of the evidence)
tools/ildump.sh                      # → the IL dumps produced by tools/ildump.sh

# full regression without hardware (host mono)
cd bleshim && ./regression.sh --stub && ./regression.sh --stub --slow-radios
mono build/TestBle.exe --quiescence  # vs the real blehelper, ~25 s

# the same run under wine-mono, real prefix (no hardware required)
cd bleshim && ./winetest.sh
```

Precise IL references:

| Fact | Where |
|---|---|
| `IsBluetoothEnabled` does a sync `Task::Wait` | `il_bluetoothprogram.txt:2-8` (`IL_0008: callvirt Task::Wait`) |
| `BT_GetModuleState` = direct `IsBluetoothEnabled` | `il_funmgr.txt:56-59` |
| `Await` = async-void (`AsyncVoidMethodBuilder`) | `il_all.txt:8213-8286` |
| Failure → retry every 5 s | `il_bluetoothsensor.txt:52-63` (`Task::Delay(5000)` + `ContinueWith → Connect`) |
| `DiscoverServices` fire-and-forget | `il_bluetoothsensor.txt:111-117` |

Branch: `fix/ble-ui-freezes` (base `062b0ab`, v0.1.3). Local modifications
only — nothing published, nothing pushed until the hardware validation
delivers its verdict.

## 8. Addendum Apple Watch / HR bridge (October 22 session, after the freeze) — work in progress

User goal: Apple Watch BPM (via the Pulse Replay bridge on an iPhone). The
freeze fixed in (1)-(4) is validated on hardware (Tacx Flux-2 connected,
subscribed, no retries; the user confirms "the UI no longer lags"). What
follows documents the hunt for the BPM values.

### 8.1 The watch exposes two 180D services

The GATT tree of the watch-named bridge entry contains **two** instances of the
Heart Rate service (same UUID):
- private instance: `2a37` with `read+notify` (the standard Apple instance,
  which demands an encrypted link from an unpaired client);
- "broadcast" instance: `2a37` with `notify` only (this is Apple's channel
  meant for third-party receivers during a workout — gym, etc.).

The shim↔helper protocol only carries UUIDs, not a service path: the old
helper therefore merged the characteristics of both instances (two `2a37`
and two `2a38` under a single service) and resolved every op against the
first sorted object.

### 8.2 Helper fix (commit `1a62f19` + service deduplication)

- `characteristics(addr, uuid)` answers with a **single instance** per
  duplicated UUID, chosen by score (a notifier without `read` — the
  broadcast profile — outranks the others; a tie → the most recent path).
- `read`/`write`/`notify` resolve against the candidates in the same order,
  filtered by the requested flags; `StartNotify` **switches to the next
  instance** if BlueZ returns an error from the encryption/pairing family.
- `services(addr)` now returns a single representative per duplicated UUID,
  keeping tree order (the game keeps its own services map by UUID; the
  duplicates silenced it).
- Every op and every answer is traced (`-v`): the live sessions show exactly
  what the game received.

### 8.3 Live proof (protocol probe, without touching the game)

Full replay of the game's sequence (`connect → services → chars ×11 →
notify 2a37 enable`) through a second helper: **the subscription succeeds
without pairing and the BPM values arrive** (`06 53` flags+83 bpm then
`06 56` 86). The three addresses seen (`7A:EC…6A`, `78:FD…39`,
`7A:BE…3B:12`, `60:0F…C2`) are Android/iOS address rotations — the alias
even flipped from the watch's Bluetooth name to plain "iPhone". The helper's
advert stream
relays every rotation to the game without a relaunch.

### 8.4 Remaining point: why the game never calls the subscription

In the live session the game walked the 11 services SEVEN times without ever
issuing a `notify` op (the Tacx, for its part, subscribes its 4 chars in
4 s). The shim's `WriteClientCharacteristicConfigurationDescriptorAsync`
logs every call or refusal; nothing for the watch — so the game decided
inside its own walk (method `DidDiscoverServicesAsync`: opaque in the IL
dumps because the disassembler does not resolve the Windows references).
Hypothesis under test: the duplicated services throw its services map into
disorder — the upcoming runs live with the deduplication; the game relaunch
(for the shim, see §4.3) is only requested when it matters.

### 8.5 The Pulse Replay chain (iPhone)

No iPhone advert carrying `180D` was received — the app is probably still
waiting for an action ("broadcast") before it starts transmitting. To be
confirmed with the user. The service deduplication + the instance ranking
also reflect THIS model (a single instantiated `180D`, strong notify
capability, no duplicates) on a Classic bridge.

### 8.6 Full resolution — the HR chain validated (22:27, October 22 session)

What the user sees now: the entry named after the watch (the Pulse Replay
bridge, which re-claims the watch's identity) → one click → "select my
bridge" → first-attempt connection, steady green; BPM at ~1 Hz in the sensor
card and then in the workout. The Tacx comes back on its own at every
relaunch.

The chain that held:
1. **BlueZ agent** (`org.bluez.Agent1` in the helper, `DisplayYesNo`,
   confirmation on the computer side is automatic, code logged): the `2a37`
   CCCD requires encryption; the SMP exchange used to land on nobody → the
   phone cut the link → a "pairing in progress" loop. Accepted once by the
   phone: **`Paired=1` permanent** — no more pairing prompts.
2. **Identity address**: BlueZ re-keys the bonded record onto the phone's
   identity address (`E8:5F:…:8A`, the iPhone) while the object lives under the
   path of its discovery RPA. The game remembers this address →
   `device_path()` now also resolves through the `Address` property.
3. **Service deduplication**: that was the original lock (the game's walk
   saw the `180d` twice and never got as far as the subscription).
4. **Resilience**: `ServicesResolved` can stay false while the tree is
   complete (a link that went through failovers) → tree as a second
   witness; the CCCDs survive helper failovers and an auto-healer re-arms
   any subscription whose `Notifying` would fall back.

Runtime behaviour: the helper re-reads its state on startup and re-arms by
itself; a Bluetooth blip (suspend, bluetoothd) heals without a relaunch.
The one remaining gesture is deliberate: tapping the bridge entry at every
game relaunch — the user prefers it this way, no change.

### 8.7 First cold boot — GATT cache incident, repaired by re-pairing

Cold-start trial (PC shut down, resumption "like a real session"): the Tacx
was perfect from the start; the bridge connected too, but `services`
answered **empty** — BlueZ 5.85 had trusted the bond's persisted UUIDs
without having a persisted GATT database (the previous day's initial
discovery had been coupled to the pairing race): `ServicesResolved` true
with zero services, hence nothing to subscribe, BPM at 0.

Repair (at protocol level): `bluetoothctl remove` of the record, and the
game's next connection redoes the GATT discovery from scratch — the
re-pairing goes through (the agent answers on the computer side, one
acceptance on the iPhone), the GATT cache is written properly this time,
and the BPM come back at 1 Hz (98/98/98/95 at 22:41).

The next cold boot should not have to go through this again. If a deviation
reappears, the traces of `session.log` will name the exact link in the
chain.

### 8.8 Four days later — the empty cache came back, so the helper now heals itself

It did reappear, and here is the shape it took. During a ride, the bridge
connected again and again (paired, bonded, trusted), and `services`
answered `[]` — twice, on two separate selections, while the Tacx enjoyed
its usual self-reconnect. The machine-side evidence ranked high enough not
to doubt: the device object under `org.bluez` carried nothing but local
audio endpoints (no `service00XX` at all), the record's UUID list held the
classic profiles only, and the journal showed zero GATT discovery activity
since the previous session.

The trap is the one "resolved with nothing": on some earlier session the
bridge had been connected while its heart-rate side was not serving (the
app on the phone had gone to sleep; an app-hosted GATT tree dies with the
app). BlueZ minted an empty cache, marked the services resolved, and from
then on trusted that emptiness over every later link — no rediscovery on
connect, none on reconnect, `[]` forever. The repair experienced on the
spot: remove the record, let the game's next tap select the bridge, accept
the one prompt on the phone — the discovery ran from zero, the cache was
written for real (`180d`/`180f` in the record, BPM back at 1 Hz at 18:55).

Answers worth keeping, though, because the incident graduated into code in
the same hour:

- **an empty `services` on a paired device is now diagnosed and healed**:
  the helper's worker, on that shape, lifts the record
  (`Adapter1.RemoveDevice` — the only lever that forces BlueZ to
  rediscover), and re-connects through the pairing agent, so the SMP half
  flows and the phone asks its owner to confirm once. No terminal ever
  needs to be involved again.
- **the walk that found the emptiness is never delayed**: it still
  receives its honest `[]` immediately, and the NEXT walk of that sensor —
  the user's next tap, the same gesture they make anyway — serves the
  rebuilt tree. Bounded: at most one extra tap, never a freeze.
- **a rate gap (90 s) fences the heal**, so a phone that is genuinely
  asleep cannot walk into a remove-and-repair loop and lose its bond for
  nothing every tap; a record whose rebuild stalled at "not on the air"
  stays lifted, and the helper pairs on sight of the bridge's return --
  opening the phone app is the user's whole part then, the prompt is the
  only thing left to tap, and a later tap of the sensor pairs the same
  way if the bridge never came inside the watch window.

The helper-side rule set is small on purpose: one constant, one method, one
dispatch hook — and it reads like the incident's trace.

