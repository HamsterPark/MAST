"""卫星协议 v1 的消息模型与校验（契约 §5.2）。"""

from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from mast.satellite import protocol as P

SHA = "a" * 64


def _hello(**kw):
    m = {"t": "hello", "proto": 1, "satellite_id": "rig1", "boot_id": "b1", "sw": "x",
         "host": "SPEC", "time_ms": 1, "devices": [{"id": "ccd", "kind": "spectrometer",
                                                     "state": "ready", "caps": ["status"]}],
         "evt_seq": 0}
    m.update(kw)
    return m


def test_every_satellite_message_parses():
    msgs = [
        _hello(),
        {"t": "evt", "seq": 1, "device": "ccd", "kind": "spectrum", "ts_ms": 5, "data": {}},
        {"t": "ack", "id": "c1", "state": "accepted"},
        {"t": "result", "id": "c1", "ok": False, "error": {"code": "operator_hold", "message": ""},
         "started_ms": 1, "finished_ms": 2},
        {"t": "pong", "n": 1, "t0": 1, "t1": 2, "t2": 3},
        {"t": "file_offer", "file_id": SHA, "name": "a.spe", "rel_path": "x/a.spe", "size": 3,
         "sha256": SHA, "mtime_ms": 1, "kind": "spe", "meta": {}},
        {"t": "file_chunk", "file_id": SHA, "offset": 0, "data_b64": "AAAA"},
        {"t": "file_end", "file_id": SHA},
        {"t": "cmd_status_reply", "items": [{"id": "c1", "state": "done", "result": {"ok": True}}]},
        {"t": "estop_ack", "stopped": ["ccd"], "errors": [{"device": "pol", "message": "x"}]},
        {"t": "log", "level": "info", "msg": "hi", "ts_ms": 1},
    ]
    kinds = [type(P.parse_to_hub(json.dumps(m))).__name__ for m in msgs]
    assert kinds == ["Hello", "Evt", "Ack", "Result", "Pong", "FileOffer", "FileChunk", "FileEnd",
                     "CmdStatusReply", "EstopAck", "Log"]


def test_every_hub_message_parses():
    msgs = [
        {"t": "welcome", "proto": 1, "hub_boot_id": "h", "session": "s", "time_ms": 1,
         "evt_ack": 0, "config": {"heartbeat_s": 1.0, "deadman_s": 5.0,
                                  "max_upload_bytes_per_s": 1048576}},
        {"t": "cmd", "id": "c", "device": "ccd", "action": "status", "args": {}, "timeout_s": 3},
        {"t": "cancel", "id": "c"},
        {"t": "ping", "n": 1, "t0": 2},
        {"t": "evt_ack", "upto": 4},
        {"t": "file_want", "file_id": SHA, "offset": 0},
        {"t": "file_done", "file_id": SHA, "ok": True, "stored": "s"},
        {"t": "cmd_status", "ids": ["c"]},
        {"t": "estop", "reason": "x"},
    ]
    kinds = [type(P.parse_to_satellite(m)).__name__ for m in msgs]
    assert kinds == ["Welcome", "Cmd", "Cancel", "Ping", "EvtAckMsg", "FileWant", "FileDone",
                     "CmdStatus", "Estop"]


def test_unknown_type_is_not_an_error_and_unknown_fields_survive():
    m = P.parse_to_hub({"t": "future_thing", "x": 1})
    assert isinstance(m, P.UnknownMessage) and m.t == "future_thing"
    h = P.parse_to_hub(_hello(extra_field="kept"))
    assert h.model_dump()["extra_field"] == "kept"


@pytest.mark.parametrize("bad, code", [
    ("not json", "bad_json"),
    ("[1,2]", "not_object"),
    ('{"x": 1}', "no_type"),
    ('{"t": "evt", "seq": 0, "device": "a", "kind": "k", "ts_ms": 1}', "invalid"),   # seq ≥ 1
    ('{"t": "ack", "id": "c", "state": "maybe"}', "invalid"),                        # 闭集
    ('{"t": "cmd_status_reply", "items": [{"id": "c", "state": "weird"}]}', "invalid"),
])
def test_invalid_messages_raise_protocol_error(bad, code):
    with pytest.raises(P.ProtocolError) as ei:
        P.parse_to_hub(bad)
    assert ei.value.code == code


def test_file_offer_requires_sha256_hex_and_normalises_case():
    m = P.parse_to_hub({"t": "file_offer", "file_id": SHA.upper(), "name": "a", "size": 1,
                        "sha256": SHA.upper()})
    assert m.file_id == SHA and m.sha256 == SHA
    with pytest.raises(P.ProtocolError):
        P.parse_to_hub({"t": "file_offer", "file_id": "xyz", "name": "a", "size": 1,
                        "sha256": "xyz"})


def test_frame_size_limit_counts_utf8_bytes():
    assert not P.frame_too_big("a" * P.MAX_FRAME_BYTES)
    assert P.frame_too_big("a" * (P.MAX_FRAME_BYTES + 1))
    # 中文 3 字节：字符数没超、字节数超了
    assert P.frame_too_big("中" * (P.MAX_FRAME_BYTES // 3 + 1))
    with pytest.raises(P.ProtocolError) as ei:
        P.parse_to_hub('{"t":"log","msg":"' + "x" * P.MAX_FRAME_BYTES + '"}')
    assert ei.value.code == "too_big"


def test_array_codec_roundtrip_and_length_check():
    y = np.arange(1340, dtype=np.uint16)
    b64 = P.encode_array(y, "u16")
    assert base64.b64decode(b64)[:4] == b"\x00\x00\x01\x00"            # 小端
    assert np.array_equal(P.decode_array(b64, "u16", 1340), y)
    nm = np.linspace(635.0, 692.0, 1340)
    assert np.allclose(P.decode_array(P.encode_array(nm, "f64"), "f64", 1340), nm)
    with pytest.raises(P.ProtocolError):
        P.decode_array(b64, "u16", 1339)
    with pytest.raises(P.ProtocolError):
        P.decode_array(b64, "i8")


def test_error_code_normalisation_keeps_unknown_codes():
    assert P.error_code("operator_hold") == "operator_hold"
    assert P.error_code("") == "internal"
    assert P.error_code("New_Code") == "new_code"
    assert {"invalid_args", "not_supported", "unavailable", "device_busy", "operator_hold",
            "limit", "timeout", "cancelled", "lost_on_restart", "internal"} == P.ERROR_CODES


def test_encode_is_compact_utf8():
    s = P.encode({"t": "log", "msg": "中文"})
    assert s == '{"t":"log","msg":"中文"}'
