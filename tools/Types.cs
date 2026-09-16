using System;using System.Linq;using System.Reflection;
class T{static void Main(string[] a){
 AppDomain.CurrentDomain.AssemblyResolve+=(s,e)=>{var n=new AssemblyName(e.Name).Name;
  var c=System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory,n+".dll");
  return System.IO.File.Exists(c)?Assembly.LoadFrom(c):null;};
 var asm=Assembly.LoadFrom(a[0]);Type[] ts;
 try{ts=asm.GetTypes();}catch(ReflectionTypeLoadException e){ts=e.Types.Where(x=>x!=null).ToArray();}
 foreach(var t in ts.OrderBy(x=>x.FullName)) Console.WriteLine(t.FullName);}}
