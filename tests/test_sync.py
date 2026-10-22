"""Sync between isolated nodes: each with its own config, data and key in a temp dir.

The server runs in a thread and both sides talk over OS pipes, just as over ssh. At the end, a
test with real processes: `cairn sync` launches a fake "ssh" (`CAIRN_SSH`) that runs
`cairn sync-peer --stdio --as <node>` with the other node's config.
"""

import json
import os
import sys
import threading

import pytest

from cairn import access, cli, config, docs, edit, keys
from cairn.store import Store
from cairn.sync import Channel, ProtocolError, client, serve

NOW = "2026-03-10T12:00:00+00:00"


class Node:
    def __init__(self, name, root, monkeypatch):
        self.name, self.root, self.mp = name, root / name, monkeypatch
        self.now = NOW
        (self.root / "projects").mkdir(parents=True)
        self.use()
        config.write(name)
        with self.store() as s:
            self.pub = s.public_line()

    def env(self) -> dict:
        return {"CAIRN_CONFIG_DIR": str(self.root / "config"), "CAIRN_DATA_DIR": str(self.root / "data"),
                "CAIRN_PROJECTS": str(self.root / "projects"), "CAIRN_NOW": self.now,
                "CAIRN_AUTHORIZED_KEYS": str(self.root / "authorized_keys")}

    def use(self):
        for k, v in self.env().items():
            self.mp.setenv(k, v)
        return self

    def store(self) -> Store:
        self.use()
        return Store(config.load())

    def trust(self, other, ssh=False):
        self.use()
        access.authorize(other.name, other.pub, ssh=ssh, command=["/opt/cairn"])

    def new(self, area, topic, type_="plan"):
        with self.store() as s:
            return docs.new(s, area, topic, type_, "demo").doc

    def names(self) -> set[str]:
        with self.store() as s:
            return {d.name for d in docs.all_docs(s)}

    def body(self, name) -> str:
        with self.store() as s:
            return docs.find(s, name).body


@pytest.fixture
def nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_NO_AUTOSYNC", "1")
    made = {}

    def make(name):
        made[name] = Node(name, tmp_path, monkeypatch)
        return made[name]
    return make


@pytest.fixture
def pair(nodes):
    """laptop and vps, which know each other; laptop connects to the vps."""
    laptop, vps = nodes("laptop"), nodes("vps")
    vps.trust(laptop, ssh=True)
    laptop.trust(vps)
    return laptop, vps


def run_sync(cli_node, srv_node, as_node=None):
    """One sync session: returns (client result, server result)."""
    c2s_r, c2s_w = os.pipe()
    s2c_r, s2c_w = os.pipe()
    srv_cfg = (srv_node.use(), config.load())[1]
    cli_cfg = (cli_node.use(), config.load())[1]
    out = {}

    def server():
        with Store(srv_cfg) as s, open(c2s_r, "rb") as r, open(s2c_w, "wb") as w:
            out["srv"] = serve(s, Channel(r, w), as_node)

    t = threading.Thread(target=server)
    t.start()
    try:
        with Store(cli_cfg) as s, open(s2c_r, "rb") as r, open(c2s_w, "wb") as w:
            try:
                out["cli"] = client(s, Channel(r, w), srv_node.name)
            except ProtocolError as e:
                out["cli"] = e
    finally:
        t.join(10)
    return out["cli"], out.get("srv")


def test_exchange_in_both_directions(pair):
    laptop, vps = pair
    laptop.new("demo", "desde_laptop")
    vps.new("demo", "desde_vps")
    res, srv = run_sync(laptop, vps)
    assert res.ok and srv.ok, (res.errors, srv.errors)
    assert (res.sent, res.received) == (1, 1)
    assert laptop.names() == vps.names() == {"2026-03-10_demo_desde_laptop", "2026-03-10_demo_desde_vps"}
    res, _ = run_sync(laptop, vps)                    # already equal: nothing travels
    assert (res.sent, res.received) == (0, 0)


def test_the_chain_continues_across_nodes(pair):
    laptop, vps = pair
    doc = laptop.new("demo", "plan")
    run_sync(laptop, vps)
    with vps.store() as s:
        edit.apply(s, doc.name, body="# editado en el vps\n")
    run_sync(laptop, vps)
    assert laptop.body(doc.name) == "# editado en el vps\n"
    with laptop.store() as s:
        assert docs.check(s) == []
        assert not docs.find(s, doc.name).divergent


def test_old_vocabulary_events_and_new_ones_across_nodes(pair):
    """The vps still has events as the Spanish-vocabulary version wrote them; the laptop keeps
    going in English on top. Both end up with the same doc."""
    laptop, vps = pair
    from cairn.store import new_doc_id
    doc_id = new_doc_id()
    with vps.store() as s, s.writer() as w:
        w.emit("create", doc_id, {"name": "2026-03-01_demo_viejo", "body": "# viejo\n", "meta": {
            "tipo": "plan", "estado": "en_progreso", "fecha": "2026-03-01", "nota": "de antes"}})
    run_sync(laptop, vps)
    with laptop.store() as s:
        docs.close(s, "2026-03-01_demo_viejo", force=True)
    run_sync(laptop, vps)
    states = []
    for node in (laptop, vps):
        with node.store() as s:
            d = docs.find(s, "2026-03-01_demo_viejo")
            states.append((d.scope, d.meta, d.head))
            assert docs.check(s) == []
    assert states[0] == states[1]
    assert states[0][0] == "done" and states[0][1] == {
        "type": "plan", "status": "completed", "date": "2026-03-01", "closed": "2026-03-10"}


def test_divergence_between_nodes_is_the_same_on_both(pair):
    laptop, vps = pair
    doc = laptop.new("demo", "plan")
    run_sync(laptop, vps)
    with laptop.store() as s:
        edit.apply(s, doc.name, body="# laptop\n")
    with vps.store() as s:
        vps.mp.setenv("CAIRN_NOW", "2026-03-10T13:00:00+00:00")   # the vps one is later
        edit.apply(s, doc.name, body="# vps\n")
    run_sync(laptop, vps)
    for n in (laptop, vps):
        with n.store() as s:
            d = docs.find(s, doc.name)
            assert d.divergent and d.body == "# laptop\n"     # the oldest branch wins


def test_relay_and_unknown_nodes(pair, nodes):
    laptop, vps = pair
    aws = nodes("aws")
    vps.trust(aws, ssh=True)
    aws.trust(vps)
    aws.new("demo", "desde_aws")
    assert run_sync(aws, vps)[0].ok
    res, _ = run_sync(laptop, vps)                    # laptop does not know aws
    assert not res.ok and any("aws" in e and "is not authorized" in e for e in res.errors)
    assert "2026-03-10_demo_desde_aws" not in laptop.names()
    laptop.trust(aws)
    res, _ = run_sync(laptop, vps)
    assert res.ok and "2026-03-10_demo_desde_aws" in laptop.names()


def test_unauthorized_client(pair, nodes):
    _, vps = pair
    intruso = nodes("intruso")
    intruso.trust(vps)
    res, srv = run_sync(intruso, vps)
    assert isinstance(res, ProtocolError) and "is not authorized on vps" in str(res)
    assert not srv.ok


def test_identity_pinned_by_authorized_keys(pair):
    laptop, vps = pair
    res, _ = run_sync(laptop, vps, as_node="otro")
    assert isinstance(res, ProtocolError) and "the key belongs to otro" in str(res)
    res, _ = run_sync(laptop, vps, as_node="laptop")
    assert res.ok


def test_nobody_writes_another_nodes_log(pair):
    laptop, vps = pair
    laptop.new("demo", "x")
    run_sync(laptop, vps)
    # a compromised vps adds an event to laptop's log (signed with its own key)
    with vps.store() as s:
        seg = s.config.logs_dir / "laptop" / "000001.jsonl"
        ev = json.loads(seg.read_text().splitlines()[0])
        ev["payload"]["name"] = "inyectado"
    with open(seg, "a") as f:
        f.write(json.dumps(ev) + "\n")
    res, _ = run_sync(laptop, vps)
    assert res.ok and res.received == 0               # the server never sends anyone their own log
    assert "inyectado" not in laptop.names()
    # and if a malicious server sent it anyway, the receiver rejects it
    import base64

    from cairn.sync import receive
    with laptop.store() as s:
        size = (s.config.logs_dir / "laptop" / "000001.jsonl").stat().st_size
        n, errors = receive(s, [{"node": "laptop", "segment": "000001", "offset": size,
                                 "data": base64.b64encode(json.dumps(ev).encode() + b"\n").decode()}])
    assert n == 0 and any("only it writes that log" in e for e in errors)
    assert "inyectado" not in laptop.names()


def test_forged_event_is_rejected(pair, nodes):
    laptop, vps = pair
    aws = nodes("aws")
    for n in (laptop, vps):
        n.trust(aws)
    aws.new("demo", "legitimo")
    # the vps forges an aws log with an event signed with another key
    with aws.store() as s:
        line = (s.config.logs_dir / "aws" / "000001.jsonl").read_text()
    falso = nodes("falso")
    ev = json.loads(line)
    from cairn import keys
    from cairn.events import Event
    with falso.store() as s:
        forged = Event.from_json(line.strip())
        ev["sig"] = keys.sign(s.key, forged.id)
    vps.use()
    d = vps.root / "data" / "logs" / "aws"
    d.mkdir(parents=True)
    (d / "000001.jsonl").write_text(json.dumps(ev, separators=(",", ":"), sort_keys=True) + "\n")
    res, _ = run_sync(laptop, vps)
    assert any("invalid signature" in e for e in res.errors)
    assert "2026-03-10_demo_legitimo" not in laptop.names()


def test_revoked(pair):
    laptop, vps = pair
    vps.use()
    access.revoke("laptop")
    res, _ = run_sync(laptop, vps)
    assert isinstance(res, ProtocolError) and "is not authorized on vps" in str(res)


def test_copies_that_are_not_a_prefix(pair):
    laptop, vps = pair
    laptop.new("demo", "x")
    run_sync(laptop, vps)
    seg = vps.root / "data" / "logs" / "laptop" / "000001.jsonl"
    data = bytearray(seg.read_bytes())
    data[10] = ord("X") if data[10] != ord("X") else ord("Y")
    seg.write_bytes(bytes(data))
    laptop.new("demo", "y")
    res, _ = run_sync(laptop, vps)
    assert any("is not a prefix" in e for e in res.errors)


def test_broken_line_at_origin_is_copied_not_projected(pair):
    laptop, vps = pair
    laptop.new("demo", "x")
    with open(laptop.root / "data" / "logs" / "laptop" / "000001.jsonl", "ab") as f:
        f.write(b'{"cortado\n')
    laptop.new("demo", "y")
    res, _ = run_sync(laptop, vps)
    assert res.ok and res.sent == 2
    assert vps.names() == laptop.names()


# ──────────────────────────────────────────────────────────────── authorized_keys

def test_authorize_and_revoke_in_authorized_keys(nodes):
    laptop, vps = nodes("laptop"), nodes("vps")
    ak = vps.root / "authorized_keys"
    ak.write_text("ssh-ed25519 AAAAotra usuario@maquina\n")
    vps.trust(laptop, ssh=True)
    vps.trust(laptop, ssh=True)                       # idempotent
    lines = ak.read_text().splitlines()
    assert lines[0] == "ssh-ed25519 AAAAotra usuario@maquina"
    assert len(lines) == 2
    assert lines[1].startswith('command="/opt/cairn sync-peer --stdio --as laptop",restrict ssh-ed25519 ')
    assert lines[1].endswith(" cairn:laptop")
    assert oct(ak.stat().st_mode & 0o777) == "0o600"
    assert access.ssh_authorized_nodes() == ["laptop"]
    access.revoke("laptop")
    assert ak.read_text().splitlines() == ["ssh-ed25519 AAAAotra usuario@maquina"]
    assert config.load().peers["laptop"].revoked == "2026-03-10"


def test_authorize_another_key_for_the_same_node(nodes):
    laptop, vps, otro = nodes("laptop"), nodes("vps"), nodes("otro")
    vps.trust(laptop)
    vps.use()
    with pytest.raises(cli.CairnError, match="another key"):
        access.authorize("laptop", otro.pub, ssh=False)
    with pytest.raises(cli.CairnError, match="this very node"):
        access.authorize("vps", otro.pub)


def test_remotes(nodes):
    nodes("laptop")
    access.remote_add("vps", "ssh://cairn@host:2222")
    assert config.load().remotes["vps"].url == "ssh://cairn@host:2222"
    with pytest.raises(cli.CairnError, match="Invalid remote URL"):
        access.remote_add("x", "cairn@host")
    access.remote_remove("vps")
    assert config.load().remotes == {}


# ──────────────────────────────────────────────────────────────── real processes

FAKE_SSH = """\
import os, sys
# like ssh with the restricted key: ignores what the client asks and runs the forced command
env = dict(os.environ, **{k[len("PEER_"):]: v for k, v in os.environ.items() if k.startswith("PEER_")})
os.execvpe(sys.executable, [sys.executable, "-m", "cairn", "sync-peer", "--stdio", "--as", "laptop"], env)
"""


def test_cli_sync_over_fake_ssh(pair, tmp_path, monkeypatch, capsys):
    laptop, vps = pair
    fake = tmp_path / "fake_ssh.py"
    fake.write_text(FAKE_SSH)
    laptop.new("demo", "desde_laptop")
    vps.new("demo", "desde_vps")
    for k, v in vps.env().items():
        monkeypatch.setenv(f"PEER_{k}", v)
    monkeypatch.setenv("CAIRN_SSH", f"{sys.executable} {fake}")
    laptop.use()
    access.remote_add("vps", "ssh://cairn@vps.example")
    assert cli.main(["sync"]) == 0, capsys.readouterr()
    assert "vps (vps): 1 sent, 1 received" in capsys.readouterr().out
    assert laptop.names() == vps.names()
    laptop.use()
    assert cli.main(["sync", "--quiet"]) == 0
    assert capsys.readouterr().out == ""


# ──────────────────────────────────────────────────────────────── resolve

def _divergir(laptop, vps):
    doc = laptop.new("demo", "plan")
    run_sync(laptop, vps)
    with laptop.store() as s:
        edit.apply(s, doc.name, body="# laptop\n", set_={"note": "de laptop"})
    with vps.store() as s:
        vps.mp.setenv("CAIRN_NOW", "2026-03-10T13:00:00+00:00")
        edit.apply(s, doc.name, body="# vps 1\n")
        edit.apply(s, doc.name, body="# vps 2\n")
    run_sync(laptop, vps)
    return doc.name


def test_resolve_keep_the_branch(pair, capsys):
    from cairn import history
    laptop, vps = pair
    name = _divergir(laptop, vps)
    laptop.use()
    assert cli.main(["resolve", name]) == 0
    out = capsys.readouterr().out
    assert "1 divergent branch(es)" in out and "(2 event(s): edit, edit)" in out
    assert "+# vps 2" in out
    with laptop.store() as s:
        [rama] = history.branches(s, docs.find(s, name))
        tip = rama.tip.id
    assert cli.main(["resolve", name, "--keep", tip[:8]]) == 0
    assert "2 event(s)" in capsys.readouterr().out
    run_sync(laptop, vps)
    for n in (laptop, vps):
        with n.store() as s:
            d = docs.find(s, name)
            assert not d.divergent and d.body == "# vps 2\n" and "note" not in d.meta
            assert docs.check(s) == []


def test_resolve_keep_the_main_chain(pair):
    from cairn import history
    laptop, vps = pair
    name = _divergir(laptop, vps)
    with vps.store() as s:
        d, n = history.resolve(s, name, "main")
        assert n == 2 and not d.divergent and d.body == "# laptop\n"
        assert docs.check(s) == []
        with pytest.raises(cli.CairnError, match="has no divergent branches"):
            history.resolve(s, name, "main")


def test_resolve_hash_that_is_not_a_branch(pair):
    from cairn import history
    laptop, vps = pair
    name = _divergir(laptop, vps)
    with laptop.store() as s, pytest.raises(cli.CairnError, match="is not the tip"):
        history.resolve(s, name, "0000")


# ──────────────────────────────────────────────────────────────── key change and restore

def at(node, when):
    node.now = when
    return node.use()


def test_rotate_and_replace_keep_the_old(pair, capsys):
    laptop, vps = pair
    doc = laptop.new("demo", "antes")
    run_sync(laptop, vps)
    with laptop.store() as s:
        old_priv = s.key
        out = access.rotate_key(s)
        assert "--replace" in out[-1]
        assert docs.check(s) == []                    # the node's own earlier events still verify
    with laptop.store() as s:
        assert s.rebuild() == 1 and docs.check(s) == [] and len(s.config.old_keys) == 1
        new_pub = s.public_line()
    at(laptop, "2026-03-11T12:00:00+00:00").new("demo", "despues")
    res, _ = run_sync(laptop, vps)                    # the vps does not know the new key yet
    assert any("invalid signature" in e for e in res.errors)

    at(vps, "2026-03-11T10:00:00+00:00")
    with pytest.raises(cli.CairnError, match="--replace"):
        access.authorize("laptop", new_pub, ssh=True, command=["/opt/cairn"])
    hecho = access.authorize("laptop", new_pub, ssh=True, command=["/opt/cairn"], replace=True)
    assert any("the previous key of laptop" in h for h in hecho)
    peer = config.load().peers["laptop"]
    assert [k.until[:10] for k in peer.old] == ["2026-03-11"]
    res, _ = run_sync(laptop, vps)
    assert res.ok, res.errors
    assert vps.names() == {doc.name, "2026-03-11_demo_despues"}
    with vps.store() as s:
        assert s.rebuild() == 2 and docs.check(s) == []

    # the old key cannot sign anything after the change
    from cairn import keys
    from cairn.events import Event
    with vps.store() as s:
        head = docs.find(s, doc.name)
        ev = Event.new(node="laptop", ts="2026-03-12T00:00:00.000000+00:00", op="edit",
                       doc_id=head.id, payload={"body": "# falso\n"}, prev=s.projection.head(head.id))
        ev = Event(**{**ev.__dict__, "sig": keys.sign(old_priv, ev.id)})
        assert "replaced on 2026-03-11" in s.verifier.problem(ev)
        viejo = Event(**{**ev.__dict__, "ts": "2026-03-10T00:00:00.000000+00:00"}).with_id()
        viejo = Event(**{**viejo.__dict__, "sig": keys.sign(old_priv, viejo.id)})
        assert s.verifier.problem(viejo) is None


def test_reauthorizing_a_revoked_node_keeps_the_old(pair):
    laptop, vps = pair
    laptop.new("demo", "antes")
    run_sync(laptop, vps)
    otro = Node("otro-laptop", vps.root.parent, vps.mp)
    vps.use()
    access.revoke("laptop")
    access.authorize("laptop", otro.pub, ssh=False)
    peer = config.load().peers["laptop"]
    assert peer.revoked is None and [k.until[:10] for k in peer.old] == ["2026-03-10"]
    with vps.store() as s:
        assert s.rebuild() == 1 and docs.check(s) == []


def test_peers_shows_previous_keys(pair, capsys):
    laptop, vps = pair
    with laptop.store() as s:
        access.rotate_key(s)
        pub = s.public_line()
    vps.use()
    access.authorize("laptop", pub, ssh=False, replace=True)
    assert cli.main(["peers"]) == 0
    out = capsys.readouterr().out
    assert "previous" in out and "until 2026-03-10T12:00:00" in out


def wipe(node):
    """The node loses its data (log, database and key) and keeps only the config."""
    import shutil
    shutil.rmtree(node.root / "data")
    with node.store() as s:
        node.pub = s.public_line()


def test_restore_of_a_node_that_lost_its_data(pair):
    laptop, vps = pair
    a = laptop.new("demo", "uno")
    with laptop.store() as s:
        edit.apply(s, a.name, body="# editado\n")
    vps.new("demo", "del_vps")
    run_sync(laptop, vps)
    huella = keys.fingerprint(laptop.pub)            # the one noted down outside the laptop
    wipe(laptop)
    assert laptop.names() == set()

    # without the replace on the vps, the restore does not start
    res, _ = run_sync_restore(laptop, vps, old_keys=[huella])
    assert isinstance(res, ProtocolError) and "--replace" in str(res)
    vps.use()
    access.authorize("laptop", laptop.pub, ssh=True, command=["/opt/cairn"], replace=True)
    # nor without the noted fingerprint: the vps says which key it was, but it is not believed blindly
    res, _ = run_sync_restore(laptop, vps)
    assert isinstance(res, ProtocolError) and "--old-key" in str(res) and huella in str(res)
    with laptop.store() as s:
        assert s.config.old_keys == () and laptop.names() == set()
    res, srv = run_sync_restore(laptop, vps, old_keys=[huella])
    assert res.ok and srv.ok, (res.errors, srv.errors)
    assert laptop.names() == vps.names() == {a.name, "2026-03-10_demo_del_vps"}
    assert laptop.body(a.name) == "# editado\n"
    with laptop.store() as s:
        assert len(s.config.old_keys) == 1 and docs.check(s) == []
    # the own chain continues: new events attach to the restored ones and the vps accepts them
    with laptop.store() as s:
        edit.apply(s, a.name, body="# después del restore\n")
    res, _ = run_sync(laptop, vps)
    assert res.ok and res.sent == 1, res.errors
    assert vps.body(a.name) == "# después del restore\n"
    with vps.store() as s:
        assert docs.check(s) == []
    # a second restore no longer applies
    res, _ = run_sync_restore(laptop, vps, old_keys=[huella])
    assert isinstance(res, ProtocolError) and "already has events of its own" in str(res)


def test_without_restore_the_own_log_is_rejected(pair):
    laptop, vps = pair
    laptop.new("demo", "uno")
    run_sync(laptop, vps)
    wipe(laptop)
    vps.use()
    access.authorize("laptop", laptop.pub, ssh=True, command=["/opt/cairn"], replace=True)
    res, _ = run_sync(laptop, vps)
    assert res.ok and laptop.names() == set()        # the vps does not send it its own log


def run_sync_restore(cli_node, srv_node, **opts):
    c2s_r, c2s_w = os.pipe()
    s2c_r, s2c_w = os.pipe()
    srv_cfg = (srv_node.use(), config.load())[1]
    cli_cfg = (cli_node.use(), config.load())[1]
    out = {}

    def server():
        try:
            with Store(srv_cfg) as s, open(c2s_r, "rb") as r, open(s2c_w, "wb") as w:
                out["srv"] = serve(s, Channel(r, w))
        except (ProtocolError, OSError) as e:          # the client never got to talk
            out["srv"] = e

    t = threading.Thread(target=server)
    t.start()
    try:
        with Store(cli_cfg) as s, open(s2c_r, "rb") as r, open(c2s_w, "wb") as w:
            try:
                out["cli"] = client(s, Channel(r, w), srv_node.name, restore=True, **opts)
            except ProtocolError as e:
                out["cli"] = e
                w.close()
    finally:
        t.join(10)
    return out["cli"], out.get("srv")


def test_cli_restore_and_rotate(pair, capsys):
    laptop, vps = pair
    laptop.use()
    access.remote_add("vps", "ssh://cairn@host")
    access.remote_add("otro", "ssh://cairn@otro")
    assert cli.main(["sync", "--restore"]) == 1
    assert "a single remote" in capsys.readouterr().err
    laptop.new("demo", "x")
    assert cli.main(["sync", "--restore", "vps"]) == 1
    assert "already has events of its own" in capsys.readouterr().err
    before = laptop.pub
    assert cli.main(["key", "--rotate"]) == 0
    out = capsys.readouterr().out
    assert "new key of laptop" in out and "authorize laptop" in out
    assert cli.main(["key"]) == 0
    now = capsys.readouterr().out.strip()
    assert now != before and (laptop.root / "data" / "node_key.pub").read_text().strip() == now
    assert not (laptop.root / "data" / "new_node_key").exists()


def _perdio_y_se_reautorizo(pair):
    """The laptop wrote, synced and lost its data; the vps already accepted its new key.
    Returns the doc name and the fingerprint of the lost key."""
    laptop, vps = pair
    a = laptop.new("demo", "uno")
    run_sync(laptop, vps)
    huella = keys.fingerprint(laptop.pub)
    wipe(laptop)
    vps.use()
    access.authorize("laptop", laptop.pub, ssh=True, command=["/opt/cairn"], replace=True)
    return a.name, huella


def _sin_restaurar(laptop):
    with laptop.store() as s:
        assert s.config.old_keys == () and s.log.segments("laptop") == []
    assert laptop.names() == set()


def test_restore_stops_if_the_peer_sends_a_wrong_old_key(pair):
    laptop, vps = pair
    _, huella = _perdio_y_se_reautorizo(pair)
    # the compromised vps claims the laptop's old key is another one, which it controls
    falsa = Node("atacante", vps.root.parent, vps.mp).pub
    vps.use()
    config.update(lambda raw: raw["peers"]["laptop"]["old"]["1"].update({"key": falsa}))
    res, _ = run_sync_restore(laptop, vps, old_keys=[huella])
    assert isinstance(res, ProtocolError) and f"has no record of the previous key {huella}" in str(res)
    _sin_restaurar(laptop)
    # without fingerprints, it reports which one the vps claims (the fake) and does not restore either
    res, _ = run_sync_restore(laptop, vps)
    assert isinstance(res, ProtocolError) and keys.fingerprint(falsa) in str(res)
    _sin_restaurar(laptop)


def test_restore_with_an_extra_or_mistyped_fingerprint(pair):
    laptop, vps = pair
    _, huella = _perdio_y_se_reautorizo(pair)
    otra = keys.fingerprint(Node("otro", vps.root.parent, vps.mp).pub)
    res, _ = run_sync_restore(laptop, vps, old_keys=[huella, otra])
    assert isinstance(res, ProtocolError) and otra in str(res)
    _sin_restaurar(laptop)


def test_restore_trusting_the_peer(pair):
    laptop, vps = pair
    name, _ = _perdio_y_se_reautorizo(pair)
    res, srv = run_sync_restore(laptop, vps, trust_peer_keys=True)
    assert res.ok and srv.ok, (res.errors, srv.errors)
    assert name in laptop.names()


def test_restore_with_the_same_key_needs_no_fingerprints(pair):
    import shutil
    laptop, vps = pair
    a = laptop.new("demo", "uno")
    run_sync(laptop, vps)
    data = laptop.root / "data"
    shutil.copy(data / "node_key", laptop.root / "backup_key")      # backup of the private key
    wipe(laptop)
    (data / "node_key").unlink()
    (data / "node_key.pub").unlink()
    shutil.copy(laptop.root / "backup_key", data / "node_key")
    res, srv = run_sync_restore(laptop, vps)
    assert res.ok and srv.ok, (res.errors, srv.errors)
    assert a.name in laptop.names()


def test_cli_fingerprint_and_restore_options(pair, capsys):
    laptop, vps = pair
    laptop.use()
    assert cli.main(["key"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == laptop.pub                            # alone, for `"$(cairn key)"`
    assert keys.fingerprint(laptop.pub) in cap.err and "--old-key" in cap.err
    assert cli.main(["key", "--fingerprint"]) == 0
    assert capsys.readouterr().out.strip() == keys.fingerprint(laptop.pub)
    access.remote_add("vps", "ssh://cairn@host")
    assert cli.main(["sync", "--old-key", keys.fingerprint(laptop.pub)]) == 1
    assert "are for `sync --restore`" in capsys.readouterr().err
    assert cli.main(["sync", "--restore", "vps", "--old-key", "SHA256:mal"]) == 1
    assert "invalid fingerprint" in capsys.readouterr().err
    assert cli.main(["key", "--rotate"]) == 0
    out = capsys.readouterr().out
    with laptop.store() as s:
        assert f"new fingerprint: {keys.fingerprint(s.public_line())}" in out
    vps.use()
    access.authorize("laptop", s.public_line(), ssh=False, replace=True)
    assert cli.main(["peers"]) == 0
    out = capsys.readouterr().out
    assert keys.fingerprint(laptop.pub) in out and "previous" in out


# ──────────────────────────────────────────────────────────────── automatic trigger

def test_every_write_triggers_sync_and_reads_do_not(pair, monkeypatch):
    from cairn import sync
    laptop, _ = pair
    calls = []
    monkeypatch.setattr(sync, "trigger", lambda cfg, delay=None: calls.append(delay))
    doc = laptop.new("demo", "plan")
    with laptop.store() as s:
        edit.apply(s, doc.name, body="# editado\n")      # editing used to not trigger it
        docs.find(s, doc.name)
        docs.all_docs(s)
    assert calls == [None, None]


def test_debounce_does_not_launch_another_if_one_is_waiting(pair, monkeypatch):
    from cairn import sync
    laptop, _ = pair
    laptop.use()
    access.remote_add("vps", "ssh://cairn@vps.example")
    monkeypatch.delenv("CAIRN_NO_AUTOSYNC")
    cfg = config.load()
    launched = []
    monkeypatch.setattr(sync.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    assert sync.trigger(cfg)
    assert launched[-1][-4:] == ["sync", "--quiet", "--after", "5"]
    with sync._flock(sync._wait_path(cfg), blocking=False) as got:   # someone waiting
        assert got
        assert not sync.trigger(cfg)
        assert not sync.wait_turn(cfg, 0.01)
        assert sync.trigger(cfg, delay=0)                  # the hook's does not wait
    assert launched[-1][-2:] == ["sync", "--quiet"] and len(launched) == 2
    assert sync.wait_turn(cfg, 0.01)


def test_no_remotes_or_auto_off_does_not_trigger(pair, monkeypatch):
    from cairn import sync
    laptop, _ = pair
    laptop.use()
    monkeypatch.delenv("CAIRN_NO_AUTOSYNC")
    monkeypatch.setattr(sync.subprocess, "Popen", lambda *a, **kw: pytest.fail("no debía lanzar"))
    assert not sync.trigger(config.load())                 # no remotes
    access.remote_add("vps", "ssh://cairn@vps.example")
    config.update(lambda raw: raw.setdefault("sync", {}).update({"auto": "false"}))
    assert not sync.trigger(config.load())


def test_an_edit_reaches_the_remote_on_its_own(pair, tmp_path, monkeypatch):
    """End to end, with real processes: editing on the laptop launches `cairn sync --after`,
    which waits for the debounce and pushes through the fake ssh."""
    import time

    from cairn import integrate
    laptop, vps = pair
    fake = tmp_path / "fake_ssh.py"
    fake.write_text(FAKE_SSH)
    for k, v in vps.env().items():
        monkeypatch.setenv(f"PEER_{k}", v)
    monkeypatch.setenv("CAIRN_SSH", f"{sys.executable} {fake}")
    monkeypatch.setattr(integrate, "default_command", lambda: [sys.executable, "-m", "cairn"])
    doc = laptop.new("demo", "sesion_larga")               # no remote yet: it does not travel
    laptop.use()
    access.remote_add("vps", "ssh://cairn@vps.example")
    config.update(lambda raw: raw.setdefault("sync", {}).update({"debounce": "0.3"}))
    monkeypatch.delenv("CAIRN_NO_AUTOSYNC")
    for i in range(3):                                      # a burst of edits
        with laptop.store() as s:
            edit.apply(s, doc.name, body=f"# edición {i}\n")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if doc.name in vps.names() and "# edición 2" in vps.body(doc.name):
            break
        time.sleep(0.2)
    else:
        log = (laptop.root / "data" / "sync.log")
        pytest.fail(f"no llegó al vps; sync.log:\n{log.read_text() if log.exists() else '(vacío)'}")


# ──────────────────────────────────────────────────────────────── versions

def _vps_says(monkeypatch, **fields):
    """The vps greets as another cairn would: `version=None` leaves the field out."""
    from cairn import sync
    real = sync._hello

    def hello(store, **extra):
        msg = real(store, **extra)
        if store.config.node == "vps":
            msg.update(fields)
            msg = {k: v for k, v in msg.items() if v is not None}
        return msg
    monkeypatch.setattr(sync, "_hello", hello)


def test_same_version_no_notice(pair):
    res, srv = run_sync(*pair)
    assert res.ok and res.notices == [] and srv.notices == []


def test_another_version_syncs_with_a_notice(pair, monkeypatch):
    from cairn import __version__
    laptop, vps = pair
    vps.new("demo", "desde_vps")
    _vps_says(monkeypatch, version="0.4.9")
    res, _ = run_sync(laptop, vps)
    assert res.ok and res.received == 1
    assert res.notices == [f"vps runs cairn 0.4.9, this node {__version__}: upgrade the older one"]


def test_a_greeting_without_version_is_older(pair, monkeypatch):
    _vps_says(monkeypatch, version=None)
    res, _ = run_sync(*pair)
    assert res.ok and "vps runs a cairn older than 0.5.0" in res.notices[0]


OLD_PEER = """\
import os, sys
# like FAKE_SSH, with a peer from before the version went in the greeting
os.environ.update({k[len("PEER_"):]: v for k, v in os.environ.items() if k.startswith("PEER_")})
from cairn import cli, sync
sync._hello = lambda store, **extra: {"cairn": "sync", "v": sync.PROTO, "node": store.config.node, **extra}
sys.exit(cli.main(["sync-peer", "--stdio", "--as", "laptop"]))
"""


def test_cli_shows_the_notice_even_quiet(pair, tmp_path, monkeypatch, capsys):
    laptop, vps = pair
    fake = tmp_path / "fake_ssh.py"
    fake.write_text(OLD_PEER)
    for k, v in vps.env().items():
        monkeypatch.setenv(f"PEER_{k}", v)
    monkeypatch.setenv("CAIRN_SSH", f"{sys.executable} {fake}")
    laptop.use()
    access.remote_add("vps", "ssh://cairn@vps.example")
    assert cli.main(["sync", "--quiet"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "[notice] vps runs a cairn older than 0.5.0" in out.err
