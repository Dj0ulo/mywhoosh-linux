// Serve the four byref-array exports of WindowsConnectivity.dll ourselves,
// because wine-mono's native marshaller will not -- and put right the return
// register of the twelve that return a float (see "float returns" below).
//
// The problem.  Four of the DLL's 98 unmanaged exports take the device list by
// reference:
//
//     int WD_GetScannedDevicesList  (out DeviceInformationStruct[] devices)
//     int WD_GetConnectedDevicesList(out DeviceInformationStruct[] devices)
//     int BT_GetScannedDevicesList  (out DeviceInformationStruct[] devices)
//     int BT_GetConnectedDevicesList(out DeviceInformationStruct[] devices)
//
// and they are exactly the pollers the game's UI calls to fill its device list.
// mono's array marshaller refuses that shape from native code -- see
// mono/metadata/marshal-ilgen.c, emit_marshal_array_ilgen,
// MARSHAL_ACTION_MANAGED_CONV_IN:
//
//     if (t->byref) { ... "Byref array marshalling to managed code is not
//                          implemented." }
//
// The refusal is compiled *into* the wrapper as a throw, so the wrapper builds
// and the first call raises MarshalDirectiveException from a native-to-managed
// frame, which is fatal.  It is native runtime code, so no rewrite of a managed
// assembly reaches it, and neither does a newer wine-mono (10.0.0 and 11.1.0
// both carry the string in libmono-2.0-x86_64.dll, and it is in no managed
// assembly).
//
// Two further walls stand behind that one, which is why "delete the byref
// check" is not the fix either: the same function needs a [MarshalAs] on the
// parameter to pick a native array shape, and then a SizeConst or
// SizeParamIndex to know how many elements to read -- and the game's metadata
// has none of them (measured: the parameter carries only [Out]).  Nothing short
// of implementing the out-direction from scratch inside libmono works, and that
// means shipping a self-built runtime.
//
// What this does instead.  The exports are not ordinary code.  Each is a
// 12-byte stub
//
//     48 A1 <abs64>   mov rax, [slot]
//     FF E0           jmp rax
//
// reading a slot in `.sdata` that belongs to the CLI header's VTableFixups
// array (98 slots at RVA 0x36000, type COR_VTABLE_64BIT|FROM_UNMANAGED).  The
// slots hold MethodDef tokens on disk, and mscoree overwrites each with mono's
// native-to-managed thunk at load.  So:
//
//   * the game reads the slot on *every* call, even through a function pointer
//     it cached from GetProcAddress long before -- there is no window to miss;
//   * writing a slot needs nothing but a store to already-writable memory in
//     our own process, and touches no file.
//
// That last point is the constraint everything here lives under: MyWhoosh
// hashes WindowsConnectivity.dll and silently declines to load it if a single
// byte differs (see ../winmd/README.md), so the file must stay pristine.  It
// does -- this rewrites pointers in a loaded image, after the hash check has
// already passed.
//
// The replacement is managed, which is the point: the marshalling mono will not
// generate is three lines of Marshal calls when written by hand, against the
// same layout mono itself computes (Marshal.SizeOf reports 64 bytes for
// DeviceInformationStruct under wine-mono).  We call the managed method
// directly -- no marshalling at all on that side -- and hand the result out the
// way the CLR does on Windows: a fresh CoTaskMemAlloc block of `count`
// elements, its address stored through the pointer, the count returned.
//
// Something inside the game has to call Install().  Here that is
// ../bleshim/src/Loader.cs, from a static constructor of the assembly the game
// loads for Bluetooth -- which runs before the first device-list poll.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

namespace MyWhoosh
{
    public static class ExportShim
    {
        const string GameAssembly = "WindowsConnectivity";
        const string GameType = "FunctionsManager.MyWhoosh";

        static readonly string[] Exports =
        {
            "WD_GetScannedDevicesList",
            "WD_GetConnectedDevicesList",
            "BT_GetScannedDevicesList",
            "BT_GetConnectedDevicesList",
        };

        // The stub every export starts with: mov rax,[abs64] / jmp rax.  The
        // absolute address is the vtable-fixup slot the game dereferences.
        const int StubSlotOffset = 2;
        static readonly byte[] StubPrefix = { 0x48, 0xA1 };
        static readonly byte[] StubSuffix = { 0xFF, 0xE0 };

        const uint PAGE_READWRITE = 0x04;

        [DllImport("kernel32", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr GetModuleHandleW(string name);

        [DllImport("kernel32", CharSet = CharSet.Ansi, SetLastError = true,
                   BestFitMapping = false, ExactSpelling = true)]
        static extern IntPtr GetProcAddress(IntPtr module, string name);

        [DllImport("kernel32", SetLastError = true)]
        static extern bool VirtualProtect(IntPtr addr, IntPtr size, uint prot, out uint old);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        delegate int GetListFn(IntPtr ppDevices);

        static readonly object Gate = new object();
        static bool installed;
        // GetFunctionPointerForDelegate does not root the delegate, and a
        // collected one leaves the slot pointing at freed trampoline code.
        static readonly List<object> Rooted = new List<object>();

        static TextWriter log;

        /// Redirect the exports.  Idempotent, and never throws: it is
        /// called from native code, where an escaping exception is fatal.
        public static void Install()
        {
            lock (Gate)
            {
                if (installed) return;
                installed = true;
                try { InstallCore(); }
                catch (Exception e) { Log("Install failed: " + e); }
            }
        }

        static void InstallCore()
        {
            Log("installing (pid " + System.Diagnostics.Process.GetCurrentProcess().Id
                + ", runtime " + Environment.Version + ")");

            Type game = FindGameType();
            if (game == null) { Log("FATAL: " + GameType + " not found; nothing hooked"); return; }

            IntPtr module = GetModuleHandleW("WindowsConnectivity.dll");
            if (module == IntPtr.Zero) module = GetModuleHandleW("WindowsConnectivity");
            if (module == IntPtr.Zero) { Log("FATAL: WindowsConnectivity.dll is not loaded"); return; }
            Log("module at 0x" + module.ToString("x16"));

            int done = 0;
            foreach (string name in Exports)
                if (Hook(module, game, name)) done++;
            Log("hooked " + done + "/" + Exports.Length + " exports");

            MirrorFloatReturns(module, game);
        }


        // ------------------------------------------------- float returns

        // The ride HUD showed a heap address for heart rate -- 861,795,712
        // one run, 864,271,936 the next -- while BT_GetHeart, called through
        // the very same export, answered 88.  The IL is `(float)GetHeart()`,
        // so the answer leaves in xmm0.  The engine declares the export as
        // returning an int and reads rax instead, which the CLR happens to
        // leave holding the int it converted from, and mono's
        // native-to-managed wrapper leaves holding a pointer (measured in the
        // prefix: xmm0 -1.0, rax 0xc5f000).
        //
        // So route every float- or double-returning export through a few
        // bytes that call mono's thunk and then copy the truncated result
        // into eax as well.  A caller that reads xmm0 sees no difference.
        //
        // What the slot holds before the first call is not the wrapper but a
        // compile-on-demand trampoline, and the first call through it
        // overwrites the slot with the compiled wrapper -- replacing us.  So
        // the stub calls through a cell of its own, and after each call, if
        // the slot no longer points at the stub, moves what mono put there
        // into the cell and takes the slot back.  (The four device-list hooks
        // never call the trampoline, which is why they do not need this.)
        //
        //     sub  rsp, 28h
        //     mov  rax, <cell>             ; starts as mono's trampoline
        //     call [rax]
        //     mov  r11, <slot>
        //     mov  r10, [r11]
        //     mov  rcx, <this stub>
        //     cmp  rcx, r10
        //     je   done
        //     mov  rax, <cell>
        //     mov  [rax], r10              ; mono's compiled wrapper
        //     mov  [r11], rcx              ; the slot, back to us
        // done:
        //     cvttss2si eax, xmm0          ; cvttsd2si for double
        //     add  rsp, 28h
        //     ret

        const int MirrorSize = 96;
        const int MirrorCell = 88;

        [StructLayout(LayoutKind.Sequential)]
        struct RuntimeFunction { public uint Begin, End, Unwind; }

        [DllImport("kernel32", SetLastError = true)]
        static extern IntPtr VirtualAlloc(IntPtr addr, IntPtr size, uint type, uint prot);

        [DllImport("kernel32")]
        static extern bool FlushInstructionCache(IntPtr process, IntPtr addr, IntPtr size);

        [DllImport("kernel32")]
        static extern IntPtr GetCurrentProcess();

        [DllImport("ntdll")]
        static extern bool RtlAddFunctionTable(IntPtr table, uint count, ulong baseAddress);

        static void MirrorFloatReturns(IntPtr module, Type game)
        {
            var targets = new List<KeyValuePair<string, bool>>();   // name, is double
            foreach (MethodInfo mi in game.GetMethods(BindingFlags.Public | BindingFlags.NonPublic
                                                      | BindingFlags.Static))
            {
                if (mi.ReturnType != typeof(float) && mi.ReturnType != typeof(double)) continue;
                if (GetProcAddress(module, mi.Name) == IntPtr.Zero) continue;
                targets.Add(new KeyValuePair<string, bool>(mi.Name, mi.ReturnType == typeof(double)));
            }
            if (targets.Count == 0) { Log("no float-returning exports"); return; }

            // One page: the stubs, then one unwind record they all share,
            // then the function table.  Without the table the stubs are
            // frames nothing can unwind through.
            const uint MEM_COMMIT_RESERVE = 0x3000, PAGE_EXECUTE_READWRITE = 0x40;
            int unwindAt = targets.Count * MirrorSize;
            int tableAt = unwindAt + 8;
            int total = tableAt + targets.Count * Marshal.SizeOf(typeof(RuntimeFunction));
            IntPtr page = VirtualAlloc(IntPtr.Zero, (IntPtr)total, MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE);
            if (page == IntPtr.Zero) { Log("float returns: VirtualAlloc failed"); return; }

            // UNWIND_INFO v1, prolog 4 bytes, one code: at offset 4,
            // UWOP_ALLOC_SMALL of (4+1)*8 = 28h.
            Marshal.Copy(new byte[] { 0x01, 0x04, 0x01, 0x00, 0x04, 0x42, 0x00, 0x00 }, 0,
                         Offset(page, unwindAt), 8);

            int done = 0;
            for (int i = 0; i < targets.Count; i++)
            {
                string name = targets[i].Key;
                IntPtr slot = DecodeStub(GetProcAddress(module, name), name);
                if (slot == IntPtr.Zero) continue;
                IntPtr was = Marshal.ReadIntPtr(slot);

                IntPtr stub = Offset(page, i * MirrorSize);
                IntPtr cell = Offset(stub, MirrorCell);
                Marshal.WriteIntPtr(cell, was);

                var code = new List<byte>();
                code.AddRange(new byte[] { 0x48, 0x83, 0xEC, 0x28 });
                code.AddRange(new byte[] { 0x48, 0xB8 }); code.AddRange(BitConverter.GetBytes(cell.ToInt64()));
                code.AddRange(new byte[] { 0xFF, 0x10 });
                code.AddRange(new byte[] { 0x49, 0xBB }); code.AddRange(BitConverter.GetBytes(slot.ToInt64()));
                code.AddRange(new byte[] { 0x4D, 0x8B, 0x13 });
                code.AddRange(new byte[] { 0x48, 0xB9 }); code.AddRange(BitConverter.GetBytes(stub.ToInt64()));
                code.AddRange(new byte[] { 0x4C, 0x39, 0xD1 });
                code.AddRange(new byte[] { 0x74, 0x10 });
                code.AddRange(new byte[] { 0x48, 0xB8 }); code.AddRange(BitConverter.GetBytes(cell.ToInt64()));
                code.AddRange(new byte[] { 0x4C, 0x89, 0x10 });
                code.AddRange(new byte[] { 0x49, 0x89, 0x0B });
                code.AddRange(targets[i].Value ? new byte[] { 0xF2, 0x0F, 0x2C, 0xC0 }
                                               : new byte[] { 0xF3, 0x0F, 0x2C, 0xC0 });
                code.AddRange(new byte[] { 0x48, 0x83, 0xC4, 0x28, 0xC3 });
                if (code.Count > MirrorCell) { Log(name + ": stub overflows its cell"); continue; }
                Marshal.Copy(code.ToArray(), 0, stub, code.Count);

                var rf = new RuntimeFunction { Begin = (uint)(i * MirrorSize),
                                               End = (uint)(i * MirrorSize + code.Count),
                                               Unwind = (uint)unwindAt };
                Marshal.StructureToPtr(rf, Offset(page, tableAt + i * Marshal.SizeOf(typeof(RuntimeFunction))), false);

                uint old;
                bool reprotected = VirtualProtect(slot, (IntPtr)IntPtr.Size, PAGE_READWRITE, out old);
                Marshal.WriteIntPtr(slot, stub);
                if (reprotected) VirtualProtect(slot, (IntPtr)IntPtr.Size, old, out old);
                if (Marshal.ReadIntPtr(slot) != stub) { Log(name + ": slot write did not stick"); continue; }
                done++;
            }
            FlushInstructionCache(GetCurrentProcess(), page, (IntPtr)total);
            bool unwinds = RtlAddFunctionTable(Offset(page, tableAt), (uint)targets.Count, (ulong)page.ToInt64());

            var names = new List<string>();
            foreach (var t in targets) names.Add(t.Key);
            Log("float returns mirrored into eax: " + done + "/" + targets.Count
                + (unwinds ? "" : " (no unwind table)") + " -- " + string.Join(",", names.ToArray()));
        }

        static IntPtr Offset(IntPtr p, int by) { return new IntPtr(p.ToInt64() + by); }

        static Type FindGameType()
        {
            foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
            {
                if (a.GetName().Name != GameAssembly) continue;
                Type t = a.GetType(GameType, false);
                if (t != null) return t;
            }
            return null;
        }

        static bool Hook(IntPtr module, Type game, string name)
        {
            MethodInfo mi = game.GetMethod(name, BindingFlags.Public | BindingFlags.NonPublic
                                                 | BindingFlags.Static);
            if (mi == null) { Log(name + ": no such managed method"); return false; }

            ParameterInfo[] ps = mi.GetParameters();
            if (ps.Length != 1 || !ps[0].ParameterType.IsByRef
                || !ps[0].ParameterType.GetElementType().IsArray)
            {
                Log(name + ": unexpected signature, leaving it alone");
                return false;
            }
            Type elem = ps[0].ParameterType.GetElementType().GetElementType();

            IntPtr stub = GetProcAddress(module, name);
            if (stub == IntPtr.Zero) { Log(name + ": not exported"); return false; }

            IntPtr slot = DecodeStub(stub, name);
            if (slot == IntPtr.Zero) return false;

            var poller = new Poller(name, mi, elem);
            GetListFn fn = poller.Call;
            IntPtr thunk = Marshal.GetFunctionPointerForDelegate(fn);
            Rooted.Add(fn);
            Rooted.Add(poller);

            IntPtr was = Marshal.ReadIntPtr(slot);

            uint old;
            bool reprotected = VirtualProtect(slot, (IntPtr)IntPtr.Size, PAGE_READWRITE, out old);
            Marshal.WriteIntPtr(slot, thunk);
            if (reprotected) VirtualProtect(slot, (IntPtr)IntPtr.Size, old, out old);

            if (Marshal.ReadIntPtr(slot) != thunk) { Log(name + ": slot write did not stick"); return false; }

            Log(name + ": slot 0x" + slot.ToString("x16") + " 0x" + was.ToString("x16")
                + " -> 0x" + thunk.ToString("x16") + "  (element " + elem.FullName
                + ", " + Marshal.SizeOf(elem) + " bytes native)");
            return true;
        }

        /// Read the vtable-fixup slot address out of an export's mov/jmp stub.
        static IntPtr DecodeStub(IntPtr stub, string name)
        {
            var bytes = new byte[12];
            Marshal.Copy(stub, bytes, 0, bytes.Length);

            bool shaped = bytes[0] == StubPrefix[0] && bytes[1] == StubPrefix[1]
                       && bytes[10] == StubSuffix[0] && bytes[11] == StubSuffix[1];
            if (!shaped)
            {
                Log(name + ": export stub is not mov rax,[abs64]/jmp rax -- "
                    + BitConverter.ToString(bytes) + "; not hooked");
                return IntPtr.Zero;
            }
            return (IntPtr)BitConverter.ToInt64(bytes, StubSlotOffset);
        }

        /// Call one of the four managed pollers, collecting the elements it
        /// wrote into its byref argument.
        static List<object> Invoke(string name, MethodInfo method)
        {
            var args = new object[] { null };
            object ret = method.Invoke(null, args);
            var list = new List<object>();
            Array a = args[0] as Array;
            if (a != null) foreach (object o in a) list.Add(o);
            if (ret is int && (int)ret != list.Count)
                Log(name + ": returned " + (int)ret + " but the array holds " + list.Count);
            return list;
        }

        sealed class Poller
        {
            readonly string name;
            readonly MethodInfo method;
            readonly int esize;
            bool firstCall = true;

            internal Poller(string name, MethodInfo method, Type elem)
            {
                this.name = name;
                this.method = method;
                this.esize = Marshal.SizeOf(elem);
            }

            internal int Call(IntPtr ppDevices)
            {
                try { return CallCore(ppDevices); }
                catch (Exception e)
                {
                    Log(name + ": " + e.GetType().Name + ": " + e.Message);
                    if (ppDevices != IntPtr.Zero) Marshal.WriteIntPtr(ppDevices, IntPtr.Zero);
                    return 0;
                }
            }

            int CallCore(IntPtr ppDevices)
            {
                if (firstCall)
                {
                    firstCall = false;
                    // For an out parameter the CLR never reads what the caller
                    // put there, so this is only evidence about the contract:
                    // a plausible pointer would mean a caller-owned buffer.
                    Log(name + ": first call, ppDevices=0x" + ppDevices.ToString("x16")
                        + " *ppDevices=0x" + (ppDevices == IntPtr.Zero ? 0L
                            : Marshal.ReadIntPtr(ppDevices).ToInt64()).ToString("x16"));
                }

                // A direct managed call: the signature mono cannot marshal is
                // not marshalled at all on this side.
                List<object> devices = Invoke(name, method);

                int count = devices.Count;

                // Mirror the CLR's out-direction: a fresh CoTaskMemAlloc block,
                // its address stored through the pointer, the count returned.
                // Never null, so a caller that dereferences before checking the
                // count reads zeroed memory instead of faulting.
                int bytes = esize * Math.Max(count, 1);
                IntPtr block = Marshal.AllocCoTaskMem(bytes);
                for (int i = 0; i < bytes; i++) Marshal.WriteByte(block, i, 0);
                for (int i = 0; i < count; i++)
                    Marshal.StructureToPtr(devices[i],
                                           new IntPtr(block.ToInt64() + (long)i * esize), false);

                if (ppDevices != IntPtr.Zero) Marshal.WriteIntPtr(ppDevices, block);
                if (count > 0) Log(name + " -> " + count + " device(s) at 0x" + block.ToString("x16"));
                return count;
            }
        }

        /// MYWHOOSH_SHIM_LOG holds a Unix path -- the launcher that sets it is a
        /// shell script, and it names the same file for the helper, ../bleshim
        /// and this.  Inside the prefix that is not a path at all: wine-mono
        /// reads it as a Windows one, the open throws, and every line here ends
        /// up on stderr instead, which is a pipe nobody is reading.  Wine maps
        /// the Linux root at Z:, so try that spelling first and keep the
        /// original as the fallback for a run on the host.  ../bleshim's
        /// Backend.OpenLog does the same thing for the same reason.
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
                        new FileStream(candidate, FileMode.Append, FileAccess.Write,
                                       FileShare.ReadWrite)) { AutoFlush = true });
                }
                catch { }
            }
            return Console.Error;
        }

        static void Log(string msg)
        {
            string line = "[" + DateTime.Now.ToString("HH:mm:ss.fff", CultureInfo.InvariantCulture)
                        + " exportshim] " + msg;
            try
            {
                if (log == null) log = OpenLog();
                log.WriteLine(line);
            }
            catch { }
            if (log != Console.Error) { try { Console.Error.WriteLine(line); } catch { } }
        }
    }
}
