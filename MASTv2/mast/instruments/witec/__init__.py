"""WITec alpha300 (Raman/SHG) driver — COM Automation of WITec Control.

Design contract: ``docs/v2/design/optical_station_witec.md``.

Layers, bottom to top:

* :mod:`~mast.instruments.witec.params` — parameter paths, interface ids, the write guard's tables
  (nothing imported, nothing touched);
* :mod:`~mast.instruments.witec.bucs` — :class:`~mast.instruments.witec.bucs.BucsClient`, the
  replaceable COM-client abstraction with the write guard; ``FakeBucsClient`` (in
  :mod:`~mast.instruments.witec.fake_bucs`) is an in-memory WITec Control;
* :mod:`~mast.instruments.witec.comtypes_client` — the real client over ``comtypes``
  (**imported lazily**: nothing here imports comtypes at import time, so a machine without it boots);
* :mod:`~mast.instruments.witec.truematch` — the WITEC_TRUEMATCH_ASCII spectrum text;
* :mod:`~mast.instruments.witec.microscope` — :class:`~mast.instruments.witec.microscope.WITecMicroscope`
  (a :class:`~mast.instruments.spectrometer.Spectrometer` *and* a
  :class:`~mast.instruments.microscope.Microscope`) and the registry's ``build(entry)``.
"""
