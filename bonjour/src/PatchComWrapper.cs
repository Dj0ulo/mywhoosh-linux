using System;
using System.IO;
using System.Reflection;
using Mono.Cecil;
using Mono.Cecil.Cil;

public static class PatchComWrapper
{
    public static int Main(string[] arguments)
    {
        try
        {
            if (arguments.Length != 3)
                throw new ArgumentException("Supply original System.Core.dll, BonjourCompat.dll, and a distinct output path.");
            Patch(arguments[0], arguments[1], arguments[2]);
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Bonjour compatibility: " + error.Message);
            return 1;
        }
    }

    public static void Patch(string input, string helper, string output)
    {
        if (String.Equals(Path.GetFullPath(input), Path.GetFullPath(output), StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("The original framework must not be overwritten.");
        using (var framework = AssemblyDefinition.ReadAssembly(input))
        using (var compatibility = AssemblyDefinition.ReadAssembly(helper))
        {
            var module = framework.MainModule;
            var wrapper = module.GetType("System.Runtime.InteropServices.ComAwareEventInfo");
            var replacement = compatibility.MainModule.GetType("BonjourCompatEventInfo");
            if (framework.Name.Name != "System.Core" || wrapper == null || replacement == null ||
                wrapper.BaseType.FullName != typeof(EventInfo).FullName || wrapper.HasFields)
                throw new NotSupportedException("Unrecognized or already-patched System.Core; no output written.");

            MethodDefinition replacementConstructor = null;
            foreach (var method in replacement.Methods)
                if (method.IsConstructor && !method.IsStatic && method.Parameters.Count == 2 &&
                    method.Parameters[0].ParameterType.FullName == "System.Type" &&
                    method.Parameters[1].ParameterType.FullName == "System.String")
                    replacementConstructor = method;
            if (replacementConstructor == null)
                throw new NotSupportedException("The Bonjour event wrapper constructor is missing.");

            MethodReference baseConstructor = null;
            var forwards = new System.Collections.Generic.Dictionary<MethodDefinition, MethodInfo>();
            foreach (var method in wrapper.Methods)
            {
                if (!method.HasBody || method.IsStatic || method.Body.HasExceptionHandlers)
                    throw new NotSupportedException("Unexpected framework method: " + method.FullName);
                var instructions = method.Body.Instructions;
                int expectedCount = method.IsConstructor ? 4 : 2;
                if (instructions.Count != expectedCount ||
                    instructions[expectedCount - 1].OpCode != OpCodes.Throw ||
                    instructions[expectedCount - 2].OpCode != OpCodes.Newobj ||
                    ((MethodReference)instructions[expectedCount - 2].Operand).DeclaringType.FullName != "System.NotImplementedException")
                    throw new NotSupportedException("Not a known Mono stub: " + method.FullName);
                if (method.IsConstructor)
                {
                    if (baseConstructor != null || method.Parameters.Count != 2 ||
                        method.Parameters[0].ParameterType.FullName != "System.Type" ||
                        method.Parameters[1].ParameterType.FullName != "System.String" ||
                        instructions[0].OpCode != OpCodes.Ldarg_0 || instructions[1].OpCode != OpCodes.Call)
                        throw new NotSupportedException("Unexpected framework constructor.");
                    baseConstructor = (MethodReference)instructions[1].Operand;
                    if (baseConstructor.DeclaringType.FullName != typeof(EventInfo).FullName ||
                        baseConstructor.Name != ".ctor" || baseConstructor.Parameters.Count != 0)
                        throw new NotSupportedException("Unexpected EventInfo base constructor.");
                }
                else
                {
                    var parameterTypes = new Type[method.Parameters.Count];
                    for (int index = 0; index < parameterTypes.Length; index++)
                        parameterTypes[index] = Type.GetType(method.Parameters[index].ParameterType.FullName, true);
                    var forwarded = typeof(EventInfo).GetMethod(method.Name, parameterTypes);
                    if (forwarded == null || forwarded.ReturnType.FullName != method.ReturnType.FullName)
                        throw new NotSupportedException("Unsupported EventInfo member: " + method.FullName);
                    forwards.Add(method, forwarded);
                }
            }
            if (baseConstructor == null || forwards.Count == 0)
                throw new NotSupportedException("Incomplete framework wrapper.");

            var field = new FieldDefinition("bonjourCompatibilityEvent", Mono.Cecil.FieldAttributes.Private, wrapper.BaseType);
            wrapper.Fields.Add(field);
            foreach (var method in wrapper.Methods)
            {
                method.Body = new Mono.Cecil.Cil.MethodBody(method);
                var processor = method.Body.GetILProcessor();
                processor.Emit(OpCodes.Ldarg_0);
                if (method.IsConstructor)
                {
                    processor.Emit(OpCodes.Call, baseConstructor);
                    processor.Emit(OpCodes.Ldarg_0);
                    processor.Emit(OpCodes.Ldarg_1);
                    processor.Emit(OpCodes.Ldarg_2);
                    processor.Emit(OpCodes.Newobj, module.ImportReference(replacementConstructor));
                    processor.Emit(OpCodes.Stfld, field);
                }
                else
                {
                    processor.Emit(OpCodes.Ldfld, field);
                    foreach (var parameter in method.Parameters)
                        processor.Emit(OpCodes.Ldarg, parameter);
                    processor.Emit(OpCodes.Callvirt, module.ImportReference(forwards[method]));
                }
                processor.Emit(OpCodes.Ret);
            }
            framework.Write(output);
            Console.WriteLine("Wrote isolated compatibility assembly: " + output);
        }
    }
}