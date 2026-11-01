# Bonjour compatibility

This adds real Bonjour COM event subscriptions to Wine Mono without modifying
MyWhoosh or the installed Wine runtime. Bonjour remains enabled. It is not a
replacement for Apple's service, and it does not switch the prefix to Microsoft
.NET (which breaks the existing WinRT Bluetooth shim).

## Requirements

- A 64-bit Wine prefix and Wine Mono with `mcs.exe` and Mono.Cecil 0.11.x bundled.
  Wine Mono 11.3.0 is the tested runtime. Other runtimes must pass setup's checks.
- Apple's Bonjour, obtained under Apple's terms. Lutris downloads the official
  [Bonjour Print Services for Windows v2.0.2 package](https://support.apple.com/en-us/106380)
  without an Apple account, verifies its pinned SHA-256, and extracts
  `Bonjour64.msi`. Complete Apple's installer and accept its licence when it
  opens. `ADDLOCAL=ALL` includes the required COM components; neither the printer
  wizard nor Apple Software Update is installed. The project does not
  redistribute Apple binaries. Merely running Linux Avahi is not sufficient.
- Lutris's 7-Zip extractor, supplied by its runtime or the system's `7zip`/`p7zip`
  package. No manual extraction is needed.
- For building a release: `mcs`, plus the existing project's build prerequisites.
  End users do not need a host Mono installation: setup uses the selected Wine
  Mono runtime's own compiler and Cecil library.

## Installation

Both Lutris recipes download and install Bonjour and run `SetupBonjour.exe`
before downloading the game. This catches missing COM components or an
unsupported Mono runtime before the large download. The release archive contains:

- `BonjourCompat.dll`: the managed event adapter, with no game dependency.
- `SetupBonjour.exe`: runtime discovery, local generation and health checks.
- `PatchComWrapper.cs`: source compiled against the installed runtime's Cecil.
- `mywhoosh-bonjour.sh`: a launch guard around Lutris's normal game command.

Setup writes only below `$GAMEDIR/bleshim/bonjour`. It checks that every method
of Mono's `ComAwareEventInfo` has the known throwing-stub implementation, then
generates a copy of `System.Core.dll` forwarding those methods to the adapter.
Unknown implementations, already-patched inputs, missing tools and native .NET
are errors, not an instruction to replace the runner's framework.

The generated framework is loaded through this game's `MONO_GAC_PREFIX`;
`MONO_PATH` continues pointing at `bleshim`, where the adapter and Bluetooth
assemblies live. Ordinary `MONO_PATH` alone did not override `System.Core` in
the tested Wine hosting configuration.

Every launch checks input and output SHA-256 fingerprints. A changed framework,
compiler, Cecil library, adapter, patcher source or setup tool triggers a rebuild.
Generation and a child-process smoke test finish before the cache is replaced.
The old cache is retained if generation fails, but the launch guard will not
start the game in that case. No global DLL override or runner file is changed.

Lutris 0.5.14 ignores a prelaunch command's failure status, so the guard uses
`system.prefix_command`. The existing Bluetooth prelaunch/postexit hooks stay
in place; do not remove them. Setup and subscription failures are reported in
the normal `MYWHOOSH_SHIM_LOG` and Lutris launch output.

### Download failures and offline use

The recipes pin Apple's HTTPS download and its SHA-256. A changed package must
be reviewed and retested before updating that hash; do not bypass verification.
The download is Apple's legacy 2010 package, still linked from its support page,
not a claim that Apple provides a current Windows release or ongoing updates.

If the download is unavailable, obtain the same `BonjourPSSetup.exe` from
[Apple's support page](https://support.apple.com/en-us/106380) on another machine
and choose **Select File** for Bonjour in Lutris's file-source screen. The same
checksum and automatic extraction apply.

An existing SDK `Bonjour64.msi` remains a manual fallback for a local recipe:
replace the `bonjour` file entry with
`'N/A:Select Apple Bonjour64.msi from the Bonjour SDK'`, remove only the
`extract` step whose `file` is `bonjour`, and set the Bonjour `msiexec` arguments
to `/i "$bonjour" ADDLOCAL=ALL /qf /norestart`. Keep `SetupBonjour.exe` and the
launch guard enabled. Do not select an MSI in place of the default EXE download;
its checksum and archive format are intentionally different.

## Build and test

```bash
bash bonjour/build.sh
WINE=/path/to/runner/bin/wine WINEPREFIX=/path/to/existing-prefix \
  bash bonjour/test.sh
```

The prefix must have Bonjour installed and a working graphical Wine session.
The test registers a temporary **local-only** DNS-SD service, not a LAN service,
and removes it afterward. It exercises a real COM callback, typed COM arguments,
enum conversion, duplicate-handler removal, unsubscription, and synthetic
dispatch of the other supported events. It also checks setup cache reuse and
the framework's managed-event forwarding. It does not launch MyWhoosh.

To additionally check the game's actual event delegates and `OBM_Initialize`:

```bash
WINE=/path/to/runner/bin/wine WINEPREFIX=/path/to/existing-prefix \
  MYWHOOSH_TEST_SHIM_DIR=/path/to/prefix/bleshim \
  bash bonjour/test.sh 'Z:/path/to/WindowsConnectivity.dll'
```

The game assembly is read, never modified or included in the repository. The
test asserts that `isBonjourEnabled` is true, so skipping OpenBike initialization
cannot produce a false pass.

If no host `mcs` is installed, a build shell can use Wine Mono's compiler:

```bash
export WINE=/path/to/runner/bin/wine
export WINEPREFIX=/path/to/existing-prefix
export WINE_MONO_ROOT=/path/to/wine-mono-11.3.0
mcs() {
  env -u MONO_GAC_PREFIX -u MONO_PATH "$WINE" \
    "$WINE_MONO_ROOT/lib/mono/4.5/mcs.exe" "$@"
}
export -f mcs
bash dist.sh
```

## Local installer before publication

The recipes pin the proposed `v0.1.5` release. They cannot download its archive
until that release is published. To test a local build
without publishing or modifying your normal game entry:

1. Run `bash dist.sh` and note the archive path it prints.
2. Generate copies of both recipes that extract the local archive directly
   (requires PyYAML, already used by distro Lutris):

```bash
python3 - /absolute/path/to/mywhoosh-bleshim-VERSION.tar.gz <<'PY'
from pathlib import Path
import sys
import yaml

archive = Path(sys.argv[1]).resolve(strict=True)
for source in Path("lutris").glob("*.yml"):
    recipe = yaml.safe_load(source.read_text())
    recipe["name"] += " (Bonjour local)"
    recipe["slug"] += "-bonjour-local"
    recipe["game_slug"] += "-bonjour-local"
    recipe["script"]["files"] = [
        entry for entry in recipe["script"]["files"] if "shim" not in entry
    ]
    for step in recipe["script"]["installer"]:
        if step.get("extract", {}).get("file") == "shim":
            step["extract"]["file"] = str(archive)
    target = Path("build") / (source.stem + "-local.yml")
    target.write_text(yaml.safe_dump(recipe, sort_keys=False))
    print(target)
PY
lutris -i build/mywhoosh-local.yml
```

The local archive bypasses Lutris's downloader: Lutris 0.5.14 does not support
`file://` downloads. Bonjour is still downloaded and verified normally.

Choose a **new destination prefix** and complete Apple's Bonjour installer when
it opens. No local MSI is required. A full game installation needs substantial
free space. Do not use `lutris/debug.sh`
as a shortcut without reading it: it removes its previous debug installation.

## Publishing

Review and commit all source, tests, documentation and installer changes first.
Publishing is a separate, explicit action:

```bash
./dist.sh --release v0.1.5
```

The script derives the GitHub repository from `origin`; `RELEASE_REPOSITORY`
can override it with `owner/repository`. It updates both recipes' repository
and release pins and asks for a commit if they changed. No patched framework,
Apple MSI, game DLL, prefix, personal path or session log belongs in the release.

## Limits and rollback

Only Bonjour's `ServiceFound`, `ServiceLost`, `ServiceResolved` and
`OperationFailed` events are implemented. Other COM interfaces/events are
explicitly rejected. This is not a general implementation of `ComEventsHelper`.
Callbacks catch and log exceptions so they do not escape into native code.
Subscriptions are retained until their handlers are removed or the process
exits; callers must unsubscribe to release them during a long-lived process.

Validated on 2026-10-31: Wine Mono 11.3.0, Lutris 0.5.14, the original
MyWhoosh 6.2.3.0 event delegates, a fresh Bonjour-only prefix, the packaged setup,
and launch-guard success/failure. The user reported the prototype working in
MyWhoosh. A fresh standard-edition installation through Lutris downloaded and
verified MyWhoosh 6.2.3.0, passed Bonjour and Bluetooth setup checks, and launched
successfully. The tester also reported normal operation on subsequent launches.
HD gameplay, every native Bonjour event and physical trainer/resistance behavior
have not been independently validated. Flatpak has not been tested with this
addition.

The automatic Apple download was also validated on 2026-10-31: the pinned
package passed checksum verification and Lutris 0.5.14 extraction; its
`Bonjour64.msi` installed with all features in a fresh prefix and passed the
real local-only COM callback, typed arguments, duplicate removal, unsubscription
and synthetic-event tests. The original-game delegate tests above used the SDK
MSI; they have not been repeated with this package.

For rollback, close the game, remove this game's `prefix_command` and
`MONO_GAC_PREFIX`, then remove only `bleshim/bonjour` and the four Bonjour files
listed above. Keep the existing Bluetooth assemblies, `MONO_PATH`, and helper
hooks. The original framework needs no restoration. **Removing the adapter
does not make the old Mono/Bonjour combination compatible**: with Bonjour
running, the original startup exception can return. Rollback does not stop,
disable or uninstall Bonjour.

If Bonjour was previously disabled, enable its startup setting and restart that
prefix before retrying. Some Wine versions do not implement `sc config`, and
changing the registry alone may leave the service manager using its cached
disabled state. Never restart a prefix while its game is running.