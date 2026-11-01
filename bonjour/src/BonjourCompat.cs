using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

[ComVisible(true)]
[Guid("21AE8D7F-D5FE-45CF-B632-CFA2C2C6B498")]
[InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
public interface IBonjourEvents
{
    [DispId(3)]
    void ServiceFound([MarshalAs(UnmanagedType.Interface)] object service, int flags,
        uint interfaceIndex, string name, string registrationType, string domain);

    [DispId(4)]
    void ServiceLost([MarshalAs(UnmanagedType.Interface)] object service, int flags,
        uint interfaceIndex, string name, string registrationType, string domain);

    [DispId(5)]
    void ServiceResolved([MarshalAs(UnmanagedType.Interface)] object service, int flags,
        uint interfaceIndex, string fullName, string hostName, ushort port,
        [MarshalAs(UnmanagedType.Interface)] object textRecord);

    [DispId(11)]
    void OperationFailed([MarshalAs(UnmanagedType.Interface)] object service, int error);
}

public sealed class BonjourCompatEventInfo : EventInfo
{
    private static readonly Dictionary<object, BonjourBridge> Bridges = new Dictionary<object, BonjourBridge>();
    private readonly EventInfo eventInfo;
    private readonly Type eventInterface;

    public BonjourCompatEventInfo(Type type, string eventName)
    {
        if (type == null)
            throw new ArgumentNullException("type");
        if (eventName == null)
            throw new ArgumentNullException("eventName");
        eventInterface = type;
        eventInfo = type.GetEvent(eventName);
        if (eventInfo == null)
            throw new ArgumentException("Event not found: " + eventName);
    }

    private void ValidateBonjour()
    {
        var metadata = (ComEventInterfaceAttribute)Attribute.GetCustomAttribute(eventInterface, typeof(ComEventInterfaceAttribute));
        if (metadata == null || metadata.SourceInterface.GUID != typeof(IBonjourEvents).GUID)
            throw new NotSupportedException("Only the verified Bonjour event interface is supported.");
        if (Name != "ServiceFound" && Name != "ServiceLost" && Name != "ServiceResolved" && Name != "OperationFailed")
            throw new NotSupportedException("Unsupported Bonjour event: " + Name);
    }

    public override void AddEventHandler(object target, Delegate handler)
    {
        if (target == null)
            throw new ArgumentNullException("target");
        if (!Marshal.IsComObject(target))
        {
            eventInfo.AddEventHandler(target, handler);
            return;
        }
        ValidateBonjour();
        if (handler == null || !eventInfo.EventHandlerType.IsInstanceOfType(handler))
            throw new ArgumentException("Handler has the wrong event delegate type.");
        lock (Bridges)
        {
            BonjourBridge bridge;
            if (!Bridges.TryGetValue(target, out bridge))
            {
                bridge = new BonjourBridge(target);
                Bridges.Add(target, bridge);
            }
            bridge.Add(Name, handler);
        }
    }

    public override void RemoveEventHandler(object target, Delegate handler)
    {
        if (target == null)
            throw new ArgumentNullException("target");
        if (!Marshal.IsComObject(target))
        {
            eventInfo.RemoveEventHandler(target, handler);
            return;
        }
        ValidateBonjour();
        lock (Bridges)
        {
            BonjourBridge bridge;
            if (Bridges.TryGetValue(target, out bridge) && bridge.Remove(Name, handler))
            {
                bridge.Dispose();
                Bridges.Remove(target);
            }
        }
    }

    public override EventAttributes Attributes { get { return eventInfo.Attributes; } }
    public override Type DeclaringType { get { return eventInfo.DeclaringType; } }
    public override string Name { get { return eventInfo.Name; } }
    public override Type ReflectedType { get { return eventInfo.ReflectedType; } }
    public override MethodInfo GetAddMethod(bool nonPublic) { return eventInfo.GetAddMethod(nonPublic); }
    public override MethodInfo GetRaiseMethod(bool nonPublic) { return eventInfo.GetRaiseMethod(nonPublic); }
    public override MethodInfo GetRemoveMethod(bool nonPublic) { return eventInfo.GetRemoveMethod(nonPublic); }
    public override object[] GetCustomAttributes(Type attributeType, bool inherit) { return eventInfo.GetCustomAttributes(attributeType, inherit); }
    public override object[] GetCustomAttributes(bool inherit) { return eventInfo.GetCustomAttributes(inherit); }
    public override bool IsDefined(Type attributeType, bool inherit) { return eventInfo.IsDefined(attributeType, inherit); }
}

[ComVisible(true)]
[ClassInterface(ClassInterfaceType.None)]
public sealed class BonjourBridge : IBonjourEvents, IDisposable
{
    private readonly Dictionary<string, Delegate> handlers = new Dictionary<string, Delegate>();
    private readonly IConnectionPoint point;
    private int cookie;

    public BonjourBridge(object target)
    {
        var interfaceId = typeof(IBonjourEvents).GUID;
        ((IConnectionPointContainer)target).FindConnectionPoint(ref interfaceId, out point);
        try
        {
            point.Advise(this, out cookie);
        }
        catch
        {
            Marshal.ReleaseComObject(point);
            throw;
        }
        Console.Error.WriteLine("[bonjourcompat] subscribed Bonjour COM events");
    }

    public void Add(string name, Delegate handler)
    {
        lock (handlers)
        {
            Delegate existing;
            handlers.TryGetValue(name, out existing);
            handlers[name] = Delegate.Combine(existing, handler);
        }
    }

    public bool Remove(string name, Delegate handler)
    {
        lock (handlers)
        {
            Delegate existing;
            if (handlers.TryGetValue(name, out existing))
            {
                var remaining = Delegate.Remove(existing, handler);
                if (remaining == null)
                    handlers.Remove(name);
                else
                    handlers[name] = remaining;
            }
            return handlers.Count == 0;
        }
    }

    private void Dispatch(string name, params object[] arguments)
    {
        Delegate handler;
        lock (handlers)
            if (!handlers.TryGetValue(name, out handler))
                return;
        foreach (var subscriber in handler.GetInvocationList())
        {
            try
            {
                var parameters = subscriber.GetType().GetMethod("Invoke").GetParameters();
                var converted = (object[])arguments.Clone();
                for (int index = 0; index < converted.Length; index++)
                {
                    var parameterType = parameters[index].ParameterType;
                    if (parameterType.IsEnum)
                        converted[index] = Enum.ToObject(parameterType, converted[index]);
                    else if (converted[index] != null && parameterType.IsInterface && Marshal.IsComObject(converted[index]))
                    {
                        var pointer = Marshal.GetIUnknownForObject(converted[index]);
                        try { converted[index] = Marshal.GetTypedObjectForIUnknown(pointer, parameterType); }
                        finally { Marshal.Release(pointer); }
                    }
                }
                subscriber.DynamicInvoke(converted);
            }
            catch (Exception error)
            {
                Console.Error.WriteLine("[bonjourcompat] " + name + " callback failed: " + error);
            }
        }
    }

    public void ServiceFound(object service, int flags, uint interfaceIndex, string name, string registrationType, string domain)
    { Dispatch("ServiceFound", service, flags, interfaceIndex, name, registrationType, domain); }
    public void ServiceLost(object service, int flags, uint interfaceIndex, string name, string registrationType, string domain)
    { Dispatch("ServiceLost", service, flags, interfaceIndex, name, registrationType, domain); }
    public void ServiceResolved(object service, int flags, uint interfaceIndex, string fullName, string hostName, ushort port, object textRecord)
    { Dispatch("ServiceResolved", service, flags, interfaceIndex, fullName, hostName, port, textRecord); }
    public void OperationFailed(object service, int error)
    { Dispatch("OperationFailed", service, error); }

    public void Dispose()
    {
        if (cookie != 0)
        {
            point.Unadvise(cookie);
            cookie = 0;
            Marshal.ReleaseComObject(point);
        }
    }
}