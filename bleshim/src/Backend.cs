// The one place that knows how this shim reaches real hardware.
//
// Everything above here is Windows.cs answering the game's WinRT calls; below
// here is Linux.  The two are separated on purpose, because the way across is
// expected to change: Wine's own Bluetooth stack (winebth.sys, BlueZ-backed)
// already enumerates GATT services and characteristics, and when it grows
// characteristic writes, notifications and advertisement scanning this file is
// the only one that has to be rewritten to use it.  Today it cannot -- see
// ../README.md for what was measured in both installed runners.
//
// Why a helper process at all.  BlueZ is reached over D-Bus, D-Bus is a unix
// socket, and Wine's winsock has no AF_UNIX: the constant is in winsock.h and
// nothing implements it.  Managed code inside the prefix therefore cannot speak
// to BlueZ directly however it is written, so blehelper.py does it on the Linux
// side and this talks to it over loopback TCP -- one hop, newline-delimited
// JSON, and a protocol dumb enough that moving it in-process later changes
// nothing above this file.
//
// The helper is optional in the sense that nothing here throws when it is
// absent: the game then sees a machine with no Bluetooth devices, which is what
// it saw from the stubs in ../../winmd and survived.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace MyWhoosh.Ble
{
    /// An advertisement, a notification or a connection change, as the helper
    /// reported it.  Deliberately flat: this is a transport type, not a model.
    class BackendEvent
    {
        public string Kind;                 // "advert" | "value" | "connection"
        public string Address;              // "AA:BB:CC:DD:EE:FF"
        public string Name;                 // advert only, may be empty
        public List<string> Uuids;          // advert only
        public string Characteristic;       // value only
        public byte[] Value;                // value only
        public bool Connected;              // connection only
    }

    static class Backend
    {
        public const int DefaultPort = 27019;

        // Long enough for a sleeping trainer to be woken and connected (the
        // helper waits for an advertisement rather than blocking in Connect(),
        // and that wait is bounded on its side), short enough that a helper
        // that died does not hang the game's UI thread forever.
        const int CallTimeoutMs = 40000;

        static readonly object Gate = new object();
        static TcpClient client;
        static StreamWriter writer;
        static Thread reader, dispatcher;
        static int nextId;
        static DateTime nextAttempt;        // do not retry a missing helper on every call
        static bool announcedMissing;       // ... and do not say so on every retry either
        static readonly Dictionary<int, Pending> Waiting = new Dictionary<int, Pending>();
        // A List used as a queue, not Queue<T>: Queue<T> lives in System.dll on
        // the framework wine-mono ships and in mscorlib on the host's mono, so
        // an assembly built here referencing it loads on the host and throws
        // TypeLoadException inside the prefix -- which is the only place that
        // matters.  Everything in this file is mscorlib-or-System.Net, checked
        // by running TestBle.exe under wine-mono.
        static readonly List<BackendEvent> Events = new List<BackendEvent>();

        /// Raised for every advertisement, notification and connection change.
        /// Handlers run on the dispatcher thread, never on the reader thread:
        /// a handler that blocks must not be able to stall a reply the caller
        /// of Call() is waiting for.
        public static event Action<BackendEvent> Received;

        class Pending
        {
            public readonly ManualResetEvent Done = new ManualResetEvent(false);
            public Dictionary<string, object> Reply;
        }

        // ------------------------------------------------------- connection

        /// True when there is a live helper connection, without raising if
        /// there is not: callers answer the game with "nothing found" instead.
        public static bool Available
        {
            get
            {
                try { return Connect(); }
                catch (Exception e) { Log("helper unavailable: " + e.Message); return false; }
            }
        }

        static bool Connect()
        {
            lock (Gate)
            {
                if (client != null && client.Connected) return true;
                // The game polls its device list about twice a second and asks
                // for the radio state on a ticker, so a helper that is not
                // running must not cost a connect() attempt per poll -- but it
                // must still be picked up when the user starts it, which is the
                // ordinary case of launching the game first.
                if (DateTime.UtcNow < nextAttempt) return false;
                nextAttempt = DateTime.UtcNow.AddSeconds(5);

                int port = DefaultPort;
                string env = Environment.GetEnvironmentVariable("MYWHOOSH_BLE_PORT");
                if (!string.IsNullOrEmpty(env)) int.TryParse(env, out port);

                try
                {
                    var c = new TcpClient();
                    c.Connect("127.0.0.1", port);
                    c.NoDelay = true;       // notifications are small and want to be prompt
                    client = c;
                    var stream = c.GetStream();
                    writer = new StreamWriter(stream, new UTF8Encoding(false)) { AutoFlush = true };

                    reader = new Thread(() => ReadLoop(new StreamReader(stream, new UTF8Encoding(false))))
                        { IsBackground = true, Name = "bleshim-reader" };
                    reader.Start();
                    if (dispatcher == null)
                    {
                        dispatcher = new Thread(DispatchLoop)
                            { IsBackground = true, Name = "bleshim-events" };
                        dispatcher.Start();
                    }
                    announcedMissing = false;
                    Log("connected to blehelper on 127.0.0.1:" + port);
                    return true;
                }
                catch (SocketException e)
                {
                    // Once per retry window, not once per poll.
                    if (!announcedMissing)
                    {
                        announcedMissing = true;
                        Log("no helper on 127.0.0.1:" + port + " (" + e.SocketErrorCode + ")"
                            + " -- start blehelper.py; the game sees no Bluetooth until then");
                    }
                    return false;
                }
            }
        }

        static void Drop(string why)
        {
            lock (Gate)
            {
                if (client == null) return;
                Log("helper connection lost: " + why);
                try { client.Close(); } catch { }
                client = null;
                writer = null;
                // Every caller blocked on a reply is now waiting for one that
                // will never arrive; release them with an empty reply, which
                // reads as a failed call.
                foreach (var p in Waiting.Values) p.Done.Set();
                Waiting.Clear();
            }
        }

        // ------------------------------------------------------------ calls

        /// Send one request and wait for its reply.  Returns null when the
        /// helper is absent, timed out or answered with an error -- the callers
        /// are WinRT methods that must never throw into the game.
        public static Dictionary<string, object> Call(params object[] pairs)
        {
            if (!Available) return null;

            int id;
            var pending = new Pending();
            lock (Gate)
            {
                if (writer == null) return null;
                id = ++nextId;
                Waiting[id] = pending;
            }

            var full = new object[pairs.Length + 2];
            full[0] = "id"; full[1] = id;
            Array.Copy(pairs, 0, full, 2, pairs.Length);
            string line = Json.Write(full);

            try
            {
                lock (Gate)
                {
                    if (writer == null) return null;
                    writer.Write(line);
                    writer.Write('\n');
                }
            }
            catch (Exception e)
            {
                lock (Gate) Waiting.Remove(id);
                Drop("write failed: " + e.Message);
                return null;
            }

            if (!pending.Done.WaitOne(CallTimeoutMs))
            {
                lock (Gate) Waiting.Remove(id);
                Log("timeout after " + CallTimeoutMs + "ms: " + line);
                return null;
            }

            var reply = pending.Reply;
            if (reply == null) return null;
            if (!Json.Bool(reply, "ok"))
            {
                Log("helper refused " + line + ": " + Json.Str(reply, "error", "(no reason given)"));
                return null;
            }
            return reply;
        }

        static void ReadLoop(StreamReader r)
        {
            try
            {
                string line;
                while ((line = r.ReadLine()) != null)
                {
                    Dictionary<string, object> msg;
                    try { msg = Json.ParseObject(line); }
                    catch (Exception e) { Log("unparsable line from helper (" + e.Message + ")"); continue; }

                    string kind = Json.Str(msg, "ev");
                    if (kind != null) { Enqueue(kind, msg); continue; }

                    int id = (int)Json.Num(msg, "id", -1);
                    Pending p = null;
                    lock (Gate)
                    {
                        if (Waiting.TryGetValue(id, out p)) Waiting.Remove(id);
                    }
                    if (p == null) { Log("reply to unknown request " + id); continue; }
                    p.Reply = msg;
                    p.Done.Set();
                }
                Drop("helper closed the connection");
            }
            catch (Exception e)
            {
                // A process on its way out aborts this thread, and the abort
                // surfaces wrapped in whatever the read was doing.  That is not
                // a helper that went away, and saying so on every exit would
                // teach the reader to ignore the line that matters.
                if (e is ThreadAbortException || e.InnerException is ThreadAbortException) return;
                Drop(e.Message);
            }
        }

        static void Enqueue(string kind, Dictionary<string, object> msg)
        {
            var ev = new BackendEvent
            {
                Kind = kind,
                Address = Json.Str(msg, "addr", ""),
                Name = Json.Str(msg, "name", ""),
                Characteristic = Json.Str(msg, "char"),
                Connected = Json.Bool(msg, "connected"),
            };
            string hex = Json.Str(msg, "value");
            if (hex != null)
            {
                try { ev.Value = Json.FromHex(hex); }
                catch (Exception e) { Log("bad value payload (" + e.Message + ")"); return; }
            }
            var uuids = new List<string>();
            foreach (object u in Json.Arr(msg, "uuids"))
            {
                string s = u as string;
                if (s != null) uuids.Add(s.ToLowerInvariant());
            }
            ev.Uuids = uuids;

            lock (Events)
            {
                // A game that stopped reading must not be able to grow this
                // without bound; advertisements are the only high-rate message
                // and the newest one is always the useful one.
                if (Events.Count > 512) Events.RemoveAt(0);
                Events.Add(ev);
                Monitor.Pulse(Events);
            }
        }

        static void DispatchLoop()
        {
            while (true)
            {
                BackendEvent ev;
                lock (Events)
                {
                    while (Events.Count == 0) Monitor.Wait(Events);
                    ev = Events[0];
                    Events.RemoveAt(0);
                }
                var handler = Received;
                if (handler == null) continue;
                // One handler throwing is a bug in Windows.cs, not a reason to
                // lose every later notification.
                try { handler(ev); }
                catch (Exception e) { Log("event handler threw: " + e); }
            }
        }

        // ----------------------------------------------------------- radios

        /// The machine's Bluetooth adapters and whether each is powered.
        ///
        /// This goes through the helper like everything else, and it is worth
        /// saying why, because the obvious shortcut does not work: Wine maps
        /// the Linux root at Z:, so /sys/class/bluetooth can be listed from
        /// inside the prefix with no helper at all, reading each adapter's
        /// `flags` file for IFF_UP -- which is how an earlier experiment did it.
        /// There is no `flags` file on a current kernel.  The
        /// adapter here is up, running and scanning, and that read reports it
        /// off, which is the one answer that makes the game refuse Bluetooth
        /// outright.
        ///
        /// BlueZ's own Adapter1.Powered cannot be wrong in that way, and the
        /// helper is where BlueZ is reachable.  No helper therefore means no
        /// radios, which is accurate rather than pessimistic: without it
        /// nothing in this shim can reach a device even if an adapter is lit.
        public static List<KeyValuePair<string, bool>> Radios()
        {
            var found = new List<KeyValuePair<string, bool>>();
            var reply = Call("op", "radios");
            if (reply == null) return found;
            foreach (object entry in Json.Arr(reply, "radios"))
            {
                var o = Json.Obj(entry);
                string name = Json.Str(o, "name");
                if (name != null) found.Add(new KeyValuePair<string, bool>(name, Json.Bool(o, "powered")));
            }
            return found;
        }

        // -------------------------------------------------------------- log

        static TextWriter log;

        public static void Log(string msg)
        {
            string line = "[" + DateTime.Now.ToString("HH:mm:ss.fff", CultureInfo.InvariantCulture)
                        + " bleshim] " + msg;
            try
            {
                if (log == null) log = OpenLog();
                log.WriteLine(line);
            }
            catch
            {
                // Logging is never worth taking the game down for.
            }
        }

        /// Where the log goes.  MYWHOOSH_SHIM_LOG is written by hand or by
        /// run.sh, so it is a Linux path -- and inside the prefix that is not a
        /// path at all: wine-mono reads "/tmp/x" as C:\\tmp\\x, which does not
        /// exist, and the whole shim then logs nothing at the one moment you
        /// need it.  Z: is Wine's mapping of the Linux root, so try that first
        /// and fall back to stderr rather than going quiet.
        static TextWriter OpenLog()
        {
            string path = Environment.GetEnvironmentVariable("MYWHOOSH_SHIM_LOG");
            if (string.IsNullOrEmpty(path)) return Console.Error;

            bool onWindows = Path.DirectorySeparatorChar == '\\';
            string[] tries = onWindows && path[0] == '/'
                ? new string[] { "Z:" + path.Replace('/', '\\'), path }
                : new string[] { path };

            foreach (string candidate in tries)
            {
                try
                {
                    return TextWriter.Synchronized(new StreamWriter(
                        new FileStream(candidate, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
                    { AutoFlush = true });
                }
                catch { }
            }
            Console.Error.WriteLine("[bleshim] cannot write " + path + ", logging here instead");
            return Console.Error;
        }
    }
}
