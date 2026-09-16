// A stand-in for System.Runtime.WindowsRuntime, the assembly that supplies the
// `await` bridge from a WinRT IAsyncOperation to a Task.  WindowsConnectivity.dll
// references exactly one member of it, `WindowsRuntimeSystemExtensions.GetAwaiter`,
// and wine-mono does not ship the assembly at all.
//
// Companion to Windows.cs; see its header for why these are stubs.

using System.Runtime.CompilerServices;
using Windows.Foundation;

[assembly: System.Reflection.AssemblyVersion("4.0.0.0")]

namespace System
{
    public static class WindowsRuntimeSystemExtensions
    {
        /// Our IAsyncOperation is always already completed, so the awaiter it
        /// hands back never suspends: the async state machine in the game's DLL
        /// runs straight through instead of parking on a callback that would
        /// never fire.
        public static TaskAwaiter<TResult> GetAwaiter<TResult>(this IAsyncOperation<TResult> source)
        {
            return source.Task.GetAwaiter();
        }
    }
}
