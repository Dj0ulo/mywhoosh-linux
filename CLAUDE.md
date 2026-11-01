# mywhoosh-linux

Runs MyWhoosh under Wine and answers its own WinRT Bluetooth calls from BlueZ.
`CONTRIBUTING.md` maps the repo; `bleshim/` and `exportshim/` each have an
`ENGINEERING.md` — read them only when working there.

- **Never modify a file in the game's directory.** The game hashes
  `WindowsConnectivity.dll` and silently stops loading it on any byte change.
  Work only by adding files to the prefix or writing our own process memory.
- **Mono is not the CLR.** wine-mono has no WinRT projection, different BCL
  assembly splits, stricter marshalling, and treats Unix paths as Windows ones.
  Test in the prefix, not on the host.
- **Nothing may throw across a native boundary** — it crashes the process.
  Entry points catch, log, and return something harmless.
- **Read the IL before deciding:** `tools/ildump.sh` (what the game does),
  `winmd/members.py` (what it references).
- **Keep Bonjour working, not hidden.** A running `"Bonjour Service"` requires
  the per-game compatibility adapter in `bonjour/` for Wine Mono's COM event
  path. Never disable Bonjour to make a test pass or replace the runner's
  installed framework. See `bonjour/README.md` for setup and callback tests.
