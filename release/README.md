# Making a release

`make-release.sh` builds every component and packs the prebuilt artifacts into
`release/dist/mywhoosh-connectivity-<version>.tar.gz`, plus a `.sha256`. The
version is `git describe`, so tag first:

```sh
git tag -a v0.2.0 -m "connectivity stack"
release/make-release.sh
```

Building it needs `mcs` (mono-devel), `x86_64-w64-mingw32-gcc` (mingw-w64),
`cc` and `python3`. *Installing* it needs none of those — that is the point of
the bundle, and `lutris/mywhoosh-connectivity.yml` relies on it: a user with
Lutris and nothing else installed can run that installer.

Publish the tarball as a GitHub release asset, then point the `files:` URL in
`lutris/mywhoosh-connectivity.yml` at the new tag. Until an asset exists at that
URL the installer cannot work; test the bundle locally first:

```sh
tar -C /tmp -xf release/dist/mywhoosh-connectivity-*.tar.gz
/tmp/mywhoosh-connectivity-*/install.sh <prefix>
/tmp/mywhoosh-connectivity-*/install.sh --verify <prefix>
```

## What is in the bundle and what is not

Shipped prebuilt: `MyWhoosh.ComEventShim.dll`, `PatchSystemCore.exe`, the two
winmd stubs, `MyWhooshShim.dll`, `fakebonjour.dll`, `bonjourstub.exe`,
`dotlocal_shim.so` (with its source, since a foreign build may not load and
`install.sh` then rebuilds it), and `blebridge.py`.

Deliberately absent:

- **a patched `System.Core.dll`.** The prefix's own wine-mono copy is rewritten
  at install time by running `PatchSystemCore.exe` under Wine, which produces a
  byte-identical result to the Linux-side build. That keeps the bundle
  independent of the runner's wine-mono version and redistributes nothing
  derived from wine-mono.
- **`Mono.Cecil.dll`.** Taken from the prefix's own wine-mono tree.
- **anything from the game, and anything of Apple's.**
