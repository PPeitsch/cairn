"""Sync entre nodos aislados: cada uno con su config, datos y clave en un temporal.

El servidor corre en un hilo y los dos lados hablan por pipes del sistema operativo, igual que
por ssh. Al final, una prueba con procesos de verdad: `cairn sync` lanza un "ssh" falso
(`CAIRN_SSH`) que corre `cairn sync-peer --stdio --as <nodo>` con la config del otro nodo.
"""

import json
import os
import sys
import threading

import pytest

from cairn import access, cli, config, docs, edit
from cairn.store import Store
from cairn.sync import Channel, ProtocolError, client, serve

NOW = "2026-03-10T12:00:00+00:00"


class Node:
    def __init__(self, name, root, monkeypatch):
        self.name, self.root, self.mp = name, root / name, monkeypatch
        (self.root / "projects").mkdir(parents=True)
        self.use()
        config.write(name)
        with self.store() as s:
            self.pub = s.public_line()

    def env(self) -> dict:
        return {"CAIRN_CONFIG_DIR": str(self.root / "config"), "CAIRN_DATA_DIR": str(self.root / "data"),
                "CAIRN_PROJECTS": str(self.root / "projects"), "CAIRN_NOW": NOW,
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

    def new(self, area, tema, tipo="plan"):
        with self.store() as s:
            return docs.new(s, area, tema, tipo, "demo").doc

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
    """laptop y vps, que se conocen; laptop se conecta al vps."""
    laptop, vps = nodes("laptop"), nodes("vps")
    vps.trust(laptop, ssh=True)
    laptop.trust(vps)
    return laptop, vps


def run_sync(cli_node, srv_node, as_node=None):
    """Una sesión de sync: devuelve (resultado del cliente, resultado del servidor)."""
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


def test_intercambio_en_los_dos_sentidos(pair):
    laptop, vps = pair
    laptop.new("demo", "desde_laptop")
    vps.new("demo", "desde_vps")
    res, srv = run_sync(laptop, vps)
    assert res.ok and srv.ok, (res.errors, srv.errors)
    assert (res.sent, res.received) == (1, 1)
    assert laptop.names() == vps.names() == {"2026-03-10_demo_desde_laptop", "2026-03-10_demo_desde_vps"}
    res, _ = run_sync(laptop, vps)                    # ya están iguales: no viaja nada
    assert (res.sent, res.received) == (0, 0)


def test_la_cadena_sigue_entre_nodos(pair):
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


def test_divergencia_entre_nodos_queda_igual_en_los_dos(pair):
    laptop, vps = pair
    doc = laptop.new("demo", "plan")
    run_sync(laptop, vps)
    with laptop.store() as s:
        edit.apply(s, doc.name, body="# laptop\n")
    with vps.store() as s:
        vps.mp.setenv("CAIRN_NOW", "2026-03-10T13:00:00+00:00")   # el del vps es posterior
        edit.apply(s, doc.name, body="# vps\n")
    run_sync(laptop, vps)
    for n in (laptop, vps):
        with n.store() as s:
            d = docs.find(s, doc.name)
            assert d.divergent and d.body == "# laptop\n"     # gana la rama más vieja


def test_relay_y_nodos_desconocidos(pair, nodes):
    laptop, vps = pair
    aws = nodes("aws")
    vps.trust(aws, ssh=True)
    aws.trust(vps)
    aws.new("demo", "desde_aws")
    assert run_sync(aws, vps)[0].ok
    res, _ = run_sync(laptop, vps)                    # laptop no conoce a aws
    assert not res.ok and any("aws" in e and "no está autorizado" in e for e in res.errors)
    assert "2026-03-10_demo_desde_aws" not in laptop.names()
    laptop.trust(aws)
    res, _ = run_sync(laptop, vps)
    assert res.ok and "2026-03-10_demo_desde_aws" in laptop.names()


def test_cliente_no_autorizado(pair, nodes):
    _, vps = pair
    intruso = nodes("intruso")
    intruso.trust(vps)
    res, srv = run_sync(intruso, vps)
    assert isinstance(res, ProtocolError) and "no está autorizado en vps" in str(res)
    assert not srv.ok


def test_identidad_fijada_por_authorized_keys(pair):
    laptop, vps = pair
    res, _ = run_sync(laptop, vps, as_node="otro")
    assert isinstance(res, ProtocolError) and "la clave es de otro" in str(res)
    res, _ = run_sync(laptop, vps, as_node="laptop")
    assert res.ok


def test_nadie_escribe_el_log_de_otro_nodo(pair):
    laptop, vps = pair
    laptop.new("demo", "x")
    run_sync(laptop, vps)
    # un vps comprometido agrega un evento al log de laptop (firmado con su propia clave)
    with vps.store() as s:
        seg = s.config.logs_dir / "laptop" / "000001.jsonl"
        ev = json.loads(seg.read_text().splitlines()[0])
        ev["payload"]["name"] = "inyectado"
    with open(seg, "a") as f:
        f.write(json.dumps(ev) + "\n")
    res, _ = run_sync(laptop, vps)
    assert res.ok and res.received == 0               # el servidor no le manda a nadie su propio log
    assert "inyectado" not in laptop.names()
    # y si un servidor malicioso lo mandara igual, el receptor lo rechaza
    import base64

    from cairn.sync import receive
    with laptop.store() as s:
        size = (s.config.logs_dir / "laptop" / "000001.jsonl").stat().st_size
        n, errors = receive(s, [{"node": "laptop", "segment": "000001", "offset": size,
                                 "data": base64.b64encode(json.dumps(ev).encode() + b"\n").decode()}])
    assert n == 0 and any("sólo lo escribe él" in e for e in errors)
    assert "inyectado" not in laptop.names()


def test_evento_falsificado_se_rechaza(pair, nodes):
    laptop, vps = pair
    aws = nodes("aws")
    for n in (laptop, vps):
        n.trust(aws)
    aws.new("demo", "legitimo")
    # el vps fabrica un log de aws con un evento firmado con otra clave
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
    assert any("firma inválida" in e for e in res.errors)
    assert "2026-03-10_demo_legitimo" not in laptop.names()


def test_revocado(pair):
    laptop, vps = pair
    vps.use()
    access.revoke("laptop")
    res, _ = run_sync(laptop, vps)
    assert isinstance(res, ProtocolError) and "no está autorizado en vps" in str(res)


def test_copias_que_no_son_prefijo(pair):
    laptop, vps = pair
    laptop.new("demo", "x")
    run_sync(laptop, vps)
    seg = vps.root / "data" / "logs" / "laptop" / "000001.jsonl"
    data = bytearray(seg.read_bytes())
    data[10] = ord("X") if data[10] != ord("X") else ord("Y")
    seg.write_bytes(bytes(data))
    laptop.new("demo", "y")
    res, _ = run_sync(laptop, vps)
    assert any("no es un prefijo" in e for e in res.errors)


def test_linea_rota_en_el_origen_se_copia_sin_proyectar(pair):
    laptop, vps = pair
    laptop.new("demo", "x")
    with open(laptop.root / "data" / "logs" / "laptop" / "000001.jsonl", "ab") as f:
        f.write(b'{"cortado\n')
    laptop.new("demo", "y")
    res, _ = run_sync(laptop, vps)
    assert res.ok and res.sent == 2
    assert vps.names() == laptop.names()


# ──────────────────────────────────────────────────────────────── authorized_keys

def test_authorize_y_revoke_en_authorized_keys(nodes):
    laptop, vps = nodes("laptop"), nodes("vps")
    ak = vps.root / "authorized_keys"
    ak.write_text("ssh-ed25519 AAAAotra usuario@maquina\n")
    vps.trust(laptop, ssh=True)
    vps.trust(laptop, ssh=True)                       # idempotente
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


def test_authorize_otra_clave_para_el_mismo_nodo(nodes):
    laptop, vps, otro = nodes("laptop"), nodes("vps"), nodes("otro")
    vps.trust(laptop)
    vps.use()
    with pytest.raises(cli.CairnError, match="otra clave"):
        access.authorize("laptop", otro.pub, ssh=False)
    with pytest.raises(cli.CairnError, match="este mismo nodo"):
        access.authorize("vps", otro.pub)


def test_remotos(nodes):
    nodes("laptop")
    access.remote_add("vps", "ssh://cairn@host:2222")
    assert config.load().remotes["vps"].url == "ssh://cairn@host:2222"
    with pytest.raises(cli.CairnError, match="URL de remoto inválida"):
        access.remote_add("x", "cairn@host")
    access.remote_remove("vps")
    assert config.load().remotes == {}


# ──────────────────────────────────────────────────────────────── procesos de verdad

FAKE_SSH = """\
import os, sys
# como ssh con la clave restringida: ignora lo que pide el cliente y corre el comando forzado
env = dict(os.environ, **{k[len("PEER_"):]: v for k, v in os.environ.items() if k.startswith("PEER_")})
os.execvpe(sys.executable, [sys.executable, "-m", "cairn", "sync-peer", "--stdio", "--as", "laptop"], env)
"""


def test_cli_sync_por_ssh_falso(pair, tmp_path, monkeypatch, capsys):
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
    assert "vps (vps): 1 enviado(s), 1 recibido(s)" in capsys.readouterr().out
    assert laptop.names() == vps.names()
    laptop.use()
    assert cli.main(["sync", "--quiet"]) == 0
    assert capsys.readouterr().out == ""
