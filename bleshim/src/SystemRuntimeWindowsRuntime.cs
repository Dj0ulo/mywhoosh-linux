// The `await` bridge from a WinRT IAsyncOperation to a Task.
//
// WindowsConnectivity.dll references exactly one member of
// System.Runtime.WindowsRuntime -- WindowsRuntimeSystemExtensions.GetAwaiter --
// because that is what the C# compiler emits when UWP code awaits an
// IAsyncOperation.  wine-mono does not ship the assembly at all, so it is
// supplied here.
//
// Companion to Windows.cs; see its header for why the operations it returns are
// already finished by the time the game awaits them.

using System.Runtime.CompilerServices;
using Windows.Foundation;

[assembly: System.Reflection.AssemblyVersion("4.0.0.0")]

namespace System
{
    public static class WindowsRuntimeSystemExtensions
    {
        /// The awaiter of a Task that has already run to completion never
        /// suspends, so the game's async state machines run straight through on
        /// the thread that called them instead of parking on a continuation.
        public static TaskAwaiter<TResult> GetAwaiter<TResult>(this IAsyncOperation<TResult> source)
        {
            return source.Task.GetAwaiter();
        }
    }
}
