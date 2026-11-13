/* libusb0.dll for the game's ANT_DLL.dll, inside the Wine prefix.
 *
 * ANT_DLL drives a Garmin/Dynastream ANT USB stick through libusb-win32
 * (libusb0.dll), whose kernel driver does not exist under Wine.  On Linux the
 * kernel already exposes the stick as a serial device (usb_serial_simple,
 * /dev/ttyUSB*), and the bytes on its bulk endpoints -- 0x01 out, 0x81 in --
 * are the plain ANT serial protocol.  So this library presents exactly one
 * USB device to ANT_DLL and turns its bulk/interrupt transfers into reads and
 * writes on that serial device.
 *
 * The stick is found as Z:\dev\serial\by-id\*Dynastream*, so no COM port has
 * to be configured.  ANT_SERIAL_PORT overrides it with any Windows path
 * (\\.\COM5, Z:\dev\ttyUSB1).
 *
 * Only kernel32 is imported and there is no C runtime, so the DLL works in any
 * prefix.  Struct layouts follow libusb-win32's usb.h, which packs them all
 * to one byte.  Return values follow libusb-win32 too: -116 (ETIMEDOUT) is
 * what ANT_DLL's receive loop expects when nothing arrived in time. */
#include <stddef.h>

typedef void *HANDLE;
typedef unsigned long DWORD;
typedef int BOOL;
typedef unsigned char BYTE;
typedef unsigned short WORD;
typedef struct { void *Internal, *InternalHigh; DWORD Offset, OffsetHigh; HANDLE hEvent; } OVERLAPPED;
typedef struct { DWORD ReadIntervalTimeout, ReadTotalTimeoutMultiplier, ReadTotalTimeoutConstant,
                 WriteTotalTimeoutMultiplier, WriteTotalTimeoutConstant; } COMMTIMEOUTS;
typedef struct { DWORD DCBlength, BaudRate, flags; WORD wReserved, XonLim, XoffLim;
                 BYTE ByteSize, Parity, StopBits; char XonChar, XoffChar, ErrorChar, EofChar, EvtChar;
                 WORD wReserved1; } DCB;
typedef struct { DWORD dwFileAttributes; DWORD ftCreation[2], ftAccess[2], ftWrite[2];
                 DWORD nFileSizeHigh, nFileSizeLow, dwReserved0, dwReserved1;
                 char cFileName[260]; char cAlternateFileName[14]; } WIN32_FIND_DATAA;

#define IMP __declspec(dllimport) __stdcall
IMP HANDLE CreateFileA(const char *, DWORD, DWORD, void *, DWORD, DWORD, HANDLE);
IMP BOOL CloseHandle(HANDLE);
IMP BOOL ReadFile(HANDLE, void *, DWORD, DWORD *, OVERLAPPED *);
IMP BOOL WriteFile(HANDLE, const void *, DWORD, DWORD *, OVERLAPPED *);
IMP BOOL GetOverlappedResult(HANDLE, OVERLAPPED *, DWORD *, BOOL);
IMP BOOL CancelIoEx(HANDLE, OVERLAPPED *);
IMP HANDLE CreateEventA(void *, BOOL, BOOL, const char *);
IMP DWORD WaitForSingleObject(HANDLE, DWORD);
IMP DWORD GetLastError(void);
IMP void SetLastError(DWORD);
IMP BOOL SetCommTimeouts(HANDLE, COMMTIMEOUTS *);
IMP BOOL GetCommState(HANDLE, DCB *);
IMP BOOL SetCommState(HANDLE, DCB *);
IMP BOOL PurgeComm(HANDLE, DWORD);
IMP HANDLE GetProcessHeap(void);
IMP void *HeapAlloc(HANDLE, DWORD, size_t);
IMP BOOL HeapFree(HANDLE, DWORD, void *);
IMP DWORD GetEnvironmentVariableA(const char *, char *, DWORD);
IMP HANDLE FindFirstFileA(const char *, WIN32_FIND_DATAA *);
IMP BOOL FindClose(HANDLE);

#define INVALID_HANDLE_VALUE ((HANDLE)(long long)-1)
#define ERROR_IO_PENDING 997
#define WAIT_OBJECT_0 0
#define INFINITE 0xFFFFFFFF
#define ETIMEDOUT_ (-116)
#define EIO_ (-5)
#define ENODEV_ (-19)

/* No CRT, but the compiler may still emit calls to these. */
void *memset(void *d, int c, size_t n) { unsigned char *p = d; while (n--) *p++ = (unsigned char)c; return d; }
void *memcpy(void *d, const void *s, size_t n) { unsigned char *p = d; const unsigned char *q = s; while (n--) *p++ = *q++; return d; }

#pragma pack(push, 1)
struct usb_endpoint_descriptor { BYTE bLength, bDescriptorType, bEndpointAddress, bmAttributes; WORD wMaxPacketSize;
  BYTE bInterval, bRefresh, bSynchAddress; unsigned char *extra; int extralen; };
struct usb_interface_descriptor { BYTE bLength, bDescriptorType, bInterfaceNumber, bAlternateSetting, bNumEndpoints,
  bInterfaceClass, bInterfaceSubClass, bInterfaceProtocol, iInterface; struct usb_endpoint_descriptor *endpoint;
  unsigned char *extra; int extralen; };
struct usb_interface { struct usb_interface_descriptor *altsetting; int num_altsetting; };
struct usb_config_descriptor { BYTE bLength, bDescriptorType; WORD wTotalLength; BYTE bNumInterfaces,
  bConfigurationValue, iConfiguration, bmAttributes, MaxPower; struct usb_interface *interface;
  unsigned char *extra; int extralen; };
struct usb_device_descriptor { BYTE bLength, bDescriptorType; WORD bcdUSB; BYTE bDeviceClass, bDeviceSubClass,
  bDeviceProtocol, bMaxPacketSize0; WORD idVendor, idProduct, bcdDevice; BYTE iManufacturer, iProduct,
  iSerialNumber, bNumConfigurations; };
struct usb_bus;
struct usb_device { struct usb_device *next, *prev; char filename[512]; struct usb_bus *bus;
  struct usb_device_descriptor descriptor; struct usb_config_descriptor *config; void *dev;
  BYTE devnum, num_children; struct usb_device **children; };
struct usb_bus { struct usb_bus *next, *prev; char dirname[512]; struct usb_device *devices;
  unsigned long location; struct usb_device *root_dev; };
#pragma pack(pop)

typedef struct { HANDLE h, rd_ev, wr_ev; } usb_dev_handle;
typedef struct { usb_dev_handle *d; OVERLAPPED ov; char *buf; int size, pending; } async_ctx;

static struct usb_endpoint_descriptor eps[2] = {
  { 7, 5, 0x81, 2, 64, 0, 0, 0, 0, 0 },
  { 7, 5, 0x01, 2, 64, 0, 0, 0, 0, 0 } };
static struct usb_interface_descriptor alt = { 9, 4, 0, 0, 2, 0xff, 0, 0, 0, eps, 0, 0 };
static struct usb_interface iface = { &alt, 1 };
static struct usb_config_descriptor cfg = { 9, 2, 32, 1, 1, 0, 0x80, 50, &iface, 0, 0 };
static struct usb_bus bus;
static struct usb_device dev;
static char port[300];

/* ---------------------------------------------------------------- logging
 * One line per open, close and failure, appended to the session log the
 * Bluetooth shim already writes (MYWHOOSH_SHIM_LOG, a Unix path).  Never per
 * message: the stick talks several times a second for the whole ride. */
static HANDLE logh;
static void say(const char *msg, long v) {
  if (!logh) {
    char path[300] = "Z:";
    logh = GetEnvironmentVariableA("MYWHOOSH_SHIM_LOG", path + 2, sizeof path - 2)
         ? CreateFileA(path, 4 /* FILE_APPEND_DATA */, 3, 0, 4 /* OPEN_ALWAYS */, 0x80, 0)
         : INVALID_HANDLE_VALUE;
  }
  if (logh == INVALID_HANDLE_VALUE) return;
  char b[400]; int i = 0;
  const char *pre = "[antbridge] ";
  while (*pre) b[i++] = *pre++;
  for (const char *q = msg; *q && i < 370; q++) b[i++] = *q;
  if (v) {
    char num[16]; int k = 0; unsigned long u = v < 0 ? (unsigned long)-v : (unsigned long)v;
    b[i++] = ' '; if (v < 0) b[i++] = '-';
    do { num[k++] = (char)('0' + u % 10); u /= 10; } while (u);
    while (k) b[i++] = num[--k];
  }
  b[i++] = '\n';
  DWORD w; WriteFile(logh, b, (DWORD)i, &w, 0);
}

/* ------------------------------------------------------------- the device */
static int find_port(void) {
  if (GetEnvironmentVariableA("ANT_SERIAL_PORT", port, sizeof port)) return 1;
  WIN32_FIND_DATAA fd;
  HANDLE f = FindFirstFileA("Z:\\dev\\serial\\by-id\\*Dynastream*", &fd);
  if (f == INVALID_HANDLE_VALUE) return 0;
  FindClose(f);
  const char *dir = "Z:\\dev\\serial\\by-id\\";
  int i = 0;
  while (*dir) port[i++] = *dir++;
  for (const char *q = fd.cFileName; *q && i < (int)sizeof port - 1; q++) port[i++] = *q;
  port[i] = 0;
  return 1;
}

static HANDLE open_port(void) {
  /* GENERIC_READ|GENERIC_WRITE, OPEN_EXISTING, FILE_FLAG_OVERLAPPED */
  return CreateFileA(port, 0xC0000000, 0, 0, 3, 0x40000000, 0);
}

__declspec(dllexport) void usb_init(void) {
  memset(&dev, 0, sizeof dev);
  memset(&bus, 0, sizeof bus);
  dev.bus = &bus;
  /* ANTUSB-m.  ANT_DLL treats every Dynastream PID the same once it is open. */
  dev.descriptor = (struct usb_device_descriptor){ 18, 1, 0x0200, 0, 0, 0, 32, 0x0fcf, 0x1009, 0x0100, 1, 2, 3, 1 };
  dev.config = &cfg;
  dev.devnum = 1;
  memcpy(dev.filename, "ant-serial", 11);
  memcpy(bus.dirname, "antbridge", 10);
}
__declspec(dllexport) void usb_set_debug(int level) { (void)level; }
__declspec(dllexport) int usb_find_busses(void) { return 1; }
__declspec(dllexport) int usb_find_devices(void) {
  bus.devices = 0;
  if (!find_port()) return 0;  /* no stick plugged in: the game shows ANT+ as unavailable */
  bus.devices = &dev;
  return 1;
}
__declspec(dllexport) struct usb_bus *usb_get_busses(void) { return &bus; }

__declspec(dllexport) usb_dev_handle *usb_open(struct usb_device *d) {
  (void)d;
  HANDLE h = open_port();
  if (h == INVALID_HANDLE_VALUE) {
    /* 5 is ERROR_ACCESS_DENIED: the udev rule in TROUBLESHOOTING.md is missing */
    say("cannot open the ANT stick, Windows error", (long)GetLastError());
    return 0;
  }
  DCB dcb; memset(&dcb, 0, sizeof dcb); dcb.DCBlength = sizeof dcb;
  GetCommState(h, &dcb);
  dcb.BaudRate = 115200; dcb.ByteSize = 8; dcb.Parity = 0; dcb.StopBits = 0;
  dcb.flags = 1; /* fBinary only: no flow control */
  SetCommState(h, &dcb);
  /* Wait for the first byte, return after a 10 ms gap; reap() enforces the
   * caller's timeout.  Wine never completes an overlapped read under the
   * usual MAXDWORD/MAXDWORD/constant "return what is there" setting. */
  COMMTIMEOUTS t = { 10, 0, 0, 0, 3000 };
  SetCommTimeouts(h, &t);
  PurgeComm(h, 0x000F);
  usb_dev_handle *u = HeapAlloc(GetProcessHeap(), 8 /* HEAP_ZERO_MEMORY */, sizeof *u);
  if (!u) { CloseHandle(h); return 0; }
  u->h = h;
  u->rd_ev = CreateEventA(0, 1, 0, 0);
  u->wr_ev = CreateEventA(0, 1, 0, 0);
  say(port, 0);
  return u;
}
__declspec(dllexport) int usb_close(usb_dev_handle *u) {
  if (!u) return 0;
  CancelIoEx(u->h, 0);
  CloseHandle(u->h); CloseHandle(u->rd_ev); CloseHandle(u->wr_ev);
  HeapFree(GetProcessHeap(), 0, u);
  return 0;
}
__declspec(dllexport) struct usb_device *usb_device(usb_dev_handle *u) { (void)u; return &dev; }

__declspec(dllexport) int usb_get_string_simple(usb_dev_handle *u, int index, char *buf, size_t len) {
  (void)u;
  const char *s = index == 1 ? "Dynastream Innovations" : index == 2 ? "ANT USB-m Stick" : index == 3 ? "1" : 0;
  if (!s || !len) return -1;
  size_t n = 0;
  while (s[n] && n + 1 < len) { buf[n] = s[n]; n++; }
  buf[n] = 0;
  return (int)n;
}
__declspec(dllexport) int usb_get_string(usb_dev_handle *u, int index, int langid, char *buf, size_t len) {
  (void)langid; return usb_get_string_simple(u, index, buf, len);
}

/* USB housekeeping has no serial equivalent; ANT_DLL only checks for 0. */
__declspec(dllexport) int usb_set_configuration(usb_dev_handle *u, int c) { (void)u; (void)c; return 0; }
__declspec(dllexport) int usb_claim_interface(usb_dev_handle *u, int i) { (void)u; (void)i; return 0; }
__declspec(dllexport) int usb_release_interface(usb_dev_handle *u, int i) { (void)u; (void)i; return 0; }
__declspec(dllexport) int usb_set_altinterface(usb_dev_handle *u, int a) { (void)u; (void)a; return 0; }
__declspec(dllexport) int usb_clear_halt(usb_dev_handle *u, unsigned int ep) { (void)u; (void)ep; return 0; }
__declspec(dllexport) int usb_resetep(usb_dev_handle *u, unsigned int ep) { (void)u; (void)ep; return 0; }
__declspec(dllexport) int usb_reset(usb_dev_handle *u) { if (u) PurgeComm(u->h, 0x000F); return 0; }
__declspec(dllexport) int usb_control_msg(usb_dev_handle *u, int rt, int r, int v, int i, char *b, int s, int t) {
  (void)u; (void)rt; (void)r; (void)v; (void)i; (void)b; (void)t; return s;
}

/* ---------------------------------------------------------------- writes */
static int do_write(usb_dev_handle *u, const char *bytes, int size, int timeout) {
  OVERLAPPED ov; memset(&ov, 0, sizeof ov); ov.hEvent = u->wr_ev;
  DWORD n = 0;
  if (!WriteFile(u->h, bytes, (DWORD)size, &n, &ov)) {
    if (GetLastError() != ERROR_IO_PENDING) { say("write failed, Windows error", (long)GetLastError()); return EIO_; }
    if (WaitForSingleObject(ov.hEvent, timeout > 0 ? (DWORD)timeout : INFINITE) != WAIT_OBJECT_0) {
      CancelIoEx(u->h, &ov); GetOverlappedResult(u->h, &ov, &n, 1); return ETIMEDOUT_;
    }
    if (!GetOverlappedResult(u->h, &ov, &n, 0)) return EIO_;
  }
  return (int)n;
}
__declspec(dllexport) int usb_interrupt_write(usb_dev_handle *u, int ep, char *b, int s, int t) { (void)ep; return do_write(u, b, s, t); }
__declspec(dllexport) int usb_bulk_write(usb_dev_handle *u, int ep, char *b, int s, int t) { (void)ep; return do_write(u, b, s, t); }

/* ----------------------------------------------------------------- reads
 * ANT_DLL submits one read and reaps it with a 1 s timeout, again and again
 * without resubmitting until data came back -- so a read that timed out stays
 * pending ("nocancel"), exactly as libusb-win32 does it. */
static int start_read(async_ctx *c) {
  memset(&c->ov, 0, sizeof c->ov); c->ov.hEvent = c->d->rd_ev;
  DWORD n = 0;
  c->pending = 1;
  if (ReadFile(c->d->h, c->buf, (DWORD)c->size, &n, &c->ov)) return 0; /* done already; the event is set */
  if (GetLastError() != ERROR_IO_PENDING) { c->pending = 0; say("read failed, Windows error", (long)GetLastError()); return EIO_; }
  return 0;
}
static int reap(async_ctx *c, int timeout, int cancel) {
  DWORD n = 0;
  SetLastError(0); /* ANT_DLL reads GetLastError()==995 after -116 as "the OS slept, reset the stick" */
  if (!c->pending) return EIO_;
  if (WaitForSingleObject(c->ov.hEvent, timeout > 0 ? (DWORD)timeout : INFINITE) != WAIT_OBJECT_0) {
    if (cancel) { CancelIoEx(c->d->h, &c->ov); GetOverlappedResult(c->d->h, &c->ov, &n, 1); c->pending = 0; }
    SetLastError(0);
    return ETIMEDOUT_;
  }
  c->pending = 0;
  if (!GetOverlappedResult(c->d->h, &c->ov, &n, 0)) return EIO_;
  if (n == 0) {
    if (!cancel) start_read(c);
    SetLastError(0);
    return ETIMEDOUT_;
  }
  return (int)n;
}
__declspec(dllexport) int usb_bulk_setup_async(usb_dev_handle *u, void **context, unsigned char ep) {
  (void)ep;
  if (!u) return ENODEV_;
  async_ctx *c = HeapAlloc(GetProcessHeap(), 8, sizeof *c);
  if (!c) return EIO_;
  c->d = u;
  *context = c;
  return 0;
}
__declspec(dllexport) int usb_interrupt_setup_async(usb_dev_handle *u, void **context, unsigned char ep) { return usb_bulk_setup_async(u, context, ep); }
__declspec(dllexport) int usb_submit_async(void *context, char *bytes, int size) {
  async_ctx *c = context;
  c->buf = bytes; c->size = size;
  return start_read(c);
}
__declspec(dllexport) int usb_reap_async_nocancel(void *context, int timeout) { return reap(context, timeout, 0); }
__declspec(dllexport) int usb_reap_async(void *context, int timeout) { return reap(context, timeout, 1); }
__declspec(dllexport) int usb_cancel_async(void *context) {
  async_ctx *c = context; DWORD n;
  if (c && c->pending) { CancelIoEx(c->d->h, &c->ov); GetOverlappedResult(c->d->h, &c->ov, &n, 1); c->pending = 0; }
  return 0;
}
__declspec(dllexport) int usb_free_async(void **context) {
  if (context && *context) { usb_cancel_async(*context); HeapFree(GetProcessHeap(), 0, *context); *context = 0; }
  return 0;
}
__declspec(dllexport) int usb_bulk_read(usb_dev_handle *u, int ep, char *bytes, int size, int timeout) {
  (void)ep;
  async_ctx c; memset(&c, 0, sizeof c); c.d = u; c.buf = bytes; c.size = size;
  int r = start_read(&c);
  return r < 0 ? r : reap(&c, timeout, 1);
}
__declspec(dllexport) int usb_interrupt_read(usb_dev_handle *u, int ep, char *b, int s, int t) { return usb_bulk_read(u, ep, b, s, t); }

BOOL __stdcall DllMain(HANDLE inst, DWORD reason, void *res) { (void)inst; (void)reason; (void)res; return 1; }
