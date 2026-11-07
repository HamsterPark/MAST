using System;
using System.Threading;

namespace MastSatellite.Core
{
    /// <summary>
    /// Token bucket for upload bytes. Tokens may go negative: a chunk larger than the bucket is
    /// let through and the caller then waits until the debt is repaid, so the long-run average
    /// never exceeds the rate. Time source and sleep are injectable for the self-test.
    /// </summary>
    public sealed class TokenBucket
    {
        private readonly object _gate = new object();
        private readonly Func<double> _now;
        private readonly Func<double, CancellationToken, bool> _sleep;
        private double _rate;
        private double _burst;
        private double _tokens;
        private double _last;

        public TokenBucket(double bytesPerSecond, double burstBytes, Func<double> now = null,
                           Func<double, CancellationToken, bool> sleep = null)
        {
            _now = now ?? (() => Clock.MonoSeconds);
            _sleep = sleep ?? DefaultSleep;
            _rate = Math.Max(1, bytesPerSecond);
            _burst = Math.Max(1, burstBytes);
            _tokens = _burst;
            _last = _now();
        }

        public double Rate { get { lock (_gate) return _rate; } }

        public void SetRate(double bytesPerSecond, double burstBytes)
        {
            lock (_gate)
            {
                Refill();
                _rate = Math.Max(1, bytesPerSecond);
                _burst = Math.Max(1, burstBytes);
                if (_tokens > _burst) _tokens = _burst;
            }
        }

        /// <summary>Takes n bytes; returns false if cancelled while waiting.</summary>
        public bool Take(int n, CancellationToken ct)
        {
            double wait;
            lock (_gate)
            {
                Refill();
                _tokens -= n;
                wait = _tokens < 0 ? -_tokens / _rate : 0;
            }
            return wait <= 0 || _sleep(wait, ct);
        }

        /// <summary>Non-blocking: takes n tokens only if they are available now.</summary>
        public bool TryTake(int n)
        {
            lock (_gate)
            {
                Refill();
                if (_tokens < n) return false;
                _tokens -= n;
                return true;
            }
        }

        private void Refill()
        {
            var t = _now();
            var dt = t - _last;
            _last = t;
            if (dt > 0) _tokens = Math.Min(_burst, _tokens + dt * _rate);
        }

        private static bool DefaultSleep(double seconds, CancellationToken ct)
        {
            return !ct.WaitHandle.WaitOne(TimeSpan.FromSeconds(seconds));
        }
    }
}
