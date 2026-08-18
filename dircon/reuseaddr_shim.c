/*
 * reuseaddr_shim -- LD_PRELOAD helper for running Apple's mDNSResponder in Wine
 * alongside a host avahi-daemon.
 *
 * On Windows, several sockets may bind the same UDP port unless one of them asks
 * for SO_EXCLUSIVEADDRUSE, so mDNSResponder never sets SO_REUSEADDR.  On Linux
 * that bind fails with EADDRINUSE when avahi-daemon already holds 0.0.0.0:5353,
 * and mDNSResponder silently falls back to an ephemeral port -- it keeps serving
 * clients but can no longer send or receive any mDNS traffic, so every browse
 * comes back empty.
 *
 * Setting SO_REUSEADDR on the UDP socket is enough for the two stacks to coexist
 * (both join the same multicast group and each receives the traffic).  This shim
 * sets it on every datagram socket the process creates, which restores the
 * Windows behaviour without patching Wine or stopping avahi.
 *
 * Build: cc -shared -fPIC -o reuseaddr_shim.so reuseaddr_shim.c -ldl
 * Use:   LD_PRELOAD=$PWD/reuseaddr_shim.so wine ...
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <sys/socket.h>

static int (*real_socket)(int, int, int);

int socket(int domain, int type, int protocol)
{
    if (!real_socket) real_socket = dlsym(RTLD_NEXT, "socket");

    int fd = real_socket(domain, type, protocol);
    if (fd >= 0 && (type & 0xff) == SOCK_DGRAM)
    {
        int one = 1;
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
#ifdef SO_REUSEPORT
        setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
#endif
    }
    return fd;
}
