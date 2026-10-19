import pytest

from cairn.events import Event, EventError, canonical_json

H = "a" * 64


def make(**kw):
    base = {"node": "laptop", "ts": "2026-03-10T12:00:00.000000+00:00", "op": "create", "doc_id": "d1",
            "payload": {"name": "x", "meta": {"tipo": "plan"}, "body": "# x\n"}, "prev": None}
    return Event.new(**{**base, **kw})


def test_canonical_json_is_sorted_and_compact():
    assert canonical_json({"b": 1, "a": "ñ"}) == '{"a":"ñ","b":1}'.encode()



def test_golden_hash():
    ev = Event.new(node="n1", ts="t", op="create", doc_id="d", payload={}, prev=None)
    import hashlib
    expected = hashlib.sha256(
        b'{"doc_id":"d","node":"n1","op":"create","payload":{},"prev":null,"ts":"t","v":1}').hexdigest()
    assert ev.id == expected


def test_sig_does_not_change_id():
    ev = make()
    signed = Event(**{**ev.__dict__, "sig": "firma"})
    signed.validate()
    assert signed.id == ev.id


def test_roundtrip_json():
    ev = make()
    assert Event.from_json(ev.to_json()) == ev


def test_tampered_payload_is_detected():
    line = make().to_json().replace('"tipo":"plan"', '"tipo":"bug"')
    with pytest.raises(EventError, match="does not match"):
        Event.from_json(line)


def test_floats_are_rejected():
    with pytest.raises(EventError, match="float"):
        make(payload={"x": 1.5})


def test_create_has_no_prev_and_changes_need_one():
    with pytest.raises(EventError):
        make(prev=H)
    with pytest.raises(EventError):
        make(op="edit", prev=None)
    assert make(op="edit", prev=H, payload={"body": "y"}).prev == H


def test_unknown_op_and_bad_node():
    with pytest.raises(EventError):
        make(op="borrar")
    with pytest.raises(EventError):
        make(node="Laptop Vieja")


def test_unknown_fields_are_rejected():
    line = make().to_json()[:-1] + ',"extra":1}'
    with pytest.raises(EventError, match="desconocidos"):
        Event.from_json(line)
