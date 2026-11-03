# The MyWhoosh runtime: Wine, DXVK, vkd3d-proton, the Bluetooth helper's
# Python, and one release archive of this repository.  NOT the game.
#
# The prefix and the game live in a volume, one per edition, which
# mywhoosh-update.py keeps current at each launch -- so a game release never
# rebuilds this image, and only `dist.sh --release` does (the image's tag is the
# release's).  See README.md here.
#
#   ./build.sh v0.1.5      the release's archive, as CI builds it
#   ./build.sh             this checkout, through ../dist.sh, to try it first
#
# Either way the archive is copied into this directory and the image installs
# exactly that file: the release asset in CI, never a separate fetch of any
# file in it.

ARG DEBIAN_VERSION=trixie

FROM debian:${DEBIAN_VERSION}-slim AS fetch
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils zstd \
 && rm -rf /var/lib/apt/lists/*

# Wine's own .NET runtime, the version wine-staging 11.20's mscoree asks for.
# Installed where Wine looks for a shared copy (<prefix>/share/wine/mono), so
# creating a prefix neither downloads it nor asks -- and a missing one would
# leave the game without the runtime its connectivity DLL runs on.
ARG WINE_MONO_VERSION=11.3.0
ARG WINE_MONO_SHA256=54a1b0111c3fe4b785eae688af94d27e64995707ad648b6fee8000381b80d298
# The two translation layers Lutris installs: DXVK at the version its
# `dxvk: true` resolves to today, and vkd3d-proton 3.0.1 for HD's Direct3D 12
# (lutris/mywhoosh-hd.yml says why not Lutris' v2.14).
ARG DXVK_VERSION=2.6.2
ARG DXVK_SHA256=17761876556afd55736cb895d184f5a1c55d43350f1b1e3b129f8d28706d7992
ARG VKD3D_VERSION=3.0.1
ARG VKD3D_SHA256=3cf2315522af5e43605ef6d3c41dad91387040bf97199934f3f7ab76caaa2f0c

WORKDIR /fetch
RUN set -e; get() { curl -fsSL -o "$1" "$2"; echo "$3  $1" | sha256sum -c -; }; \
    get mono.tar.xz "https://dl.winehq.org/wine/wine-mono/${WINE_MONO_VERSION}/wine-mono-${WINE_MONO_VERSION}-x86.tar.xz" "$WINE_MONO_SHA256"; \
    get dxvk.tar.gz "https://github.com/doitsujin/dxvk/releases/download/v${DXVK_VERSION}/dxvk-${DXVK_VERSION}.tar.gz" "$DXVK_SHA256"; \
    get vkd3d.tar.zst "https://github.com/HansKristian-Work/vkd3d-proton/releases/download/v${VKD3D_VERSION}/vkd3d-proton-${VKD3D_VERSION}.tar.zst" "$VKD3D_SHA256"; \
    mkdir -p out/mono out/dxvk out/vkd3d; \
    tar -xJf mono.tar.xz -C out/mono; \
    tar -xzf dxvk.tar.gz -C out/dxvk --strip-components=1; \
    tar --zstd -xf vkd3d.tar.zst -C out/vkd3d --strip-components=1

# The release archive: the shim, the helper, mywhoosh-ble.sh, the updater and
# the downloader it vendors, as one build (dist.sh says why one).
ARG SHIM_RELEASE
RUN test -n "$SHIM_RELEASE" || { echo "--build-arg SHIM_RELEASE=<tag> is required" >&2; exit 1; }
COPY mywhoosh-bleshim-${SHIM_RELEASE}.tar.gz /fetch/shim.tar.gz
RUN mkdir out/bleshim \
 && tar -xzf shim.tar.gz -C out/bleshim --strip-components=1 \
 && grep -qx "version ${SHIM_RELEASE}" out/bleshim/MANIFEST


FROM debian:${DEBIAN_VERSION}-slim
ARG DEBIAN_VERSION
ARG DEBIAN_FRONTEND=noninteractive

# WineHQ's wine-staging, pinned.  Why this Wine:
#   - it runs the game: 11.20 is what the Lutris installs here create their
#     prefixes with, and the version the shim was last ridden on;
#   - it is an apt package, so its libraries come with it -- a GE-Proton
#     build expects the Steam runtime (or Lutris') to supply libavcodec,
#     libpcap and a dozen more, which nothing in a plain Debian image does;
#   - it has the wayland driver (since 9.x), and WineHQ keeps every past
#     build in its repository, so the pin keeps resolving.
# It does not ship wine-mono; the matching one is added above, by hash.
ARG WINE_VERSION=11.20~${DEBIAN_VERSION}-1

# - python3-dbus, python3-gi: blehelper.py (BlueZ over D-Bus, GLib main loop)
# - zenity: the updater's progress window, on the display we already forward
# - mesa-vulkan-drivers, libvulkan1: DXVK and vkd3d-proton on Intel/AMD
#   (NVIDIA's userspace comes from nvidia-container-toolkit at run time)
# - procps: pkill/pgrep for the entrypoint
RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gpg \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://dl.winehq.org/wine-builds/winehq.key | gpg --dearmor -o /etc/apt/keyrings/winehq-archive.key \
 && curl -fsSL -o /etc/apt/sources.list.d/winehq.sources \
        "https://dl.winehq.org/wine-builds/debian/dists/${DEBIAN_VERSION}/winehq-${DEBIAN_VERSION}.sources" \
 && apt-get update \
 && apt-get install -y \
        wine-staging=${WINE_VERSION} \
        wine-staging-amd64=${WINE_VERSION} \
        wine-staging-i386=${WINE_VERSION} \
 && apt-get install -y --no-install-recommends \
        python3 python3-dbus python3-gi gir1.2-glib-2.0 \
        zenity \
        libvulkan1 mesa-vulkan-drivers libgl1 libegl1 \
        libpulse0 \
        procps \
 && apt-get purge -y gpg curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY --from=fetch /fetch/out/mono /opt/wine-staging/share/wine/mono
COPY --from=fetch /fetch/out/dxvk /opt/dxvk
COPY --from=fetch /fetch/out/vkd3d /opt/vkd3d-proton
COPY --from=fetch /fetch/out/bleshim /opt/mywhoosh/bleshim
COPY entrypoint.sh /opt/mywhoosh/entrypoint.sh

# NVIDIA: what nvidia-container-toolkit injects (a no-op without it).
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=all \
    PATH=/opt/wine-staging/bin:$PATH \
    WINEDEBUG=-all \
    WINEARCH=win64

# The volume's mount point.  Mode 1777 because a new named volume takes the
# mode of the directory it is first mounted over, and the container runs as
# the host user -- whose uid the image cannot know -- not as root.
RUN mkdir -m 1777 /data
VOLUME /data

LABEL org.opencontainers.image.title="mywhoosh-linux" \
      org.opencontainers.image.description="MyWhoosh under Wine, with Bluetooth from BlueZ. The game is downloaded into a volume at first launch." \
      org.opencontainers.image.source="https://github.com/Dj0ulo/mywhoosh-linux"

ENTRYPOINT ["/opt/mywhoosh/entrypoint.sh"]
