// Every method reachable through the DLL's 98 unmanaged exports, with the
// marshalling shape of each parameter -- so blocker 4's blast radius is known.
using System;using System.Linq;using System.Reflection;using System.Runtime.InteropServices;
class AllSigs{
 static string M(ParameterInfo p){
  foreach(var a in p.GetCustomAttributes(false)){var m=a as MarshalAsAttribute;
   if(m!=null)return " MarshalAs("+m.Value+(m.SizeConst!=0?",Const="+m.SizeConst:"")+(m.SizeParamIndex!=0?",Idx="+m.SizeParamIndex:"")+")";}
  return "";}
 static void Main(string[] a){
  AppDomain.CurrentDomain.AssemblyResolve+=(s,e)=>{var n=new AssemblyName(e.Name).Name;
   var c=System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory,n+".dll");
   return System.IO.File.Exists(c)?Assembly.LoadFrom(c):null;};
  var asm=Assembly.LoadFrom(a[0]);Type[] ts;
  try{ts=asm.GetTypes();}catch(ReflectionTypeLoadException e){ts=e.Types.Where(x=>x!=null).ToArray();}
  var t=ts.First(x=>x.FullName=="FunctionsManager.MyWhoosh");
  foreach(var mi in t.GetMethods(BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Static|BindingFlags.DeclaredOnly).OrderBy(x=>x.Name)){
   var ps=mi.GetParameters();
   bool odd=ps.Any(p=>p.ParameterType.IsByRef||p.ParameterType.IsArray||p.ParameterType==typeof(string)||!p.ParameterType.IsPrimitive);
   Console.WriteLine((odd?"! ":"  ")+mi.ReturnType.Name+" "+mi.Name+"("+string.Join(", ",ps.Select(p=>p.ParameterType.Name+" "+p.Name+M(p)))+")");
  }
 }
}
