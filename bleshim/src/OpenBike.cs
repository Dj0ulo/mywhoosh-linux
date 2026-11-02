// The game's OpenBike path -- network controllers such as BikeControl -- with
// Avahi doing the browsing Apple Bonjour does on Windows.
//
// What the game does, read out of WindowsConnectivity.dll's IL.  Three exports:
//
//     OBC_Initialize(OpenBikeDataDelegate cb)   new OpenBikeManager, OBM_Initialize,
//                                               then DelegateCallbacks.OpenBikeDataCallback = cb
//     OBC_StartScan()                           OBM_StartScan
//     OBC_StopScan()                            OBM_StopScan
//
// OBM_StartScan browses Bonjour for "_openbikecontrol._tcp." and, for each
// instance it resolves, OpenBikeManager::ServiceResolved fills an
// FOpenBikeDataStruct { string ipAddress; int port; string hostName; } and
// invokes DelegateCallbacks.OpenBikeDataCallback with it.  That callback is the
// engine's native code; it opens the TCP connection to the controller itself,
// with its own winsock, which works under Wine as it is.
//
// The browse is what does not work: it goes through Bonjour's COM objects and
// ComAwareEventInfo, behind the Bonjour gate (../ENGINEERING.md).  With the gate
// shut OBM_Initialize and OBM_StartScan both return at their first branch -- but
// OBC_Initialize still stores the callback, after OBM_Initialize returns.  So
// everything the game needs is in place except someone to call it, and this is
// that someone: ../../exportshim points OBC_StartScan and OBC_StopScan here,
// blehelper.py browses with Avahi, and each "service" event it sends becomes one
// call of the game's own callback, with the struct ServiceResolved would have
// built.
//
// Reflection, not a reference, for the game's types: this assembly is built
// without WindowsConnectivity.dll, and is loaded into processes that have none
// (TestBle.exe), where everything here is a logged no-op.

using System;
using System.Collections.Generic;
using System.Reflection;
using System.Threading;

namespace MyWhoosh.Ble
{
    public static class OpenBike
    {
        public const string ServiceType = "_openbikecontrol._tcp";

        const string GameAssembly = "WindowsConnectivity";
        const string CallbacksType = "ConnectivityConstants.DelegateCallbacks";
        const string CallbackField = "OpenBikeDataCallback";
        const string DataType = "ConnectivityConstants.FOpenBikeDataStruct";

        // How often the helper's browse is checked against what the game
        // asked for, and how long to leave a helper that refused it (no
        // avahi-daemon, say) before asking again.
        const int CheckMs = 5000;
        static readonly TimeSpan RefusedRetry = TimeSpan.FromSeconds(30);

        static readonly object Gate = new object();
        static bool subscribed;
        static bool scanning;               // what the game asked for

        // What the helper is doing for us, kept in step with `scanning` by
        // Reconcile.  The helper drops its browse when our connection goes
        // away, so "browsing" is recorded as the Backend.Generation it was
        // set up on; -1 is "not browsing".  Without this, a browse asked for
        // before the helper was up -- or lost when it restarted -- would
        // never be asked for again, and the game asks only once per screen.
        static readonly object ReconcileGate = new object();
        static Timer checker;
        static int browsingOn = -1;
        static DateTime nextTry;
        static bool again;                  // a request came in mid-Reconcile
        // The services the game has been told about this scan, by name.  The
        // helper reports an instance once per interface it is seen on, and
        // again on a second browse; ServiceFound on Windows resolves each name
        // once (its `services.ContainsKey(fullName)`), and the engine dials
        // every address it is handed, so it is handed one.  A Dictionary used
        // as a set, not HashSet<T>: that lives in System.Core, and Backend.cs
        // has the story of a collection type that is in a different assembly
        // under wine-mono than on the host.  Everything here is mscorlib.
        static readonly Dictionary<string, bool> Delivered = new Dictionary<string, bool>();

        /// The game asked to look for network controllers.  Called from the
        /// OBC_StartScan export through ../../exportshim, so from native code:
        /// it must never throw, and it must not block the caller on the helper.
        public static void StartScan()
        {
            try
            {
                lock (Gate)
                {
                    Subscribe();
                    scanning = true;
                    Delivered.Clear();
                    nextTry = DateTime.MinValue;
                }
                Backend.Log("OpenBike: scan requested");
                Kick();
            }
            catch (Exception e) { Backend.Log("OpenBike: StartScan failed: " + e.Message); }
        }

        /// The game stopped looking.  Same rules as StartScan.
        public static void StopScan()
        {
            try
            {
                lock (Gate)
                {
                    scanning = false;
                    nextTry = DateTime.MinValue;
                }
                Backend.Log("OpenBike: scan stopped");
                Kick();
            }
            catch (Exception e) { Backend.Log("OpenBike: StopScan failed: " + e.Message); }
        }

        /// Called by ../../exportshim once the two exports are redirected.
        /// The shim is started from the Bluetooth path's first types, and the
        /// game may well have initialized OpenBike and asked for its scan
        /// before then -- through the original exports, which did nothing.
        /// A stored callback is the only trace that leaves -- it says the game
        /// initialized OpenBike, not whether it asked for a scan -- so start
        /// one: the game may believe it is running, and one it did not ask
        /// for only finds controllers sooner.
        public static void Attach()
        {
            try
            {
                if (GameCallback() != null)
                {
                    Backend.Log("OpenBike: the game set up OpenBike before the exports were redirected;"
                                + " any scan it asked for went nowhere, so starting one now");
                    StartScan();
                }
            }
            catch (Exception e) { Backend.Log("OpenBike: Attach failed: " + e.Message); }
        }

        /// Bring Reconcile forward to now, on a thread-pool thread: the
        /// callers are the game's own exports, which must not wait on the
        /// helper.  The timer keeps running every CheckMs after that, which
        /// is what notices a helper that was restarted.
        static void Kick()
        {
            lock (Gate)
            {
                if (checker == null)
                    checker = new Timer(_ => Reconcile(), null, 0, CheckMs);
                else
                    checker.Change(0, CheckMs);
            }
        }

        /// Make the helper's browse match `scanning`.  Runs on timer threads,
        /// one at a time; a request arriving while one runs makes it go round
        /// again rather than waiting behind a Call that may take a while.
        static void Reconcile()
        {
            try
            {
                if (!Monitor.TryEnter(ReconcileGate))
                {
                    lock (Gate) again = true;
                    return;
                }
                try
                {
                    while (true)
                    {
                        lock (Gate) again = false;
                        ReconcileOnce();
                        lock (Gate) if (!again) break;
                    }
                }
                finally { Monitor.Exit(ReconcileGate); }
            }
            catch (Exception e) { Backend.Log("OpenBike: keeping the browse in step failed: " + e.Message); }
        }

        static void ReconcileOnce()
        {
            bool want;
            lock (Gate)
            {
                want = scanning;
                if (!want && browsingOn < 0) return;        // idle: not even a helper check
                if (DateTime.UtcNow < nextTry) return;
            }
            if (!Backend.Available) return;                 // retried on the next tick
            // Read before the call: a reconnect in between only means one
            // browse too many next tick, and browse is idempotent.
            int gen = Backend.Generation;

            if (want)
            {
                if (browsingOn == gen) return;
                if (browsingOn >= 0)
                    Backend.Log("OpenBike: the helper connection was replaced; asking it to browse again");
                if (Backend.Call("op", "browse", "type", ServiceType, "enable", true) != null)
                {
                    browsingOn = gen;
                    return;
                }
                Backend.Log("OpenBike: the helper cannot browse for " + ServiceType
                            + " -- is avahi-daemon running?  Asking again in "
                            + (int)RefusedRetry.TotalSeconds + " s");
                lock (Gate) nextTry = DateTime.UtcNow + RefusedRetry;
            }
            else
            {
                // A browse set up on an earlier connection died with it.
                if (browsingOn == gen)
                    Backend.Call("op", "browse", "type", ServiceType, "enable", false);
                browsingOn = -1;
            }
        }

        static void Subscribe()
        {
            if (subscribed) return;
            subscribed = true;
            Backend.Received += OnEvent;
        }

        static void OnEvent(BackendEvent ev)
        {
            if (ev.ServiceType == null || ev.ServiceType.TrimEnd('.') != ServiceType) return;

            if (ev.Kind == "service_lost")
            {
                // Nothing to tell the game: it has no "lost" callback, and its
                // own connection to the controller notices on its own.  Forget
                // it, so the controller coming back is handed over again.
                lock (Gate) Delivered.Remove(ev.Name ?? "");
                Backend.Log("OpenBike: " + ev.Name + " went away");
                return;
            }
            if (ev.Kind != "service") return;

            // The same test ServiceResolved makes before calling back: an
            // address, and one long enough to be one.
            if (string.IsNullOrEmpty(ev.Ip) || ev.Ip.Length < 5 || ev.Port <= 0)
            {
                Backend.Log("OpenBike: " + ev.Name + " resolved without a usable address, ignored");
                return;
            }

            string key = ev.Name ?? "";
            lock (Gate)
            {
                if (!scanning) return;
                if (Delivered.ContainsKey(key)) return;
                Delivered[key] = true;
            }
            // Not on this thread: it is Backend's one event dispatcher, which
            // also carries the trainer's and the heart-rate strap's values,
            // and the engine's callback may well dial the controller before
            // it returns.  A phone gone quiet must not hold up the power.
            ThreadPool.QueueUserWorkItem(_ => Deliver(ev));
        }

        /// Hand one controller to the game, as OpenBikeManager::ServiceResolved
        /// does: a fresh FOpenBikeDataStruct, Initialize()d, the three fields
        /// set, through DelegateCallbacks.OpenBikeDataCallback.  Runs on a
        /// thread-pool thread, never the game's own; ServiceResolved blocks on
        /// a DNS lookup before calling back, so the game cannot expect to be
        /// called from anywhere in particular.  Never throws: a worker thread
        /// with an escaping exception takes the process down.
        static void Deliver(BackendEvent ev)
        {
            try
            {
                Delegate callback = GameCallback();
                if (callback == null)
                {
                    Backend.Log("OpenBike: found " + ev.Name + " but the game has no callback yet");
                    lock (Gate) Delivered.Remove(ev.Name ?? "");
                    return;
                }
                Type dataType = FindGameType(DataType);
                if (dataType == null) { Backend.Log("OpenBike: no " + DataType + " in the game"); return; }

                object data = Activator.CreateInstance(dataType);
                MethodInfo init = dataType.GetMethod("Initialize", BindingFlags.Public | BindingFlags.Instance);
                if (init != null) init.Invoke(data, null);   // on the box, so it sticks
                Set(dataType, data, "ipAddress", ev.Ip);
                Set(dataType, data, "port", ev.Port);
                Set(dataType, data, "hostName", ev.Host ?? "");

                Backend.Log("OpenBike: handing " + ev.Name + " at " + ev.Ip + ":" + ev.Port
                            + " (" + ev.Host + ") to the game");
                callback.DynamicInvoke(data);
            }
            catch (Exception e)
            {
                Exception inner = e is TargetInvocationException && e.InnerException != null ? e.InnerException : e;
                Backend.Log("OpenBike: handing " + ev.Name + " to the game failed: "
                            + inner.GetType().Name + ": " + inner.Message);
            }
        }

        static void Set(Type t, object box, string field, object value)
        {
            FieldInfo f = t.GetField(field, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (f == null) throw new MissingFieldException(t.FullName, field);
            f.SetValue(box, value);
        }

        static Delegate GameCallback()
        {
            Type t = FindGameType(CallbacksType);
            if (t == null) return null;
            FieldInfo f = t.GetField(CallbackField, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
            return f == null ? null : f.GetValue(null) as Delegate;
        }

        static Type FindGameType(string name)
        {
            foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
            {
                if (a.GetName().Name != GameAssembly) continue;
                Type t = a.GetType(name, false);
                if (t != null) return t;
            }
            return null;
        }
    }
}
