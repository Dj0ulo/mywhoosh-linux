/*
 * Resolve *.local to one fixed address, for Wine processes only.
 *
 * MyWhoosh connects to a Dircon sensor by the host name Bonjour reported --
 * DirconSensor does Dns.GetHostAddresses(WFTNP_HostName) -- and that host name
 * has to be "<service name>.local.", because ServiceResolved looks its own
 * service table up by it.  Nothing under Wine resolves .local names: Wine
 * ignores the Windows hosts file (measured) and hands the lookup to the host
 * resolver, which only answers .local if nss-mdns is installed and something is
 * publishing the name.
 *
 * LD_PRELOAD this and the lookup answers 127.0.0.1 (FAKESENSOR_ADDR overrides).
 * Same trick as ../dircon/reuseaddr_shim.c: no root, no Wine patch, and it
 * touches nothing outside the process it is preloaded into.
 *
 * Note it returns exactly ONE address on purpose: given several, DirconSensor
 * keeps only one that contains "192.168" and ends up with none.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <netdb.h>
#include <stdlib.h>
#include <string.h>

static const char *target(void)
{
    const char *v = getenv("FAKESENSOR_ADDR");
    return (v && *v) ? v : "127.0.0.1";
}

static int is_dot_local(const char *name)
{
    size_t n;
    if (!name) return 0;
    n = strlen(name);
    if (n && name[n - 1] == '.') n--;          /* fully qualified form */
    return n > 6 && strncasecmp(name + n - 6, ".local", 6) == 0;
}

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res)
{
    static int (*real)(const char *, const char *, const struct addrinfo *, struct addrinfo **);
    if (!real) real = dlsym(RTLD_NEXT, "getaddrinfo");
    if (is_dot_local(node)) node = target();
    return real(node, service, hints, res);
}

struct hostent *gethostbyname(const char *name)
{
    static struct hostent *(*real)(const char *);
    if (!real) real = dlsym(RTLD_NEXT, "gethostbyname");
    if (is_dot_local(name)) name = target();
    return real(name);
}
