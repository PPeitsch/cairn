import pytest

from cairn.eventlog import EventLog
from cairn.events import Event, EventError


def ev(i, node="laptop"):
    return Event.new(node=node, ts=f"2026-03-10T12:00:{i:02d}.000000+00:00", op="create",
                     doc_id=f"d{i}", payload={"name": f"doc{i}", "body": "x" * 50}, prev=None)


def test_append_and_read(tmp_path):
    log = EventLog(tmp_path, "laptop")
    log.append([ev(1), ev(2)])
    log.append([ev(3)])
    entries = list(log.read_all())
    assert [e.event.doc_id for e in entries] == ["d1", "d2", "d3"]
    assert (tmp_path / "laptop" / "000001.jsonl").is_file()
    # reading from a position returns only what follows
    rest = list(log.read_segment("laptop", "000001", start=entries[1].end))
    assert [e.event.doc_id for e in rest] == ["d3"]


def test_segments_roll_over(tmp_path):
    log = EventLog(tmp_path, "laptop", segment_bytes=200)
    for i in range(5):
        log.append([ev(i)])
    assert len(log.segments("laptop")) > 1
    assert [e.event.doc_id for e in log.read_all()] == [f"d{i}" for i in range(5)]


def test_only_writes_own_node(tmp_path):
    with pytest.raises(EventError):
        EventLog(tmp_path, "laptop").append([ev(1, node="vps")])


def test_torn_line_is_isolated(tmp_path):
    log = EventLog(tmp_path, "laptop")
    log.append([ev(1)])
    seg = tmp_path / "laptop" / "000001.jsonl"
    with open(seg, "a") as f:
        f.write('{"cortado":')           # a write cut off halfway
    assert [e.event.doc_id for e in log.read_all()] == ["d1"]   # the incomplete line is not read
    log.append([ev(2)])
    entries = list(log.read_all())
    assert [e.event.doc_id if e.event else None for e in entries] == ["d1", None, "d2"]
    assert entries[1].error


def test_event_from_other_node_in_my_folder_is_flagged(tmp_path):
    (tmp_path / "laptop").mkdir()
    (tmp_path / "laptop" / "000001.jsonl").write_text(ev(1, node="vps").to_json() + "\n")
    [entry] = EventLog(tmp_path, "laptop").read_all()
    assert entry.event is None and "vps" in entry.error
