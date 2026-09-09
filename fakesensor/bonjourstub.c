/*
 * A Windows service that exists only to be named "Bonjour Service".
 *
 * Before MyWhoosh does anything Direct Connect, GetNetworkState() looks up a
 * service by that exact name and refuses to go on unless the SCM reports it
 * Running (../dircon/README.md, Blocker 2).  Nothing behind the gate is ever
 * asked of it: discovery is answered by fakebonjour.dll inside the game's own
 * process, and the trainer is served over TCP.  Only the name and the state are
 * read, so only the name and the state are provided here.
 *
 * That is worth a file of its own because the alternative is installing Apple's
 * mDNSResponder out of the game's SDK bundle -- which needs 7z, writes a real
 * service into the prefix, and under Wine never joins the multicast group
 * anyway, so it answers nothing.  This is the same gate, opened with 60 lines.
 *
 * Install with sc.exe, not by running it: started outside the SCM it exits.
 *
 *   sc create "Bonjour Service" binPath= C:\windows\bonjourstub.exe start= auto
 *   net start "Bonjour Service"
 */
#include <windows.h>

#define SVC_NAME "Bonjour Service"

static SERVICE_STATUS_HANDLE status_handle;
static SERVICE_STATUS status;
static HANDLE stop_event;

static void report(DWORD state, DWORD exit_code)
{
    status.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    status.dwCurrentState = state;
    /* Stop is the only control worth accepting; there is nothing to pause. */
    status.dwControlsAccepted = (state == SERVICE_RUNNING) ? SERVICE_ACCEPT_STOP : 0;
    status.dwWin32ExitCode = exit_code;
    status.dwCheckPoint = 0;
    status.dwWaitHint = 0;
    if (status_handle)
        SetServiceStatus(status_handle, &status);
}

static DWORD WINAPI control_handler(DWORD control, DWORD type, LPVOID data, LPVOID ctx)
{
    (void)type; (void)data; (void)ctx;
    switch (control) {
    case SERVICE_CONTROL_STOP:
    case SERVICE_CONTROL_SHUTDOWN:
        report(SERVICE_STOP_PENDING, NO_ERROR);
        SetEvent(stop_event);
        return NO_ERROR;
    case SERVICE_CONTROL_INTERROGATE:
        report(status.dwCurrentState, NO_ERROR);
        return NO_ERROR;
    default:
        return ERROR_CALL_NOT_IMPLEMENTED;
    }
}

static void WINAPI service_main(DWORD argc, LPSTR *argv)
{
    (void)argc; (void)argv;

    status_handle = RegisterServiceCtrlHandlerExA(SVC_NAME, control_handler, NULL);
    if (!status_handle)
        return;

    stop_event = CreateEventA(NULL, TRUE, FALSE, NULL);
    if (!stop_event) {
        report(SERVICE_STOPPED, GetLastError());
        return;
    }

    report(SERVICE_RUNNING, NO_ERROR);
    WaitForSingleObject(stop_event, INFINITE);
    report(SERVICE_STOPPED, NO_ERROR);
}

int main(void)
{
    SERVICE_TABLE_ENTRYA table[] = {
        { (LPSTR)SVC_NAME, service_main },
        { NULL, NULL },
    };
    /* Fails with ERROR_FAILED_SERVICE_CONTROLLER_CONNECT when run by hand. */
    return StartServiceCtrlDispatcherA(table) ? 0 : (int)GetLastError();
}
