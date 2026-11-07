using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

namespace MastSatellite.Core
{
    /// <summary>
    /// A dedicated STA thread with its own Windows message loop. Vendor objects that want an
    /// apartment/dispatcher (LightField Automation, the SpectraSense COM server) are created and
    /// called only here. Calls are marshalled with a timeout so a hung vendor call cannot block
    /// the hub link or the status window.
    /// </summary>
    public sealed class StaWorker : IDisposable
    {
        private readonly Thread _thread;
        private readonly ManualResetEventSlim _ready = new ManualResetEventSlim(false);
        private Control _marshal;
        private ApplicationContext _appCtx;

        public StaWorker(string name)
        {
            _thread = new Thread(Run) { IsBackground = true, Name = name };
            _thread.SetApartmentState(ApartmentState.STA);
            _thread.Start();
            if (!_ready.Wait(TimeSpan.FromSeconds(10))) throw new InvalidOperationException("STA thread did not start");
        }

        public bool IsCurrentThread => Thread.CurrentThread == _thread;

        private void Run()
        {
            _marshal = new Control();
            _marshal.CreateControl();
            var _ = _marshal.Handle; // force the window handle on this thread
            _appCtx = new ApplicationContext();
            _ready.Set();
            try { Application.Run(_appCtx); }
            catch (Exception ex) { Log.Error("STA message loop crashed", ex); }
        }

        /// <summary>Runs f on the STA thread and waits up to timeout. Vendor exceptions are rethrown unwrapped.</summary>
        public T Invoke<T>(Func<T> f, TimeSpan timeout)
        {
            if (IsCurrentThread) return f();
            var done = new ManualResetEventSlim(false);
            T result = default(T);
            Exception error = null;
            _marshal.BeginInvoke((Action)(() =>
            {
                try { result = f(); }
                catch (Exception ex) { error = ex; }
                finally { done.Set(); }
            }));
            if (!done.Wait(timeout)) throw new TimeoutException($"vendor call did not return within {timeout.TotalSeconds:0} s");
            if (error != null)
            {
                if (error is TargetInvocationException tie && tie.InnerException != null) error = tie.InnerException;
                System.Runtime.ExceptionServices.ExceptionDispatchInfo.Capture(error).Throw();
            }
            return result;
        }

        public void Invoke(Action a, TimeSpan timeout)
        {
            Invoke<object>(() => { a(); return null; }, timeout);
        }

        public void Post(Action a)
        {
            try { _marshal.BeginInvoke(a); } catch (Exception ex) { Log.Error("STA post failed", ex); }
        }

        public void Dispose()
        {
            try { _marshal.BeginInvoke((Action)(() => _appCtx.ExitThread())); } catch { }
            _thread.Join(5000);
        }
    }

    /// <summary>
    /// Reflection over vendor interface types. Members are looked up on the public interface
    /// (vendor implementations are often non-public proxy classes, which defeats `dynamic`).
    /// </summary>
    public static class LateBound
    {
        private static IEnumerable<Type> Hierarchy(Type t)
        {
            yield return t;
            foreach (var i in t.GetInterfaces()) yield return i;
            for (var b = t.BaseType; b != null; b = b.BaseType) yield return b;
        }

        public static PropertyInfo Prop(Type t, string name)
        {
            foreach (var x in Hierarchy(t))
            {
                var p = x.GetProperty(name, BindingFlags.Public | BindingFlags.Instance);
                if (p != null) return p;
            }
            throw new MissingMemberException(t.FullName, name);
        }

        public static object Get(object target, Type iface, string name)
        {
            try { return Prop(iface, name).GetValue(target, null); }
            catch (TargetInvocationException e) when (e.InnerException != null) { throw e.InnerException; }
        }

        public static void Set(object target, Type iface, string name, object value)
        {
            try { Prop(iface, name).SetValue(target, value, null); }
            catch (TargetInvocationException e) when (e.InnerException != null) { throw e.InnerException; }
        }

        public static object Call(object target, Type iface, string name, params object[] args)
        {
            args = args ?? new object[0];
            MethodInfo best = null;
            foreach (var x in Hierarchy(iface))
            {
                foreach (var m in x.GetMethods(BindingFlags.Public | BindingFlags.Instance).Where(m => m.Name == name))
                {
                    var ps = m.GetParameters();
                    if (ps.Length != args.Length) continue;
                    bool ok = true;
                    for (int i = 0; i < ps.Length && ok; i++)
                    {
                        var pt = ps[i].ParameterType;
                        if (pt.IsByRef) pt = pt.GetElementType();
                        ok = args[i] == null ? !pt.IsValueType || Nullable.GetUnderlyingType(pt) != null : pt.IsInstanceOfType(args[i]);
                    }
                    if (ok) { best = m; break; }
                }
                if (best != null) break;
            }
            if (best == null) throw new MissingMethodException(iface.FullName, name + "/" + args.Length);
            try { return best.Invoke(target, args); }
            catch (TargetInvocationException e) when (e.InnerException != null) { throw e.InnerException; }
        }

        /// <summary>Subscribes handler(object sender, EventArgs e) to an event whose args type lives in a vendor assembly.</summary>
        public static Delegate Subscribe(object target, Type iface, string eventName, object handlerOwner, string handlerMethod)
        {
            EventInfo ev = null;
            foreach (var x in Hierarchy(iface))
            {
                ev = x.GetEvent(eventName, BindingFlags.Public | BindingFlags.Instance);
                if (ev != null) break;
            }
            if (ev == null) throw new MissingMemberException(iface.FullName, eventName);
            var mi = handlerOwner.GetType().GetMethod(handlerMethod, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
            var d = Delegate.CreateDelegate(ev.EventHandlerType, handlerOwner, mi);
            ev.AddEventHandler(target, d);
            return d;
        }

        public static void Unsubscribe(object target, Type iface, string eventName, Delegate d)
        {
            if (d == null) return;
            foreach (var x in Hierarchy(iface))
            {
                var ev = x.GetEvent(eventName, BindingFlags.Public | BindingFlags.Instance);
                if (ev == null) continue;
                try { ev.RemoveEventHandler(target, d); } catch { }
                return;
            }
        }

        /// <summary>Public instance properties of a value (struct or object) as a JSON-friendly dictionary.</summary>
        public static Dictionary<string, object> PropsToDict(object o)
        {
            var d = new Dictionary<string, object>(StringComparer.Ordinal);
            if (o == null) return d;
            foreach (var p in o.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
            {
                if (p.GetIndexParameters().Length > 0) continue;
                try { d[p.Name] = ToJsonValue(p.GetValue(o, null), 1); } catch { }
            }
            return d;
        }

        /// <summary>Converts a vendor value to something the JSON writer can emit.</summary>
        public static object ToJsonValue(object v, int depth = 0)
        {
            switch (v)
            {
                case null: return null;
                case string _:
                case bool _:
                case double _:
                case float _:
                case int _:
                case long _:
                case short _:
                case uint _:
                case ulong _:
                case ushort _:
                case byte _:
                case decimal _:
                    return v;
                case Enum e: return e.ToString();
                case TimeSpan ts: return ts.TotalMilliseconds;
                case DateTime dt: return dt.ToUniversalTime().ToString("o", System.Globalization.CultureInfo.InvariantCulture);
                case DateTimeOffset dto: return dto.ToString("o", System.Globalization.CultureInfo.InvariantCulture);
                case Array a:
                {
                    if (depth > 3) return a.ToString();
                    var l = new List<object>(a.Length);
                    foreach (var x in a) l.Add(ToJsonValue(x, depth + 1));
                    return l;
                }
                case System.Collections.IEnumerable seq:
                {
                    if (depth > 3) return v.ToString();
                    var l = new List<object>();
                    foreach (var x in seq) l.Add(ToJsonValue(x, depth + 1));
                    return l;
                }
            }
            if (depth > 2) return v.ToString();
            var t = v.GetType();
            if (t.IsValueType && !t.IsPrimitive) return PropsToDict(v);
            return v.ToString();
        }
    }
}
