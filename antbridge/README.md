# antbridge

`libusb0.dll` for the Wine prefix, so the game's ANT+ code can use a USB ANT
stick.

MyWhoosh talks to ANT+ through `ANT_DLL.dll`, Garmin's ANT library, which
loads `libusb0.dll` (libusb-win32) at runtime to reach the stick. The real
libusb-win32 needs its own Windows kernel driver, and Wine has none, so the
game finds no stick and shows ANT+ as unavailable.

On Linux the kernel already turns the stick into a serial device
(`usb_serial_simple`, `/dev/ttyUSB*`). What travels over its USB bulk
endpoints is the plain ANT serial protocol, so this DLL presents one USB
device to `ANT_DLL` and turns every bulk transfer into a read or write on that
serial device. `ANT_DLL` itself is untouched, and so is everything else in the
game's directory.

## What it covers

Only the libusb-win32 calls `ANT_DLL` makes. Their names come from
`GetProcAddress` strings in the game's `ANT_DLL.dll`. Their expected behaviour
comes from Garmin's ANT SDK source for the same library: `-116` for a read that
timed out, and a pending read that survives the timeout.

The stick is found as `Z:\dev\serial\by-id\*Dynastream*`. Set `ANT_SERIAL_PORT`
to any Windows path (`\\.\COM5`, `Z:\dev\ttyUSB1`) to use another device.

Tested with an ANTUSB-m (`0fcf:1009`) under GE-Proton 11, through
umu-launcher. The ANTUSB2 (`0fcf:1008`) uses the same kernel driver and
protocol but has not been tried.

## Building

```sh
./build.sh
```

Needs `clang` and `lld` (`lld-link`, `llvm-dlltool`). There is no MinGW and no
C runtime involved: the DLL imports only `kernel32`, and `build.sh` generates
that import library from the declarations in `libusb0.c`.

## Installing

`dist.sh` puts the DLL in the release archive. The installers then unpack it
into `bleshim/` and add that directory to `WINEPATH`, which is where
`LoadLibrary("libusb0.dll")` finds it.

Users still need access to the serial device. `TROUBLESHOOTING.md` has the
udev rule.

## One Wine detail

Reads use overlapped I/O with `COMMTIMEOUTS` of a 10 ms interval and no total
timeout, and `WaitForSingleObject` enforces the caller's timeout. Under Wine,
the usual "return whatever is there within N ms" setting (`MAXDWORD`,
`MAXDWORD`, N) never completes an overlapped read on a serial port: every read
timed out with the stick answering.
