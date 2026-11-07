using System;
using System.Collections.Generic;
using MastSatellite.Core;

namespace MastSatellite.Devices
{
    public static class DeviceFactory
    {
        /// <summary>driver -> kind (契约 §5.4).</summary>
        public static readonly Dictionary<string, string> KindOfDriver = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["lightfield"] = "spectrometer",
            ["kinesis_kdc101"] = "rotation_stage",
            ["picomotor8742"] = "openloop_stepper",
            ["motion_manipulator"] = "xyz_stage",
            ["ds1"] = "aotf",
            ["spectrasense"] = "pmt",
            ["spe_watcher"] = "file_watcher",
        };

        public static DeviceBase Create(DeviceConfig c, IDeviceHost host)
        {
            if (!KindOfDriver.ContainsKey(c.Driver ?? ""))
                throw new ArgumentException($"unknown driver '{c.Driver}' (known: {string.Join(", ", KindOfDriver.Keys)})");
            if (c.Sim)
            {
                switch (c.Driver)
                {
                    case "lightfield": return new SimSpectrometer(c, host);
                    case "kinesis_kdc101": return new SimRotationStage(c, host);
                    case "picomotor8742": return new SimOpenLoopStepper(c, host);
                    case "motion_manipulator": return new SimXyzStage(c, host);
                    case "ds1": return new SimAotf(c, host);
                    case "spectrasense": return new SimPmt(c, host);
                    case "spe_watcher": return new SpeWatcherDevice(c, host); // watches a real folder either way
                }
            }
            switch (c.Driver)
            {
                case "lightfield": return new LightFieldDevice(c, host);
                case "kinesis_kdc101": return new KinesisKdc101Device(c, host);
                case "picomotor8742": return new Picomotor8742Device(c, host);
                case "motion_manipulator": return new MotionManipulatorDevice(c, host);
                case "ds1": return new Ds1Device(c, host);
                case "spectrasense": return new SpectraSenseDevice(c, host);
                case "spe_watcher": return new SpeWatcherDevice(c, host);
            }
            throw new ArgumentException("unknown driver " + c.Driver);
        }
    }

    /// <summary>A device present in the config but "enabled": false (or whose driver failed to construct).</summary>
    public sealed class DisabledDevice : DeviceBase
    {
        private static readonly string[] CapList = { "status", "stop", "safe" };
        private readonly string _why;

        public DisabledDevice(DeviceConfig cfg, IDeviceHost host, string why = null)
            : base(cfg, host, DeviceFactory.KindOfDriver.TryGetValue(cfg.Driver ?? "", out var k) ? k : "unknown")
        {
            _why = why ?? "disabled in satellite config";
        }

        public override IReadOnlyList<string> Caps => CapList;
        public override void Start() { SetState(StateOffline, _why); }
        protected override Dictionary<string, object> BuildStatus(bool fresh) { return J.Obj("enabled", false); }
        protected override object ExecuteAction(CommandContext ctx) { throw new DeviceError(ErrorCodes.Unavailable, _why); }
        protected override void StopHardware(bool emergency, bool hubOnly) { }
    }
}
