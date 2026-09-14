# dist — what the Lutris installer downloads

Three built assemblies, committed on purpose. Everything else in this
repository is built from source on the machine that uses it; these are the
exception, because the Lutris installer runs on a user's machine, which has no
Mono compiler and no checkout of this repository. `lutris/mywhoosh.yml` fetches
them by raw URL from this branch, so **what is committed here is what people
install**.

| File | Built from | What it does |
|---|---|---|
| `Windows.dll` | `../bleshim/src/` | The assembly the game loads instead of WinRT |
| `System.Runtime.WindowsRuntime.dll` | `../bleshim/src/` | The one member the game needs to `await` a WinRT call |
| `MyWhooshShim.dll` | `../exportshim/` | The four device-list exports wine-mono cannot marshal |
| `MANIFEST` | `../dist.sh` | Date, source commit, compiler, SHA-256 of the three |

## Refreshing

```sh
./dist.sh          # from the repository root
git add dist && git commit
```

`dist.sh` rebuilds both components and rewrites `MANIFEST`. Do it in the same
commit as any change under `bleshim/src/` or `exportshim/`, or users will
install a shim that does not match the sources next to it — the kind of
mismatch that takes a whole ride to notice and an evening to explain.

`MANIFEST` records the commit it was built from, and says `+dirty` when the
tree had uncommitted changes, which is the one way to tell afterwards whether a
build corresponds to anything.

Keep `GAME_LIBS` set when you run it: `bleshim/build.sh` then checks the shim's
surface against the game's own metadata, and a member missing from a release
build is a `MissingMethodException` in the middle of someone's ride.
