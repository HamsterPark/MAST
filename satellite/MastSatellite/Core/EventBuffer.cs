using System;
using System.Collections.Generic;

namespace MastSatellite.Core
{
    /// <summary>
    /// Events of this boot, numbered from 1, kept until the hub acks them (cumulative evt_ack).
    /// Bounded by count and bytes; on overflow the oldest are dropped and counted. After a
    /// reconnect the cursor rewinds to the hub's evt_ack so everything newer is sent again.
    /// </summary>
    public sealed class EventBuffer
    {
        public sealed class Item
        {
            public long Seq;
            public string Text;
            public int Bytes;
            public string Kind;
        }

        private readonly object _gate = new object();
        private readonly LinkedList<Item> _items = new LinkedList<Item>();
        private readonly int _maxEvents;
        private readonly long _maxBytes;
        private LinkedListNode<Item> _next; // first item not yet handed to the sender on this connection
        private long _nextSeq = 1;
        private long _bytes;
        private long _acked;
        private long _dropped;
        private long _droppedUnsent;

        /// <summary>Raised after each append; the sender uses it as a wake-up.</summary>
        public event Action Appended;

        public EventBuffer(int maxEvents, long maxBytes)
        {
            _maxEvents = Math.Max(10, maxEvents);
            _maxBytes = Math.Max(64 * 1024, maxBytes);
        }

        public long LastSeq { get { lock (_gate) return _nextSeq - 1; } }
        public long Acked { get { lock (_gate) return _acked; } }
        public int Count { get { lock (_gate) return _items.Count; } }
        public long Bytes { get { lock (_gate) return _bytes; } }
        public long Dropped { get { lock (_gate) return _dropped; } }
        public long DroppedUnsent { get { lock (_gate) return _droppedUnsent; } }
        public bool HasUnsent { get { lock (_gate) return _next != null; } }

        public long Append(string device, string kind, object data)
        {
            long seq;
            lock (_gate)
            {
                seq = _nextSeq++;
                var msg = J.Obj("t", "evt", "seq", seq, "device", device ?? "", "kind", kind, "ts_ms", Clock.UnixMs(), "data", data);
                var text = Json.Serialize(msg);
                var node = _items.AddLast(new Item { Seq = seq, Text = text, Bytes = text.Length * 2, Kind = kind });
                _bytes += node.Value.Bytes;
                if (_next == null) _next = node;
                while (_items.Count > _maxEvents || (_bytes > _maxBytes && _items.Count > 1))
                {
                    if (_items.First == _next) _droppedUnsent++;
                    RemoveFirst();
                    _dropped++;
                }
            }
            try { Appended?.Invoke(); } catch { }
            return seq;
        }

        private void RemoveFirst()
        {
            var first = _items.First;
            if (first == _next) _next = first.Next;
            _bytes -= first.Value.Bytes;
            _items.RemoveFirst();
        }

        /// <summary>Cumulative ack from the hub: everything up to and including upto has landed.</summary>
        public void Ack(long upto)
        {
            lock (_gate)
            {
                if (upto <= _acked) return;
                _acked = Math.Min(upto, _nextSeq - 1);
                while (_items.Count > 0 && _items.First.Value.Seq <= _acked) RemoveFirst();
            }
        }

        /// <summary>New connection: the hub already has seq &lt;= hubAck; resend everything after it.</summary>
        public void Rewind(long hubAck)
        {
            lock (_gate)
            {
                if (hubAck > _nextSeq - 1)
                {
                    // The hub is talking about more events than this boot ever produced.
                    Log.Warn($"hub evt_ack {hubAck} exceeds last seq {_nextSeq - 1}; resending everything");
                    hubAck = 0;
                }
                Ack(hubAck);
                _next = _items.First;
                while (_next != null && _next.Value.Seq <= hubAck) _next = _next.Next;
            }
        }

        public bool TryTakeNext(out Item item)
        {
            lock (_gate)
            {
                if (_next == null) { item = null; return false; }
                item = _next.Value;
                _next = _next.Next;
                return true;
            }
        }
    }
}
