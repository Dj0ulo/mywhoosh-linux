using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.ServiceProcess;

public static class SetupBonjour
{
    public sealed class ManagedEvents
    {
        public event EventHandler Changed;
        public void Raise() { if (Changed != null) Changed(this, EventArgs.Empty); }
    }

    [STAThread]
    public static int Main(string[] arguments)
    {
        try
        {
            if (Type.GetType("Mono.Runtime") == null)
                throw new NotSupportedException("Wine Mono is required; Microsoft .NET does not support the Bluetooth shim.");
            if (arguments.Length == 1 && arguments[0] == "--verify")
            {
                Verify();
                return 0;
            }
            if (arguments.Length > 1 || (arguments.Length == 1 && arguments[0] != "--prepare-only"))
                throw new ArgumentException("Usage: SetupBonjour.exe [--prepare-only]");
            Prepare();
            if (arguments.Length == 0)
                CheckBonjour();
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("[bonjourcompat] " + error);
            return 1;
        }
    }

    private static string Hash(string path)
    {
        using (var algorithm = SHA256.Create())
        using (var stream = File.OpenRead(path))
            return BitConverter.ToString(algorithm.ComputeHash(stream)).Replace("-", "");
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static void Run(string program, string arguments, string directory, string cache)
    {
        var start = new ProcessStartInfo(program, arguments) {
            UseShellExecute = false,
            WorkingDirectory = directory
        };
        start.EnvironmentVariables["MONO_GAC_PREFIX"] = cache ?? "";
        start.EnvironmentVariables["MONO_PATH"] = directory;
        using (var process = Process.Start(start))
        {
            if (!process.WaitForExit(60000))
            {
                process.Kill();
                process.WaitForExit();
                throw new System.TimeoutException("Timed out running " + program);
            }
            if (process.ExitCode != 0)
                throw new InvalidOperationException(program + " exited with " + process.ExitCode);
        }
    }

    private static void Prepare()
    {
        string directory = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string runtime = Path.GetDirectoryName(typeof(object).Assembly.Location);
        string framework = Path.Combine(runtime, "System.Core.dll");
        string compiler = Path.Combine(runtime, "mcs.exe");
        string helper = Path.Combine(directory, "BonjourCompat.dll");
        string source = Path.Combine(directory, "PatchComWrapper.cs");
        string cecilRoot = Path.GetFullPath(Path.Combine(runtime, "..", "gac", "Mono.Cecil"));
        if (!File.Exists(compiler) || !Directory.Exists(cecilRoot))
            throw new NotSupportedException("This Wine Mono installation must include mcs.exe and Mono.Cecil 0.11.x.");
        string cecil = null;
        Version cecilVersion = null;
        foreach (string candidate in Directory.GetFiles(cecilRoot, "Mono.Cecil.dll", SearchOption.AllDirectories))
        {
            var version = AssemblyName.GetAssemblyName(candidate).Version;
            if (version.Major == 0 && version.Minor == 11 && (cecilVersion == null || version > cecilVersion))
            {
                cecil = candidate;
                cecilVersion = version;
            }
        }
        if (cecil == null)
            throw new NotSupportedException("Mono.Cecil 0.11.x was not found in the selected Wine Mono runtime.");
        var identity = AssemblyName.GetAssemblyName(framework);
        if (identity.Name != "System.Core" || identity.Version.ToString() != "4.0.0.0" ||
            BitConverter.ToString(identity.GetPublicKeyToken()) != "B7-7A-5C-56-19-34-E0-89")
            throw new NotSupportedException("Unsupported System.Core identity; no framework was replaced.");

        string cache = Path.Combine(directory, "bonjour");
        Directory.CreateDirectory(cache);
        using (var setupLock = new FileStream(Path.Combine(cache, "setup.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
        {
            string relative = Path.Combine("lib", "mono", "gac", "System.Core", "4.0.0.0__b77a5c561934e089", "System.Core.dll");
            string output = Path.Combine(cache, relative);
            string stamp = Path.Combine(cache, "inputs.sha256");
            string inputs = String.Join("\n", new[] { Hash(framework), Hash(helper), Hash(source),
                Hash(compiler), Hash(cecil), Hash(Assembly.GetExecutingAssembly().Location) });
            if (File.Exists(output) && File.Exists(stamp) && File.ReadAllText(stamp) == inputs + "\n" + Hash(output))
            {
                Console.WriteLine("[bonjourcompat] framework copy matches this Wine Mono runtime");
                return;
            }
            string stage = Path.Combine(cache, ".build-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(stage);
            try
            {
                string patcher = Path.Combine(stage, "PatchComWrapper.exe");
                Run(compiler, "-out:" + Quote(patcher) + " -r:" + Quote(cecil) + " " + Quote(source), directory, null);
                string stagedOutput = Path.Combine(stage, relative);
                Directory.CreateDirectory(Path.GetDirectoryName(stagedOutput));
                Run(patcher, Quote(framework) + " " + Quote(helper) + " " + Quote(stagedOutput), directory, null);
                Run(Assembly.GetExecutingAssembly().Location, "--verify", directory, stage);
                Directory.CreateDirectory(Path.GetDirectoryName(output));
                if (File.Exists(output))
                    File.Replace(stagedOutput, output, null);
                else
                    File.Move(stagedOutput, output);
                File.WriteAllText(stamp, inputs + "\n" + Hash(output));
                Console.WriteLine("[bonjourcompat] prepared " + output);
            }
            finally
            {
                Directory.Delete(stage, true);
            }
        }
    }

    private static void Verify()
    {
        var target = new ManagedEvents();
        var eventInfo = new ComAwareEventInfo(typeof(ManagedEvents), "Changed");
        int calls = 0;
        EventHandler handler = delegate { calls++; };
        eventInfo.AddEventHandler(target, handler);
        target.Raise();
        eventInfo.RemoveEventHandler(target, handler);
        target.Raise();
        if (calls != 1 || eventInfo.Name != "Changed" || eventInfo.EventHandlerType != typeof(EventHandler))
            throw new InvalidOperationException("The framework wrapper failed its managed event smoke test.");
        Console.WriteLine("[bonjourcompat] verified framework wrapper: " + typeof(ComAwareEventInfo).Assembly.Location);
    }

    private static void CheckBonjour()
    {
        using (var service = new ServiceController("Bonjour Service"))
        {
            if (service.Status != ServiceControllerStatus.Running)
            {
                service.Start();
                service.WaitForStatus(ServiceControllerStatus.Running, TimeSpan.FromSeconds(15));
            }
        }
        object manager = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("BEEB932A-8D4A-4619-AEFE-A836F988B221"), true));
        try
        {
            using (var bridge = new BonjourBridge(manager)) { }
        }
        finally
        {
            Marshal.ReleaseComObject(manager);
        }
        Console.WriteLine("[bonjourcompat] Bonjour service and COM event subscription are ready");
    }
}