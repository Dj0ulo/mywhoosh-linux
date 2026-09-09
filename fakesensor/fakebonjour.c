/*
 * A stand-in for Apple's Bonjour COM server plus the sensors behind it.
 *
 * Registered under Bonjour's two CLSIDs, this DLL is loaded into the game's own
 * process instead of dnssdX.dll.  It answers Browse()/Resolve() out of its own
 * head -- from a small table of sensors, no mDNS on the wire at all -- and
 * serves each sensor's GATT services over a loopback TCP socket in Wahoo's
 * Direct Connect protocol.  The point is to prove the whole in-app path end to end without
 * Apple's mDNSResponder, which under Wine never joins the multicast group (see
 * ../dircon/README.md).
 *
 * Three things about this were measured rather than assumed:
 *
 *  * The sink is called EARLY-BOUND, through the _IDNSSDEvents vtable, not
 *    through IDispatch::Invoke.  Mono's CCW does answer QueryInterface for
 *    IID_IDispatch, but Invoke rejects both the type library's dispid
 *    (DISP_E_MEMBERNOTFOUND) and the id its own GetIDsOfNames hands out
 *    (E_INVALIDARG).  ../winemono/SinkInvokeProbe.cs is the measurement.
 *
 *  * Slot numbers: the emitted sink interface is declared IDispatch-derived, so
 *    its four IDispatch slots sit between IUnknown and the first event -- the
 *    events start at slot 7, in the order the shim emits them (ServiceFound,
 *    ServiceLost, ServiceResolved, OperationFailed).
 *
 *  * The events must be delivered AFTER Browse() returns, on the thread that
 *    called it.  MyWhoosh assigns `this.browser = mainService.Browse(...)` and
 *    its ServiceFound handler calls `browser.Resolve(...)`, so firing from
 *    inside Browse() would hit a null browser.  A message-only window on the
 *    calling (STA) thread carries the callbacks, which is also how Bonjour
 *    itself delivers them.
 *
 * Environment (this describes the first sensor; the handshake file below can
 * describe several):
 *   FAKESENSOR_NAME    service instance name         (default FakeTrainer)
 *   FAKESENSOR_SERIAL  serial-number TXT value, digits only, parsed as UInt64
 *   FAKESENSOR_MAC     mac-address TXT value
 *   FAKESENSOR_PORT    Dircon TCP port               (default 36866)
 *   FAKESENSOR_POWER   watts reported on 0x2a63      (default 150)
 *   FAKESENSOR_BPM     heart rate reported on 0x2a37 (default 75)
 *   FAKESENSOR_CAPS    slots it may fill: power,cadence,controllable,hr
 *   FAKESENSOR_HR      1 = advertise a second, heart-rate-only fake sensor on
 *                      FAKESENSOR_PORT+1, so the game's heart-rate slot can be
 *                      exercised without a strap
 *   FAKESENSOR_SINKBASE  first event vtable slot     (default 7)
 *   FAKESENSOR_EXTERNAL  1 = do not serve Dircon ourselves; something else
 *                        already listens on FAKESENSOR_PORT (blebridge.py)
 *   FAKESENSOR_LOG     log file; otherwise stderr
 *
 * C:\\fakesensor-device, if it exists, replaces the whole table and implies
 * FAKESENSOR_EXTERNAL -- blebridge.py writes it for the devices it has actually
 * connected to, one record per `name=' key.  See load_handshake.
 */

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <ocidl.h>
#include <olectl.h>
#include <oleauto.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ config */

/* One advertised sensor.  There used to be exactly one, hard-coded -- but the
 * game pairs each slot separately (power, cadence, controllable, heart rate),
 * so a heart-rate strap next to a trainer is a second service, with its own
 * name, its own hostname, its own serial and its own Dircon port. */

#define MAX_DEVICES 8

/* Which slots a sensor is willing to be paired as.  The game asks for one slot
 * at a time (EDeviceTypeEnum), and ../exportshim uses these to keep a sensor
 * out of a slot it cannot fill -- a heart-rate strap is not a trainer. */
#define CAP_POWER        1
#define CAP_CADENCE      2
#define CAP_CONTROLLABLE 4
#define CAP_HEART        8
#define CAP_ALL          (CAP_POWER | CAP_CADENCE | CAP_CONTROLLABLE | CAP_HEART)

typedef struct {
    char name[64];
    char serial[32];
    char mac[32];
    int  port;
    int  power;              /* what the built-in server reports on 0x2a63 ... */
    int  bpm;                /* ... and on 0x2a37, when it serves this port */
    unsigned caps;
    int  external;           /* something else already listens on `port' */
    char hostname[128];      /* Browse fills these in: the names we report */
    char fullname[192];
    volatile LONG serving;   /* our Dircon thread is up */
} device;

static device devs[MAX_DEVICES];
static int  ndevs;

static int  cfg_sink_base  = 7;
static int  cfg_external   = 0;
static int  cfg_hr         = 0;

static FILE *logfp;
static CRITICAL_SECTION loglock;

static void logmsg(const char *fmt, ...)
{
    va_list ap;
    SYSTEMTIME t;
    GetLocalTime(&t);
    EnterCriticalSection(&loglock);
    fprintf(logfp, "[%02d:%02d:%02d.%03d fakebonjour] ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_start(ap, fmt);
    vfprintf(logfp, fmt, ap);
    va_end(ap);
    fputc('\n', logfp);
    fflush(logfp);
    LeaveCriticalSection(&loglock);
}

static void env_str(const char *name, char *out, size_t n)
{
    const char *v = getenv(name);
    if (v && *v) { strncpy(out, v, n - 1); out[n - 1] = 0; }
}

static void env_int(const char *name, int *out)
{
    const char *v = getenv(name);
    if (v && *v) *out = atoi(v);
}

static unsigned parse_caps(const char *s)
{
    unsigned caps = 0;
    while (*s) {
        while (*s == ',' || *s == ' ') s++;
        if (!strncmp(s, "power", 5))             caps |= CAP_POWER;
        else if (!strncmp(s, "cadence", 7))      caps |= CAP_CADENCE;
        else if (!strncmp(s, "controllable", 12)) caps |= CAP_CONTROLLABLE;
        else if (!strncmp(s, "hr", 2) || !strncmp(s, "heart", 5)) caps |= CAP_HEART;
        else if (*s) logmsg("unknown capability in \"%s\", ignoring it", s);
        while (*s && *s != ',') s++;
    }
    return caps;
}

static const char *caps_str(unsigned caps, char *out, size_t n)
{
    size_t len;
    snprintf(out, n, "%s%s%s%s",
             caps & CAP_POWER        ? "power,"        : "",
             caps & CAP_CADENCE      ? "cadence,"      : "",
             caps & CAP_CONTROLLABLE ? "controllable," : "",
             caps & CAP_HEART        ? "hr,"           : "");
    len = strlen(out);
    if (len) out[len - 1] = 0;          /* the trailing comma */
    return out;
}

static void default_devices(void)
{
    device *d = &devs[0];
    memset(devs, 0, sizeof devs);
    ndevs = 1;
    strcpy(d->name, "FakeTrainer");
    strcpy(d->serial, "1234567890");
    strcpy(d->mac, "de:ad:be:ef:00:01");
    d->port = 36866;
    d->power = 150;
    d->bpm = 75;
    d->caps = CAP_ALL;
}

/* blebridge.py writes this when it has real hardware on the wire.  The DLL
 * runs inside the game and the bridge is a separate Linux process, so there is
 * no other channel between them in time: the name and serial have to be known
 * before the discovery answer goes out, which is long before anything connects
 * to the Dircon socket.
 *
 * It wins over the environment on purpose.  The env vars are static defaults --
 * the Lutris installer sets FakeTrainer/150W in them -- while this file is
 * written by a bridge that is running right now and knows what is actually
 * attached.  Its presence also implies FAKESENSOR_EXTERNAL: whoever wrote it is
 * serving the socket. */
#define HANDSHAKE_PATH "C:\\fakesensor-device"

/* What the export shim reads.  It has to filter the game's scan list by slot
   -- the game knows nothing about a sensor before it connects, so without this
   a strap sits in the trainer list and a power-only trainer in the heart-rate
   one -- and the handshake above is not enough on its own, because it only
   exists when blebridge.py is running.  So publish the effective table here,
   however it was configured, and let the shim read one file.  See
   ../exportshim/ExportShim.cs, DeviceCaps. */
#define TABLE_PATH "C:\\fakesensor-table"

static void load_handshake(void)
{
    char line[256], caps[64];
    device parsed[MAX_DEVICES];
    device *d = NULL;
    int n_parsed = 0, i;
    FILE *f = fopen(HANDSHAKE_PATH, "r");
    if (!f)
        return;

    while (fgets(line, sizeof line, f)) {
        char *eq = strchr(line, '=');
        char *key = line, *val;
        size_t n;
        if (!eq)
            continue;
        *eq = 0;
        val = eq + 1;
        n = strlen(val);
        while (n && (val[n - 1] == '\n' || val[n - 1] == '\r' || val[n - 1] == ' '))
            val[--n] = 0;
        if (!n)
            continue;
        /* `name' starts a record, which is what makes the one-device file an
           older bridge wrote still mean one device. */
        if (!strcmp(key, "name")) {
            if (n_parsed == MAX_DEVICES) {
                logmsg("handshake: more than %d devices, ignoring the rest", MAX_DEVICES);
                break;
            }
            d = &parsed[n_parsed++];
            memset(d, 0, sizeof *d);
            d->port = 36866 + n_parsed - 1;
            d->power = 150;
            d->bpm = 75;
            d->caps = CAP_ALL;      /* an older bridge says nothing about slots */
            strncpy(d->name, val, sizeof d->name - 1);
        }
        else if (!d)                     logmsg("handshake: \"%s\" before any name=, ignored", key);
        else if (!strcmp(key, "serial")) strncpy(d->serial, val, sizeof d->serial - 1);
        else if (!strcmp(key, "mac"))    strncpy(d->mac, val, sizeof d->mac - 1);
        else if (!strcmp(key, "port"))   d->port = atoi(val);
        else if (!strcmp(key, "caps"))   d->caps = parse_caps(val);
        else if (!strcmp(key, "power"))  d->power = atoi(val);
        else if (!strcmp(key, "bpm"))    d->bpm = atoi(val);
    }
    fclose(f);

    if (!n_parsed) {
        logmsg("handshake %s has no device records; keeping the configured one", HANDSHAKE_PATH);
        return;
    }
    memset(devs, 0, sizeof devs);
    for (i = 0; i < n_parsed; i++) {
        devs[i] = parsed[i];
        /* whoever wrote the file is serving the socket */
        devs[i].external = 1;
        logmsg("handshake %s: name=\"%s\" serial=%s mac=%s port=%d caps=%s",
               HANDSHAKE_PATH, devs[i].name, devs[i].serial, devs[i].mac, devs[i].port,
               caps_str(devs[i].caps, caps, sizeof caps));
    }
    ndevs = n_parsed;
}

/* The names we answer with.  MyWhoosh keys its service table on the instance
   name and its scan list on the host name, so two sensors need two of each --
   with the spaces dashed out, which is what it does to the name itself. */
static void build_names(void)
{
    int i, j;
    for (i = 0; i < ndevs; i++) {
        device *d = &devs[i];
        char *p;
        snprintf(d->hostname, sizeof d->hostname, "%s.local.", d->name);
        for (j = 0; j < i; j++) {
            if (strcmp(devs[j].name, d->name) != 0) continue;
            /* Two sensors under one name would share a scan-list entry and a
               service-table entry; the host name at least stays distinct. */
            logmsg("two sensors are both called \"%s\"; giving the second host name a suffix",
                   d->name);
            snprintf(d->hostname, sizeof d->hostname, "%s-%d.local.", d->name, i + 1);
            break;
        }
        for (p = d->hostname; *p; p++) if (*p == ' ') *p = '-';
        snprintf(d->fullname, sizeof d->fullname, "%s._wahoo-fitness-tnp._tcp.local.", d->name);
    }
}

/* Hand the export shim the table we ended up with. */
static void publish_table(void)
{
    char caps[64];
    int i;
    FILE *f = fopen(TABLE_PATH, "w");

    if (!f) { logmsg("cannot write %s; the shim cannot fill the slots we do", TABLE_PATH); return; }
    for (i = 0; i < ndevs; i++)
        fprintf(f, "name=%s\nserial=%s\nhost=%s\nport=%d\ncaps=%s\n",
                devs[i].name, devs[i].serial, devs[i].hostname, devs[i].port,
                caps_str(devs[i].caps, caps, sizeof caps));
    fclose(f);
}

static void load_config(void)
{
    const char *path = getenv("FAKESENSOR_LOG");
    const char *caps;
    int i;

    logfp = stderr;
    if (path && *path) {
        FILE *f = fopen(path, "a");
        if (f) logfp = f;
    }
    default_devices();
    env_str("FAKESENSOR_NAME", devs[0].name, sizeof devs[0].name);
    env_str("FAKESENSOR_SERIAL", devs[0].serial, sizeof devs[0].serial);
    env_str("FAKESENSOR_MAC", devs[0].mac, sizeof devs[0].mac);
    env_int("FAKESENSOR_PORT", &devs[0].port);
    env_int("FAKESENSOR_POWER", &devs[0].power);
    env_int("FAKESENSOR_BPM", &devs[0].bpm);
    caps = getenv("FAKESENSOR_CAPS");
    if (caps && *caps) devs[0].caps = parse_caps(caps);
    env_int("FAKESENSOR_SINKBASE", &cfg_sink_base);
    env_int("FAKESENSOR_EXTERNAL", &cfg_external);
    env_int("FAKESENSOR_HR", &cfg_hr);

    /* A second built-in sensor that only offers heart rate, so the in-game
       heart-rate slot can be exercised with no hardware at all. */
    if (cfg_hr) {
        device *d = &devs[1];
        strcpy(d->name, "FakeHRM");
        snprintf(d->serial, sizeof d->serial, "1234567891");
        strcpy(d->mac, "de:ad:be:ef:00:02");
        d->port = devs[0].port + 1;
        d->bpm = devs[0].bpm;
        d->caps = CAP_HEART;
        ndevs = 2;
    }
    for (i = 0; i < ndevs; i++)
        devs[i].external = cfg_external;
    load_handshake();
    build_names();
    publish_table();
}

/* -------------------------------------------------------------------- GUIDs */

/* The two coclasses MyWhoosh activates by CLSID (WFTNP_Init hard-codes them). */
static const GUID CLSID_DNSSDService =
    {0x24CD4DE9,0xFF84,0x4701,{0x9D,0xC1,0x9B,0x69,0xE0,0xD1,0x09,0x0A}};
static const GUID CLSID_DNSSDEventManager =
    {0xBEEB932A,0x8D4A,0x4619,{0xAE,0xFE,0xA8,0x36,0xF9,0x88,0xB2,0x21}};

/* Interface IIDs, from the interop types embedded in WindowsConnectivity.dll.
   Each coclass shares its GUID with its default interface there. */
static const GUID IID_IDNSSDService =
    {0x29DE265F,0x8402,0x474F,{0x83,0x3A,0xD4,0x65,0x3B,0x23,0x45,0x8F}};
static const GUID IID_IDNSSDEventManager =
    {0x7FD72324,0x63E1,0x45AD,{0xB3,0x37,0x4D,0x52,0x5B,0xD9,0x8D,0xAD}};
static const GUID IID__IDNSSDEvents =
    {0x21AE8D7F,0xD5FE,0x45CF,{0xB6,0x32,0xCF,0xA2,0xC2,0xC6,0xB4,0x98}};
static const GUID IID_ITXTRecord =
    {0x8FA0889C,0x5973,0x4FC9,{0x97,0x0B,0xEC,0x15,0xC9,0x25,0xD0,0xCE}};

/* Private: hands a caller back the C object behind an interface pointer, so
   Browse()/Resolve() can recognise the event manager they are given. */
static const GUID IID_FakeEventManager =
    {0x5f9a1c00,0x0001,0x4000,{0x9a,0x0f,0x00,0x00,0x00,0x00,0x00,0x01}};

static int guid_eq(const GUID *a, const GUID *b) { return memcmp(a, b, sizeof(GUID)) == 0; }

static BSTR bstr(const char *utf8)
{
    int n = MultiByteToWideChar(CP_UTF8, 0, utf8, -1, NULL, 0);
    WCHAR *w = (WCHAR *)malloc(n * sizeof(WCHAR));
    BSTR b;
    MultiByteToWideChar(CP_UTF8, 0, utf8, -1, w, n);
    b = SysAllocString(w);
    free(w);
    return b;
}

/* --------------------------------------------------------- IDNSSDService */
/*
 * Vtable layout follows the interop metadata exactly: IUnknown (3), IDispatch
 * (4, because the interface is dual), then one placeholder for the member with
 * dispid 1, Browse (dispid 2), Resolve (3), eight placeholders (4..11) and
 * Stop (12).  Only Browse, Resolve and Stop are ever called; the placeholders
 * log so a layout mistake names itself instead of crashing silently.
 */

typedef struct svc svc;
typedef struct evmgr evmgr;

typedef struct svcVtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(svc *, const IID *, void **);
    ULONG   (STDMETHODCALLTYPE *AddRef)(svc *);
    ULONG   (STDMETHODCALLTYPE *Release)(svc *);
    HRESULT (STDMETHODCALLTYPE *GetTypeInfoCount)(svc *, UINT *);
    HRESULT (STDMETHODCALLTYPE *GetTypeInfo)(svc *, UINT, LCID, void **);
    HRESULT (STDMETHODCALLTYPE *GetIDsOfNames)(svc *, const IID *, LPOLESTR *, UINT, LCID, DISPID *);
    HRESULT (STDMETHODCALLTYPE *Invoke)(svc *, DISPID, const IID *, LCID, WORD,
                                        DISPPARAMS *, VARIANT *, EXCEPINFO *, UINT *);
    HRESULT (STDMETHODCALLTYPE *Gap1)(svc *);
    HRESULT (STDMETHODCALLTYPE *Browse)(svc *, int, UINT, BSTR, BSTR, IUnknown *, svc **);
    HRESULT (STDMETHODCALLTYPE *Resolve)(svc *, int, UINT, BSTR, BSTR, BSTR, IUnknown *, svc **);
    HRESULT (STDMETHODCALLTYPE *Gap2[8])(svc *);
    HRESULT (STDMETHODCALLTYPE *Stop)(svc *);
} svcVtbl;

enum { SVC_MAIN, SVC_BROWSE, SVC_RESOLVE };
static const char *svc_kind[] = { "main", "browser", "resolver" };

struct svc {
    const svcVtbl *vtbl;
    LONG ref;
    int kind;
};

static const svcVtbl svc_vtbl;   /* defined after the methods */

static svc *svc_new(int kind)
{
    svc *s = (svc *)calloc(1, sizeof *s);
    s->vtbl = &svc_vtbl;
    s->ref = 1;
    s->kind = kind;
    return s;
}

static HRESULT STDMETHODCALLTYPE svc_QI(svc *s, const IID *iid, void **out)
{
    if (!out) return E_POINTER;
    if (guid_eq(iid, &IID_IUnknown) || guid_eq(iid, &IID_IDispatch)
        || guid_eq(iid, &IID_IDNSSDService)) {
        *out = s;
        s->vtbl->AddRef(s);
        return S_OK;
    }
    *out = NULL;
    return E_NOINTERFACE;
}

static ULONG STDMETHODCALLTYPE svc_AddRef(svc *s) { return InterlockedIncrement(&s->ref); }

static ULONG STDMETHODCALLTYPE svc_Release(svc *s)
{
    LONG n = InterlockedDecrement(&s->ref);
    if (n == 0) free(s);
    return n;
}

static HRESULT STDMETHODCALLTYPE svc_GetTypeInfoCount(svc *s, UINT *n) { if (n) *n = 0; return S_OK; }
static HRESULT STDMETHODCALLTYPE svc_GetTypeInfo(svc *s, UINT i, LCID l, void **o) { return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE svc_GetIDsOfNames(svc *s, const IID *r, LPOLESTR *n, UINT c,
                                                   LCID l, DISPID *d) { return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE svc_Invoke(svc *s, DISPID id, const IID *r, LCID l, WORD f,
                                            DISPPARAMS *p, VARIANT *v, EXCEPINFO *e, UINT *a)
{
    logmsg("IDNSSDService::Invoke(dispid=%ld) on the %s -- callers are supposed to be early-bound",
           (long)id, svc_kind[s->kind]);
    return DISP_E_MEMBERNOTFOUND;
}

static HRESULT STDMETHODCALLTYPE svc_gap(svc *s)
{
    logmsg("unexpected IDNSSDService vtable slot called on the %s: vtable layout is wrong",
           svc_kind[s->kind]);
    return E_NOTIMPL;
}

/* ----------------------------------------------------- callback delivery */

/* Slot indices within the sink interface, in the order ../winemono emits them. */
enum { EV_FOUND, EV_LOST, EV_RESOLVED, EV_FAILED };

typedef HRESULT (STDMETHODCALLTYPE *ServiceFoundFn)(void *self, void *browser, int flags,
                                                    UINT ifIndex, BSTR name, BSTR regtype, BSTR domain);
typedef HRESULT (STDMETHODCALLTYPE *ServiceResolvedFn)(void *self, void *service, int flags,
                                                       UINT ifIndex, BSTR fullname, BSTR hostname,
                                                       unsigned short port, void *txt);

static void *sink_slot(void *sink, int index)
{
    void **vtbl = *(void ***)sink;
    return vtbl[cfg_sink_base + index];
}

/* ------------------------------------------------------------- ITXTRecord */
/*
 * Two entries, "mac-address" and "serial-number": ServiceResolved bails out
 * unless it finds both, and parses the serial as a UInt64 device identifier.
 * GetValueAtIndex returns a VARIANT holding a SAFEARRAY of VT_UI1, which is
 * what the game's `(byte[])record.GetValueAtIndex(i)` dynamic cast expects.
 */

typedef struct txtrec txtrec;
typedef struct txtrecVtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(txtrec *, const IID *, void **);
    ULONG   (STDMETHODCALLTYPE *AddRef)(txtrec *);
    ULONG   (STDMETHODCALLTYPE *Release)(txtrec *);
    HRESULT (STDMETHODCALLTYPE *GetTypeInfoCount)(txtrec *, UINT *);
    HRESULT (STDMETHODCALLTYPE *GetTypeInfo)(txtrec *, UINT, LCID, void **);
    HRESULT (STDMETHODCALLTYPE *GetIDsOfNames)(txtrec *, const IID *, LPOLESTR *, UINT, LCID, DISPID *);
    HRESULT (STDMETHODCALLTYPE *Invoke)(txtrec *, DISPID, const IID *, LCID, WORD,
                                        DISPPARAMS *, VARIANT *, EXCEPINFO *, UINT *);
    HRESULT (STDMETHODCALLTYPE *Gap[4])(txtrec *);   /* dispids 1..4 */
    HRESULT (STDMETHODCALLTYPE *GetCount)(txtrec *, UINT *);
    HRESULT (STDMETHODCALLTYPE *GetKeyAtIndex)(txtrec *, UINT, BSTR *);
    HRESULT (STDMETHODCALLTYPE *GetValueAtIndex)(txtrec *, UINT, VARIANT *);
} txtrecVtbl;

struct txtrec { const txtrecVtbl *vtbl; LONG ref; const device *dev; };

static const txtrecVtbl txt_vtbl;

static HRESULT STDMETHODCALLTYPE txt_QI(txtrec *t, const IID *iid, void **out)
{
    if (!out) return E_POINTER;
    if (guid_eq(iid, &IID_IUnknown) || guid_eq(iid, &IID_IDispatch)
        || guid_eq(iid, &IID_ITXTRecord)) {
        *out = t;
        InterlockedIncrement(&t->ref);
        return S_OK;
    }
    *out = NULL;
    return E_NOINTERFACE;
}

static ULONG STDMETHODCALLTYPE txt_AddRef(txtrec *t) { return InterlockedIncrement(&t->ref); }
static ULONG STDMETHODCALLTYPE txt_Release(txtrec *t)
{
    LONG n = InterlockedDecrement(&t->ref);
    if (n == 0) free(t);
    return n;
}
static HRESULT STDMETHODCALLTYPE txt_GetTypeInfoCount(txtrec *t, UINT *n) { if (n) *n = 0; return S_OK; }
static HRESULT STDMETHODCALLTYPE txt_GetTypeInfo(txtrec *t, UINT i, LCID l, void **o) { return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE txt_GetIDsOfNames(txtrec *t, const IID *r, LPOLESTR *n, UINT c,
                                                   LCID l, DISPID *d) { return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE txt_Invoke(txtrec *t, DISPID id, const IID *r, LCID l, WORD f,
                                            DISPPARAMS *p, VARIANT *v, EXCEPINFO *e, UINT *a)
{ return DISP_E_MEMBERNOTFOUND; }
static HRESULT STDMETHODCALLTYPE txt_gap(txtrec *t)
{
    logmsg("unexpected ITXTRecord vtable slot called: vtable layout is wrong");
    return E_NOTIMPL;
}

static HRESULT STDMETHODCALLTYPE txt_GetCount(txtrec *t, UINT *n)
{
    if (!n) return E_POINTER;
    *n = 2;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE txt_GetKeyAtIndex(txtrec *t, UINT i, BSTR *out)
{
    if (!out) return E_POINTER;
    *out = bstr(i == 0 ? "mac-address" : "serial-number");
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE txt_GetValueAtIndex(txtrec *t, UINT i, VARIANT *out)
{
    const char *v = (i == 0) ? t->dev->mac : t->dev->serial;
    ULONG n = (ULONG)strlen(v);
    SAFEARRAY *sa;
    void *data;

    if (!out) return E_POINTER;
    VariantInit(out);
    sa = SafeArrayCreateVector(VT_UI1, 0, n);
    if (!sa) return E_OUTOFMEMORY;
    if (SafeArrayAccessData(sa, &data) == S_OK) {
        memcpy(data, v, n);
        SafeArrayUnaccessData(sa);
    }
    V_VT(out) = VT_ARRAY | VT_UI1;
    V_ARRAY(out) = sa;
    return S_OK;
}

static const txtrecVtbl txt_vtbl = {
    txt_QI, txt_AddRef, txt_Release,
    txt_GetTypeInfoCount, txt_GetTypeInfo, txt_GetIDsOfNames, txt_Invoke,
    { txt_gap, txt_gap, txt_gap, txt_gap },
    txt_GetCount, txt_GetKeyAtIndex, txt_GetValueAtIndex,
};

static txtrec *txt_new(const device *d)
{
    txtrec *t = (txtrec *)calloc(1, sizeof *t);
    t->vtbl = &txt_vtbl;
    t->ref = 1;
    t->dev = d;
    return t;
}

/* --------------------------------------- DNSSDEventManager + connection point */

typedef struct {
    const IConnectionPointContainerVtbl *vtbl;
    evmgr *self;
} cpc_part;

typedef struct {
    const IConnectionPointVtbl *vtbl;
    evmgr *self;
} cp_part;

typedef struct evmgrVtbl {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(evmgr *, const IID *, void **);
    ULONG   (STDMETHODCALLTYPE *AddRef)(evmgr *);
    ULONG   (STDMETHODCALLTYPE *Release)(evmgr *);
    HRESULT (STDMETHODCALLTYPE *GetTypeInfoCount)(evmgr *, UINT *);
    HRESULT (STDMETHODCALLTYPE *GetTypeInfo)(evmgr *, UINT, LCID, void **);
    HRESULT (STDMETHODCALLTYPE *GetIDsOfNames)(evmgr *, const IID *, LPOLESTR *, UINT, LCID, DISPID *);
    HRESULT (STDMETHODCALLTYPE *Invoke)(evmgr *, DISPID, const IID *, LCID, WORD,
                                        DISPPARAMS *, VARIANT *, EXCEPINFO *, UINT *);
} evmgrVtbl;

struct evmgr {
    const evmgrVtbl *vtbl;
    LONG ref;
    cpc_part cpc;
    cp_part cp;
    IUnknown *sink_unk;      /* what Advise was given */
    void *sink_ev;           /* the same sink, as _IDNSSDEvents */
    DWORD cookie;
};

static const evmgrVtbl evmgr_vtbl;
static const IConnectionPointContainerVtbl cpc_vtbl;
static const IConnectionPointVtbl cp_vtbl;

static HRESULT STDMETHODCALLTYPE evmgr_QI(evmgr *m, const IID *iid, void **out)
{
    if (!out) return E_POINTER;
    if (guid_eq(iid, &IID_IUnknown) || guid_eq(iid, &IID_IDispatch)
        || guid_eq(iid, &IID_IDNSSDEventManager)) {
        *out = m;
        InterlockedIncrement(&m->ref);
        return S_OK;
    }
    if (guid_eq(iid, &IID_IConnectionPointContainer)) {
        *out = &m->cpc;
        InterlockedIncrement(&m->ref);
        return S_OK;
    }
    if (guid_eq(iid, &IID_FakeEventManager)) {
        *out = m;                 /* private: no AddRef, no marshalling */
        return S_OK;
    }
    *out = NULL;
    return E_NOINTERFACE;
}

static ULONG STDMETHODCALLTYPE evmgr_AddRef(evmgr *m) { return InterlockedIncrement(&m->ref); }

static ULONG STDMETHODCALLTYPE evmgr_Release(evmgr *m)
{
    LONG n = InterlockedDecrement(&m->ref);
    if (n == 0) {
        if (m->sink_ev) ((IUnknown *)m->sink_ev)->lpVtbl->Release((IUnknown *)m->sink_ev);
        if (m->sink_unk) m->sink_unk->lpVtbl->Release(m->sink_unk);
        free(m);
    }
    return n;
}

static HRESULT STDMETHODCALLTYPE evmgr_GetTypeInfoCount(evmgr *m, UINT *n) { if (n) *n = 0; return S_OK; }
static HRESULT STDMETHODCALLTYPE evmgr_GetTypeInfo(evmgr *m, UINT i, LCID l, void **o) { return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE evmgr_GetIDsOfNames(evmgr *m, const IID *r, LPOLESTR *n, UINT c,
                                                     LCID l, DISPID *d) { return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE evmgr_Invoke(evmgr *m, DISPID id, const IID *r, LCID l, WORD f,
                                              DISPPARAMS *p, VARIANT *v, EXCEPINFO *e, UINT *a)
{ return DISP_E_MEMBERNOTFOUND; }

static const evmgrVtbl evmgr_vtbl = {
    evmgr_QI, evmgr_AddRef, evmgr_Release,
    evmgr_GetTypeInfoCount, evmgr_GetTypeInfo, evmgr_GetIDsOfNames, evmgr_Invoke,
};

/* IConnectionPointContainer */

static HRESULT STDMETHODCALLTYPE cpc_QI(IConnectionPointContainer *this_, const IID *iid, void **out)
{ return evmgr_QI(((cpc_part *)this_)->self, iid, out); }
static ULONG STDMETHODCALLTYPE cpc_AddRef(IConnectionPointContainer *this_)
{ return evmgr_AddRef(((cpc_part *)this_)->self); }
static ULONG STDMETHODCALLTYPE cpc_Release(IConnectionPointContainer *this_)
{ return evmgr_Release(((cpc_part *)this_)->self); }

static HRESULT STDMETHODCALLTYPE cpc_EnumConnectionPoints(IConnectionPointContainer *this_,
                                                          IEnumConnectionPoints **out)
{ return E_NOTIMPL; }

static HRESULT STDMETHODCALLTYPE cpc_FindConnectionPoint(IConnectionPointContainer *this_,
                                                         const IID *iid, IConnectionPoint **out)
{
    evmgr *m = ((cpc_part *)this_)->self;
    if (!out) return E_POINTER;
    if (!guid_eq(iid, &IID__IDNSSDEvents)) {
        logmsg("FindConnectionPoint for an interface we do not source");
        *out = NULL;
        return CONNECT_E_NOCONNECTION;
    }
    *out = (IConnectionPoint *)&m->cp;
    evmgr_AddRef(m);
    return S_OK;
}

static const IConnectionPointContainerVtbl cpc_vtbl = {
    cpc_QI, cpc_AddRef, cpc_Release, cpc_EnumConnectionPoints, cpc_FindConnectionPoint,
};

/* IConnectionPoint */

static HRESULT STDMETHODCALLTYPE cp_QI(IConnectionPoint *this_, const IID *iid, void **out)
{
    cp_part *p = (cp_part *)this_;
    if (!out) return E_POINTER;
    if (guid_eq(iid, &IID_IUnknown) || guid_eq(iid, &IID_IConnectionPoint)) {
        *out = this_;
        evmgr_AddRef(p->self);
        return S_OK;
    }
    return evmgr_QI(p->self, iid, out);
}
static ULONG STDMETHODCALLTYPE cp_AddRef(IConnectionPoint *this_)
{ return evmgr_AddRef(((cp_part *)this_)->self); }
static ULONG STDMETHODCALLTYPE cp_Release(IConnectionPoint *this_)
{ return evmgr_Release(((cp_part *)this_)->self); }

static HRESULT STDMETHODCALLTYPE cp_GetConnectionInterface(IConnectionPoint *this_, IID *out)
{
    if (!out) return E_POINTER;
    *out = IID__IDNSSDEvents;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE cp_GetConnectionPointContainer(IConnectionPoint *this_,
                                                                IConnectionPointContainer **out)
{
    evmgr *m = ((cp_part *)this_)->self;
    if (!out) return E_POINTER;
    *out = (IConnectionPointContainer *)&m->cpc;
    evmgr_AddRef(m);
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE cp_Advise(IConnectionPoint *this_, IUnknown *sink, DWORD *cookie)
{
    evmgr *m = ((cp_part *)this_)->self;
    void *ev = NULL;
    HRESULT hr;

    if (!sink || !cookie) return E_POINTER;
    hr = sink->lpVtbl->QueryInterface(sink, &IID__IDNSSDEvents, &ev);
    if (hr != S_OK) {
        logmsg("Advise: the sink does not implement _IDNSSDEvents (0x%08lx)", (unsigned long)hr);
        return CONNECT_E_CANNOTCONNECT;
    }

    if (m->sink_ev) ((IUnknown *)m->sink_ev)->lpVtbl->Release((IUnknown *)m->sink_ev);
    if (m->sink_unk) m->sink_unk->lpVtbl->Release(m->sink_unk);

    sink->lpVtbl->AddRef(sink);
    m->sink_unk = sink;
    m->sink_ev = ev;
    m->cookie = 1;
    *cookie = m->cookie;
    logmsg("Advise: sink %p (_IDNSSDEvents %p), events start at vtable slot %d",
           (void *)sink, ev, cfg_sink_base);
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE cp_Unadvise(IConnectionPoint *this_, DWORD cookie)
{
    evmgr *m = ((cp_part *)this_)->self;
    if (cookie != m->cookie) return CONNECT_E_NOCONNECTION;
    if (m->sink_ev) { ((IUnknown *)m->sink_ev)->lpVtbl->Release((IUnknown *)m->sink_ev); m->sink_ev = NULL; }
    if (m->sink_unk) { m->sink_unk->lpVtbl->Release(m->sink_unk); m->sink_unk = NULL; }
    m->cookie = 0;
    logmsg("Unadvise");
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE cp_EnumConnections(IConnectionPoint *this_, IEnumConnections **out)
{ return E_NOTIMPL; }

static const IConnectionPointVtbl cp_vtbl = {
    cp_QI, cp_AddRef, cp_Release,
    cp_GetConnectionInterface, cp_GetConnectionPointContainer,
    cp_Advise, cp_Unadvise, cp_EnumConnections,
};

static evmgr *evmgr_new(void)
{
    evmgr *m = (evmgr *)calloc(1, sizeof *m);
    m->vtbl = &evmgr_vtbl;
    m->ref = 1;
    m->cpc.vtbl = &cpc_vtbl;
    m->cpc.self = m;
    m->cp.vtbl = &cp_vtbl;
    m->cp.self = m;
    return m;
}

/* ------------------------------------------------- the discovery it reports */

static evmgr *g_mgr;             /* whoever Browse() was handed */
static svc *g_browser;
static svc *g_resolver;
static UINT g_ifindex = 1;

/* Which device the callback is about travels in the message's WPARAM: the
   events are delivered on the browsing thread, after the call that asked for
   them returned, so there is nowhere else to put it. */
#define WM_FAKE_FOUND    (WM_APP + 1)
#define WM_FAKE_RESOLVED (WM_APP + 2)

static void fire_found(int i)
{
    ServiceFoundFn fn;
    BSTR name, regtype, domain;
    HRESULT hr;

    if (i < 0 || i >= ndevs) return;
    if (!g_mgr || !g_mgr->sink_ev) { logmsg("ServiceFound: nothing Advised, dropping"); return; }
    fn = (ServiceFoundFn)sink_slot(g_mgr->sink_ev, EV_FOUND);
    name = bstr(devs[i].name);
    regtype = bstr("_wahoo-fitness-tnp._tcp.");
    domain = bstr("local.");
    /* flags 2 is Bonjour's kDNSServiceFlagsAdd; MyWhoosh only logs it. */
    hr = fn(g_mgr->sink_ev, g_browser, 2, g_ifindex, name, regtype, domain);
    logmsg("ServiceFound(\"%s\") -> 0x%08lx", devs[i].name, (unsigned long)hr);
    SysFreeString(name); SysFreeString(regtype); SysFreeString(domain);
}

static void fire_resolved(int i)
{
    ServiceResolvedFn fn;
    BSTR full, host;
    txtrec *txt;
    HRESULT hr;

    if (i < 0 || i >= ndevs) return;
    if (!g_mgr || !g_mgr->sink_ev) { logmsg("ServiceResolved: nothing Advised, dropping"); return; }
    fn = (ServiceResolvedFn)sink_slot(g_mgr->sink_ev, EV_RESOLVED);
    full = bstr(devs[i].fullname);
    host = bstr(devs[i].hostname);
    txt = txt_new(&devs[i]);
    hr = fn(g_mgr->sink_ev, g_resolver, 0, g_ifindex, full, host,
            (unsigned short)devs[i].port, txt);
    /* MyWhoosh's ServiceResolved ends with `resolver.Stop()` on a field that is
       never assigned, so it always throws NullReferenceException back at us --
       after doing everything that matters.  0x80004003 here is expected. */
    logmsg("ServiceResolved(\"%s\" at %s:%d) -> 0x%08lx%s",
           devs[i].fullname, devs[i].hostname, devs[i].port, (unsigned long)hr,
           hr == 0x80004003L ? "  (the game's own NullReferenceException, harmless)" : "");
    txt->vtbl->Release(txt);
    SysFreeString(full); SysFreeString(host);
}

static LRESULT CALLBACK fake_wndproc(HWND h, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_FAKE_FOUND:    fire_found((int)wp);    return 0;
    case WM_FAKE_RESOLVED: fire_resolved((int)wp); return 0;
    }
    return DefWindowProcW(h, msg, wp, lp);
}

/* One message-only window per thread that browses: the callbacks have to run on
   the apartment that asked for them, after the call that asked returns. */
static DWORD tls_hwnd = TLS_OUT_OF_INDEXES;

static HWND callback_window(void)
{
    HWND h;
    static const WCHAR cls[] = L"FakeBonjourCallbacks";
    WNDCLASSEXW wc;

    if (tls_hwnd == TLS_OUT_OF_INDEXES) return NULL;
    h = (HWND)TlsGetValue(tls_hwnd);
    if (h) return h;

    memset(&wc, 0, sizeof wc);
    wc.cbSize = sizeof wc;
    wc.lpfnWndProc = fake_wndproc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.lpszClassName = cls;
    RegisterClassExW(&wc);          /* harmless if already registered */

    h = CreateWindowExW(0, cls, cls, 0, 0, 0, 0, 0, HWND_MESSAGE, NULL, wc.hInstance, NULL);
    if (!h) { logmsg("CreateWindowEx failed: %lu", GetLastError()); return NULL; }
    TlsSetValue(tls_hwnd, h);
    logmsg("callback window %p on thread %lu", (void *)h, GetCurrentThreadId());
    return h;
}

/* ------------------------------------------------------ Dircon TCP server */

/* Wahoo Direct Connect, as decoded from DirconSensor/WahooUtility:
 *   header  01 <msgId> <seq> <responseCode> <len hi> <len lo>
 *   body    16-byte big-endian UUID, then the payload
 * Message ids: 1 discover services, 2 discover characteristics,
 * 3 read, 4 write, 5 enable notifications, 6 notification (server-initiated).
 * Characteristic property bits are Dircon's own: 1 read, 2 write, 4 notify.
 */

#define UUID16(x) { 0,0,(unsigned char)((x)>>8),(unsigned char)((x)&0xff), \
                    0,0,0x10,0x00,0x80,0x00,0x00,0x80,0x5f,0x9b,0x34,0xfb }

static const unsigned char UUID_CYCLING_POWER[16] = UUID16(0x1818);
static const unsigned char UUID_HEART_RATE[16]    = UUID16(0x180d);
static const unsigned char UUID_POWER_MEAS[16]    = UUID16(0x2a63);
static const unsigned char UUID_HR_MEAS[16]       = UUID16(0x2a37);

static int send_all(SOCKET s, const unsigned char *p, int n)
{
    while (n > 0) {
        int k = send(s, (const char *)p, n, 0);
        if (k <= 0) return 0;
        p += k; n -= k;
    }
    return 1;
}

static int recv_all(SOCKET s, unsigned char *p, int n)
{
    while (n > 0) {
        int k = recv(s, (char *)p, n, 0);
        if (k <= 0) return 0;
        p += k; n -= k;
    }
    return 1;
}

static int send_msg(SOCKET s, int msgid, int seq, const unsigned char *body, int len)
{
    unsigned char hdr[6];
    hdr[0] = 1;
    hdr[1] = (unsigned char)msgid;
    hdr[2] = (unsigned char)seq;
    hdr[3] = 0;
    hdr[4] = (unsigned char)((len >> 8) & 0xff);
    hdr[5] = (unsigned char)(len & 0xff);
    if (!send_all(s, hdr, 6)) return 0;
    return len ? send_all(s, body, len) : 1;
}

static int is_uuid(const unsigned char *b, const unsigned char *uuid) { return memcmp(b, uuid, 16) == 0; }

static void serve_client(device *d, SOCKET c)
{
    unsigned char hdr[6], body[1024], out[1024];
    int notify_power = 0, notify_hr = 0, tick = 0;
    DWORD last = GetTickCount();

    logmsg("dircon: client connected on port %d (%s)", d->port, d->name);
    for (;;) {
        fd_set rd;
        struct timeval tv;
        int n;

        FD_ZERO(&rd);
        FD_SET(c, &rd);
        tv.tv_sec = 0;
        tv.tv_usec = 200 * 1000;
        n = select(0, &rd, NULL, NULL, &tv);
        if (n == SOCKET_ERROR) break;

        if (n > 0) {
            int len, seq, id;
            if (!recv_all(c, hdr, 6)) break;
            if (hdr[0] != 1) { logmsg("dircon: protocol version %u, giving up", hdr[0]); break; }
            id = hdr[1];
            seq = hdr[2];
            len = (hdr[4] << 8) | hdr[5];
            if (len < 0 || len > (int)sizeof body) { logmsg("dircon: body of %d bytes, giving up", len); break; }
            if (len && !recv_all(c, body, len)) break;
            logmsg("dircon: request id=%d seq=%d len=%d", id, seq, len);

            switch (id) {
            case 1: { /* discover services -- only what this sensor claims */
                int n = 0;
                if (d->caps & (CAP_POWER | CAP_CADENCE | CAP_CONTROLLABLE)) {
                    memcpy(out + n, UUID_CYCLING_POWER, 16);
                    n += 16;
                }
                if (d->caps & CAP_HEART) {
                    memcpy(out + n, UUID_HEART_RATE, 16);
                    n += 16;
                }
                if (!send_msg(c, 1, seq, out, n)) goto done;
                break;
            }

            case 2: { /* discover characteristics of one service */
                const unsigned char *ch;
                if (len < 16) goto bad;
                memcpy(out, body, 16);
                ch = is_uuid(body, UUID_CYCLING_POWER) && (d->caps & (CAP_POWER | CAP_CADENCE | CAP_CONTROLLABLE))
                        ? UUID_POWER_MEAS
                   : is_uuid(body, UUID_HEART_RATE) && (d->caps & CAP_HEART)
                        ? UUID_HR_MEAS : NULL;
                if (!ch) {
                    logmsg("dircon: characteristics asked for an unknown service");
                    if (!send_msg(c, 2, seq, out, 16)) goto done;
                    break;
                }
                memcpy(out + 16, ch, 16);
                out[32] = 0x04;         /* notify only: nothing here is readable */
                if (!send_msg(c, 2, seq, out, 33)) goto done;
                break;
            }

            case 3:   /* read -- nothing is advertised readable, answer empty */
                if (len < 16) goto bad;
                memcpy(out, body, 16);
                out[16] = 0;
                if (!send_msg(c, 3, seq, out, 17)) goto done;
                break;

            case 5: { /* enable/disable notifications */
                int enable;
                if (len < 17) goto bad;
                enable = body[16] ? 1 : 0;
                if (is_uuid(body, UUID_POWER_MEAS)) notify_power = enable;
                else if (is_uuid(body, UUID_HR_MEAS)) notify_hr = enable;
                else logmsg("dircon: notifications asked for an unknown characteristic");
                memcpy(out, body, 17);
                if (!send_msg(c, 5, seq, out, 17)) goto done;
                logmsg("dircon: notifications power=%d hr=%d", notify_power, notify_hr);
                break;
            }

            case 4:   /* write: acknowledge, the trainer has nothing to obey */
                if (!send_msg(c, 4, seq, body, len ? len : 1)) goto done;
                break;

            default:
            bad:
                logmsg("dircon: unhandled request id=%d", id);
                out[0] = 0;
                if (!send_msg(c, id, seq, out, 1)) goto done;
                break;
            }
        }

        if (GetTickCount() - last >= 1000) {
            last += 1000;
            tick++;
            if (notify_power) {
                int watts = d->power + (tick % 5) * 2;
                memcpy(out, UUID_POWER_MEAS, 16);
                out[16] = 0; out[17] = 0;                       /* flags: nothing optional */
                out[18] = (unsigned char)(watts & 0xff);        /* sint16, little endian */
                out[19] = (unsigned char)((watts >> 8) & 0xff);
                if (!send_msg(c, 6, 0, out, 20)) goto done;
            }
            if (notify_hr) {
                int bpm = d->bpm + (tick % 7);
                memcpy(out, UUID_HR_MEAS, 16);
                out[16] = 0;                                    /* flags: 8-bit value */
                out[17] = (unsigned char)bpm;
                if (!send_msg(c, 6, 0, out, 18)) goto done;
            }
        }
    }
done:
    logmsg("dircon: client gone (port %d)", d->port);
    closesocket(c);
}

static DWORD WINAPI dircon_thread(void *arg)
{
    device *d = (device *)arg;
    WSADATA wsa;
    SOCKET l;
    struct sockaddr_in a;
    int on = 1;

    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) { logmsg("WSAStartup failed"); return 1; }
    l = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (l == INVALID_SOCKET) { logmsg("socket failed: %d", WSAGetLastError()); return 1; }
    setsockopt(l, SOL_SOCKET, SO_REUSEADDR, (const char *)&on, sizeof on);

    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    a.sin_port = htons((unsigned short)d->port);
    if (bind(l, (struct sockaddr *)&a, sizeof a) != 0) {
        logmsg("bind 127.0.0.1:%d failed: %d", d->port, WSAGetLastError());
        closesocket(l);
        return 1;
    }
    if (listen(l, 4) != 0) { logmsg("listen failed: %d", WSAGetLastError()); closesocket(l); return 1; }
    logmsg("dircon: listening on 127.0.0.1:%d for \"%s\"", d->port, d->name);

    for (;;) {
        SOCKET c = accept(l, NULL, NULL);
        if (c == INVALID_SOCKET) break;
        serve_client(d, c);
    }
    closesocket(l);
    return 0;
}

/* One listener per sensor we serve ourselves.  Each is its own thread because
   the game opens one Dircon connection per sensor and keeps it open. */
static void dircon_start_all(void)
{
    int i;
    for (i = 0; i < ndevs; i++) {
        device *d = &devs[i];
        if (d->external) {
            logmsg("dircon: \"%s\" is served elsewhere, expecting a server on port %d already",
                   d->name, d->port);
            continue;
        }
        if (InterlockedCompareExchange(&d->serving, 1, 0) != 0) continue;
        CloseHandle(CreateThread(NULL, 0, dircon_thread, d, 0, NULL));
    }
}

/* ------------------------------------------------- Browse / Resolve / Stop */

static evmgr *manager_of(IUnknown *unk)
{
    evmgr *m = NULL;
    if (unk) unk->lpVtbl->QueryInterface(unk, &IID_FakeEventManager, (void **)&m);
    return m;
}

static HRESULT STDMETHODCALLTYPE svc_Browse(svc *s, int flags, UINT ifIndex, BSTR regtype,
                                            BSTR domain, IUnknown *mgr, svc **out)
{
    char type[128] = "";
    evmgr *m = manager_of(mgr);

    if (!out) return E_POINTER;
    if (regtype) WideCharToMultiByte(CP_UTF8, 0, regtype, -1, type, sizeof type, NULL, NULL);
    logmsg("Browse(flags=%d, ifIndex=%u, \"%s\") on the %s", flags, ifIndex, type, svc_kind[s->kind]);

    if (!m) { logmsg("Browse: the event manager is not one of ours"); return E_INVALIDARG; }
    if (strcmp(type, "_wahoo-fitness-tnp._tcp.") != 0) {
        logmsg("Browse: not the Dircon service type, reporting nothing");
        *out = svc_new(SVC_BROWSE);
        return S_OK;
    }

    g_mgr = m;
    if (g_browser) svc_Release(g_browser);
    g_browser = svc_new(SVC_BROWSE);
    svc_AddRef(g_browser);          /* one reference for us, one for the caller */
    *out = g_browser;

    dircon_start_all();
    {   /* One ServiceFound per sensor.  The game resolves each in turn, and
               ends up with one scan-list entry per sensor. */
        HWND h = callback_window();
        int i;
        for (i = 0; i < ndevs; i++)
            PostMessageW(h, WM_FAKE_FOUND, (WPARAM)i, 0);
    }
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE svc_Resolve(svc *s, int flags, UINT ifIndex, BSTR name,
                                             BSTR regtype, BSTR domain, IUnknown *mgr, svc **out)
{
    char n[128] = "";
    evmgr *m = manager_of(mgr);
    int i, idx = -1;

    if (!out) return E_POINTER;
    if (name) WideCharToMultiByte(CP_UTF8, 0, name, -1, n, sizeof n, NULL, NULL);
    logmsg("Resolve(ifIndex=%u, \"%s\") on the %s", ifIndex, n, svc_kind[s->kind]);

    if (m) g_mgr = m;
    g_ifindex = ifIndex;
    if (g_resolver) svc_Release(g_resolver);
    g_resolver = svc_new(SVC_RESOLVE);
    svc_AddRef(g_resolver);
    *out = g_resolver;

    /* Answer for the sensor that was asked about: with more than one
       advertised, resolving them all to the first would give the game one
       device wearing several names. */
    for (i = 0; i < ndevs; i++)
        if (!strcmp(n, devs[i].name)) { idx = i; break; }
    if (idx < 0) {
        logmsg("Resolve: \"%s\" is not one of ours, reporting nothing", n);
        return S_OK;
    }

    PostMessageW(callback_window(), WM_FAKE_RESOLVED, (WPARAM)idx, 0);
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE svc_Stop(svc *s)
{
    logmsg("Stop() on the %s", svc_kind[s->kind]);
    return S_OK;
}

static const svcVtbl svc_vtbl = {
    svc_QI, svc_AddRef, svc_Release,
    svc_GetTypeInfoCount, svc_GetTypeInfo, svc_GetIDsOfNames, svc_Invoke,
    svc_gap,                    /* dispid 1 */
    svc_Browse,                 /* dispid 2 */
    svc_Resolve,                /* dispid 3 */
    { svc_gap, svc_gap, svc_gap, svc_gap, svc_gap, svc_gap, svc_gap, svc_gap },  /* 4..11 */
    svc_Stop,                   /* dispid 12 */
};

/* ---------------------------------------------- managed export shim (kick) */
/*
 * Blocker 4: four of WindowsConnectivity.dll's exports take the device list as
 * `out DeviceInformationStruct[]`, and wine-mono's native marshaller refuses
 * that shape -- "Byref array marshalling to managed code is not implemented."
 * The refusal is compiled into the native-to-managed wrapper as a throw, so the
 * game's first device-list poll is a fatal unhandled exception.
 *
 * ../exportshim serves those four itself, in managed code, by rewriting the
 * vtable-fixup slots the export stubs jump through -- in memory, so no game
 * file is touched.  It needs someone inside the game process to load and start
 * it, and that is this DLL: it is already here, mono is already up, and the
 * game's managed connectivity init is what put us here, so the poll it dies on
 * has not happened yet (measured: fakebonjour.dll loads well before the crash).
 *
 * The invoke target is a static void method with no arguments, which is the one
 * shape mono's embedding API can call without marshalling anything -- the whole
 * problem being marshalled shapes.
 */

typedef void *(*fn_get_root_domain)(void);
typedef void *(*fn_domain_get)(void);
typedef void *(*fn_thread_attach)(void *domain);
typedef void *(*fn_assembly_open)(void *domain, const char *name);
typedef void *(*fn_assembly_get_image)(void *assembly);
typedef void *(*fn_class_from_name)(void *image, const char *ns, const char *name);
typedef void *(*fn_method_from_name)(void *klass, const char *name, int argc);
typedef void *(*fn_runtime_invoke)(void *method, void *obj, void **params, void **exc);

/* c:\windows\mono\mono-2.0\bin\libmono-...dll -> ...\mono-2.0\lib\<dll> */
static int shim_path(HMODULE mono, const char *dll, char *out, size_t n)
{
    char self[MAX_PATH];
    char *slash;

    if (!GetModuleFileNameA(mono, self, sizeof(self))) return 0;
    slash = strrchr(self, '\\');            /* strip the file name */
    if (!slash) return 0;
    *slash = 0;
    slash = strrchr(self, '\\');            /* strip "bin" */
    if (!slash) return 0;
    *slash = 0;
    _snprintf(out, n, "%s\\lib\\%s", self, dll);
    out[n - 1] = 0;
    return 1;
}

static void shim_kick(void)
{
    static LONG once;
    HMODULE mono;
    char path[MAX_PATH];
    const char *env;
    fn_get_root_domain get_root_domain;
    fn_domain_get domain_get;
    fn_thread_attach thread_attach;
    fn_assembly_open assembly_open;
    fn_assembly_get_image assembly_get_image;
    fn_class_from_name class_from_name;
    fn_method_from_name method_from_name;
    fn_runtime_invoke runtime_invoke;
    void *domain, *assembly, *image, *klass, *method, *exc = NULL;

    if (InterlockedCompareExchange(&once, 1, 0) != 0) return;

    env = getenv("MYWHOOSH_SHIM_DLL");
    if (env && !*env) { logmsg("export shim disabled (MYWHOOSH_SHIM_DLL is empty)"); return; }

    mono = GetModuleHandleA("libmono-2.0-x86_64.dll");
    if (!mono) mono = GetModuleHandleA("libmono-2.0-x86.dll");
    if (!mono) { logmsg("export shim: libmono is not loaded, nothing to kick"); return; }

    get_root_domain     = (fn_get_root_domain)    GetProcAddress(mono, "mono_get_root_domain");
    domain_get          = (fn_domain_get)         GetProcAddress(mono, "mono_domain_get");
    thread_attach       = (fn_thread_attach)      GetProcAddress(mono, "mono_thread_attach");
    assembly_open       = (fn_assembly_open)      GetProcAddress(mono, "mono_domain_assembly_open");
    assembly_get_image  = (fn_assembly_get_image) GetProcAddress(mono, "mono_assembly_get_image");
    class_from_name     = (fn_class_from_name)    GetProcAddress(mono, "mono_class_from_name");
    method_from_name    = (fn_method_from_name)   GetProcAddress(mono, "mono_class_get_method_from_name");
    runtime_invoke      = (fn_runtime_invoke)     GetProcAddress(mono, "mono_runtime_invoke");

    if (!get_root_domain || !domain_get || !thread_attach || !assembly_open
        || !assembly_get_image || !class_from_name || !method_from_name || !runtime_invoke) {
        logmsg("export shim: libmono is missing an embedding entry point, giving up");
        return;
    }

    if (env) { _snprintf(path, sizeof(path), "%s", env); path[sizeof(path) - 1] = 0; }
    else if (!shim_path(mono, "MyWhooshShim.dll", path, sizeof(path))) {
        logmsg("export shim: cannot work out where MyWhooshShim.dll lives");
        return;
    }

    domain = domain_get();
    if (!domain) {
        domain = get_root_domain();
        if (!domain) { logmsg("export shim: no mono domain"); return; }
        thread_attach(domain);
    }

    assembly = assembly_open(domain, path);
    if (!assembly) { logmsg("export shim: cannot load %s", path); return; }
    image = assembly_get_image(assembly);
    klass = image ? class_from_name(image, "MyWhoosh", "ExportShim") : NULL;
    method = klass ? method_from_name(klass, "Install", 0) : NULL;
    if (!method) { logmsg("export shim: MyWhoosh.ExportShim::Install not found in %s", path); return; }

    logmsg("export shim: invoking Install from %s", path);
    runtime_invoke(method, NULL, NULL, &exc);
    if (exc) logmsg("export shim: Install threw (see the shim's own log)");
}

/* --------------------------------------------------------- class factory */

typedef struct {
    const IClassFactoryVtbl *vtbl;
    const GUID *clsid;
} factory;

static HRESULT STDMETHODCALLTYPE cf_QI(IClassFactory *this_, const IID *iid, void **out)
{
    if (!out) return E_POINTER;
    if (guid_eq(iid, &IID_IUnknown) || guid_eq(iid, &IID_IClassFactory)) { *out = this_; return S_OK; }
    *out = NULL;
    return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE cf_AddRef(IClassFactory *this_) { return 2; }
static ULONG STDMETHODCALLTYPE cf_Release(IClassFactory *this_) { return 1; }

static HRESULT STDMETHODCALLTYPE cf_CreateInstance(IClassFactory *this_, IUnknown *outer,
                                                   const IID *iid, void **out)
{
    factory *f = (factory *)this_;
    HRESULT hr;

    if (!out) return E_POINTER;
    *out = NULL;
    if (outer) return CLASS_E_NOAGGREGATION;

    /* The game's connectivity init is what got us here, so this is the earliest
     * moment our own managed code can run inside its process.  See shim_kick. */
    shim_kick();

    if (guid_eq(f->clsid, &CLSID_DNSSDService)) {
        svc *s = svc_new(SVC_MAIN);
        hr = svc_QI(s, iid, out);
        svc_Release(s);
        logmsg("created DNSSDService -> 0x%08lx", (unsigned long)hr);
    } else {
        evmgr *m = evmgr_new();
        hr = evmgr_QI(m, iid, out);
        evmgr_Release(m);
        logmsg("created DNSSDEventManager -> 0x%08lx", (unsigned long)hr);
    }
    return hr;
}

static HRESULT STDMETHODCALLTYPE cf_LockServer(IClassFactory *this_, BOOL lock) { return S_OK; }

static const IClassFactoryVtbl cf_vtbl = {
    cf_QI, cf_AddRef, cf_Release, cf_CreateInstance, cf_LockServer,
};

static factory factory_service = { &cf_vtbl, &CLSID_DNSSDService };
static factory factory_manager = { &cf_vtbl, &CLSID_DNSSDEventManager };

/* --------------------------------------------------------------- exports */

__declspec(dllexport) HRESULT WINAPI DllGetClassObject(const CLSID *clsid, const IID *iid, void **out)
{
    if (!out) return E_POINTER;
    if (guid_eq(clsid, &CLSID_DNSSDService))
        return cf_QI((IClassFactory *)&factory_service, iid, out);
    if (guid_eq(clsid, &CLSID_DNSSDEventManager))
        return cf_QI((IClassFactory *)&factory_manager, iid, out);
    *out = NULL;
    return CLASS_E_CLASSNOTAVAILABLE;
}

__declspec(dllexport) HRESULT WINAPI DllCanUnloadNow(void) { return S_FALSE; }

BOOL WINAPI DllMain(HINSTANCE dll, DWORD reason, void *reserved)
{
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(dll);
        InitializeCriticalSection(&loglock);
        tls_hwnd = TlsAlloc();
        load_config();
        {
            char caps[64];
            int i;
            for (i = 0; i < ndevs; i++)
                logmsg("loaded: name=\"%s\" serial=%s port=%d caps=%s power=%dW bpm=%d%s",
                       devs[i].name, devs[i].serial, devs[i].port,
                       caps_str(devs[i].caps, caps, sizeof caps),
                       devs[i].power, devs[i].bpm, devs[i].external ? " (served elsewhere)" : "");
        }
    }
    return TRUE;
}
