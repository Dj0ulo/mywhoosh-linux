// Rewrites wine-mono's System.Core.dll so that
// System.Runtime.InteropServices.ComAwareEventInfo -- every member of which is
// a NotImplementedException stub -- becomes a thin wrapper over the real
// implementation in MyWhoosh.ComEventShim.dll.
//
//   usage: mono PatchSystemCore.exe <System.Core.dll> <shim.dll> <out.dll>
//
// The class gains one field, `__inner`, holding the EventInfo it was
// constructed for; the reflection members forward to it and Add/RemoveEventHandler
// forward to the shim.  Nothing else in the assembly is touched.

using System;
using System.IO;
using System.Linq;
using System.Reflection;
using Mono.Cecil;
using Mono.Cecil.Cil;

class PatchSystemCore
{
    const string TargetType = "System.Runtime.InteropServices.ComAwareEventInfo";
    const string ShimType   = "MyWhoosh.ComEventShim.ComEvents";

    static ModuleDefinition module;
    static FieldDefinition inner;

    static int Main(string[] args)
    {
        if (args.Length != 3)
        {
            Console.Error.WriteLine("usage: PatchSystemCore.exe <System.Core.dll> <shim.dll> <out.dll>");
            return 2;
        }
        string input = args[0], shimPath = args[1], output = args[2];

        var resolver = new DefaultAssemblyResolver();
        resolver.AddSearchDirectory(Path.GetDirectoryName(Path.GetFullPath(input)));
        resolver.AddSearchDirectory(Path.GetDirectoryName(Path.GetFullPath(shimPath)));

        module = ModuleDefinition.ReadModule(input, new ReaderParameters { AssemblyResolver = resolver });
        ModuleDefinition shimModule = ModuleDefinition.ReadModule(shimPath);

        TypeDefinition cae = module.GetType(TargetType);
        if (cae == null) { Console.Error.WriteLine("no " + TargetType + " in " + input); return 1; }
        if (cae.Fields.Any(f => f.Name == "__inner"))
        { Console.Error.WriteLine("already patched: " + input); return 1; }

        TypeDefinition shim = shimModule.GetType(ShimType);
        if (shim == null) { Console.Error.WriteLine("no " + ShimType + " in " + shimPath); return 1; }

        TypeReference eventInfo = module.ImportReference(typeof(EventInfo));
        inner = new FieldDefinition("__inner", Mono.Cecil.FieldAttributes.Private, eventInfo);
        cae.Fields.Add(inner);

        // ctor(Type, string): store the EventInfo the shim resolves for us.
        Rewrite(cae, ".ctor", il => {
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Call, module.ImportReference(
                typeof(EventInfo).GetConstructor(BindingFlags.Instance | BindingFlags.NonPublic,
                                                 null, Type.EmptyTypes, null)));
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Ldarg_2);
            il.Emit(OpCodes.Call, Shim(shim, "GetEventInfo"));
            il.Emit(OpCodes.Stfld, inner);
            il.Emit(OpCodes.Ret);
        });

        // The event plumbing itself.
        ForwardToShim(cae, "AddEventHandler",    Shim(shim, "AddEventHandler"));
        ForwardToShim(cae, "RemoveEventHandler", Shim(shim, "RemoveEventHandler"));

        // Everything else is just EventInfo/MemberInfo reflection over __inner.
        ForwardToInner(cae, "get_Name",           Member("Name"));
        ForwardToInner(cae, "get_DeclaringType",  Member("DeclaringType"));
        ForwardToInner(cae, "get_ReflectedType",  Member("ReflectedType"));
        ForwardToInner(cae, "get_Attributes",     Member("Attributes"));
        ForwardToInner(cae, "GetAddMethod",       Method("GetAddMethod",    typeof(bool)));
        ForwardToInner(cae, "GetRemoveMethod",    Method("GetRemoveMethod", typeof(bool)));
        ForwardToInner(cae, "GetRaiseMethod",     Method("GetRaiseMethod",  typeof(bool)));
        ForwardToInner(cae, "GetOtherMethods",    Method("GetOtherMethods", typeof(bool)));
        ForwardToInner(cae, "IsDefined",          Method("IsDefined", typeof(Type), typeof(bool)));
        ForwardToInner(cae, "GetCustomAttributes", Method("GetCustomAttributes", typeof(bool)),
                       typeof(bool));
        ForwardToInner(cae, "GetCustomAttributes", Method("GetCustomAttributes", typeof(Type), typeof(bool)),
                       typeof(Type), typeof(bool));

        module.Write(output);
        Console.WriteLine("patched " + TargetType + " -> " + output);
        return 0;
    }

    static MethodReference Shim(TypeDefinition shim, string name)
    {
        MethodDefinition m = shim.Methods.Single(x => x.Name == name);
        return module.ImportReference(m);
    }

    static MethodReference Member(string property)
    {
        return module.ImportReference(typeof(EventInfo).GetProperty(property).GetGetMethod());
    }

    static MethodReference Method(string name, params Type[] parameters)
    {
        MethodInfo m = typeof(EventInfo).GetMethod(name, parameters);
        return m == null ? null : module.ImportReference(m);
    }

    // this.__inner.<target>(args...)
    static void ForwardToInner(TypeDefinition type, string name, MethodReference target,
                              params Type[] signature)
    {
        if (target == null) return;              // member absent from this mono's EventInfo
        Rewrite(type, name, il => {
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Ldfld, inner);
            for (int i = 0; i < target.Parameters.Count; i++) il.Emit(OpCodes.Ldarg, i + 1);
            il.Emit(OpCodes.Callvirt, target);
            il.Emit(OpCodes.Ret);
        }, signature);
    }

    // ComEvents.<target>(this.__inner, target, handler)
    static void ForwardToShim(TypeDefinition type, string name, MethodReference target)
    {
        Rewrite(type, name, il => {
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Ldfld, inner);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Ldarg_2);
            il.Emit(OpCodes.Call, target);
            il.Emit(OpCodes.Ret);
        });
    }

    static void Rewrite(TypeDefinition type, string name, Action<ILProcessor> emit,
                        params Type[] signature)
    {
        MethodDefinition[] candidates = type.Methods.Where(m => m.Name == name).ToArray();
        MethodDefinition method = signature.Length == 0
            ? candidates.SingleOrDefault()
            : candidates.SingleOrDefault(m => m.Parameters.Count == signature.Length
                                  && m.Parameters.Select(p => p.ParameterType.FullName)
                                       .SequenceEqual(signature.Select(t => t.FullName)));
        // Members the stub inherits rather than overrides need no rewriting.
        if (method == null) { Console.WriteLine("  skipped " + type.Name + "::" + name + " (not overridden)"); return; }

        method.Body = new Mono.Cecil.Cil.MethodBody(method);
        emit(method.Body.GetILProcessor());
        method.Body.MaxStackSize = 8;
        Console.WriteLine("  rewrote " + method.FullName);
    }
}
