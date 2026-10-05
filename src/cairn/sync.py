"""Sync entre nodos, al estilo git: `cairn sync` abre `ssh <remoto> cairn sync-peer --stdio` y los
dos lados se pasan los eventos que le faltan al otro.

Protocolo: un objeto JSON por línea, estrictamente por turnos (nunca escriben los dos a la vez):

    cliente                                   servidor
    {"cairn": "sync", "v": 1, "node": …}  →
                                          ←   {"cairn": "sync", "v": 1, "node": …}  | {"error": …}
    {"inventory": …}                      →
                                          ←   {"inventory": …}
    {"chunk": …} … {"end": true}          →
                                          ←   {"result": …}
                                          ←   {"chunk": …} … {"end": true}

El inventario es `{nodo: {segmento: [tamaño, sha256]}}`. Como cada nodo es el único que escribe
su log, la copia que tiene otro es siempre un prefijo del original: alcanza con mandar los bytes
desde el tamaño que tiene el otro, después de comprobar (con el hash) que de verdad es un prefijo.

Lo que llega se valida antes de escribir nada: ningún nodo acepta datos de su propio log, ni de
un nodo desconocido o revocado, ni eventos sin firma válida. Una línea que no se puede leer
(una escritura cortada en el nodo de origen) se copia igual, para que los logs sigan siendo
prefijos, y la proyección la ignora.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cairn import config
from cairn.events import Event, EventError
from cairn.store import CairnError, Store

PROTO = 1


class ProtocolError(CairnError):
    pass


@dataclass
class Result:
    remote: str
    peer: str = ""
    sent: int = 0           # eventos que el otro aceptó
    received: int = 0       # eventos que aceptó este nodo
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ──────────────────────────────────────────────────────────────── canal

class Channel:
    """Mensajes JSON de a una línea sobre un par de streams binarios."""

    def __init__(self, rfile, wfile):
        self.rfile, self.wfile = rfile, wfile

    def send(self, msg: dict) -> None:
        self.wfile.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        self.wfile.flush()

    def recv(self) -> dict:
        line = self.rfile.readline()
        if not line:
            raise ProtocolError("el otro lado cortó la conexión")
        try:
            msg = json.loads(line)
        except ValueError:
            raise ProtocolError(f"mensaje que no es JSON: {line[:80]!r}") from None
        if not isinstance(msg, dict):
            raise ProtocolError("mensaje que no es un objeto")
        if "error" in msg:
            raise ProtocolError(f"el otro lado: {msg['error']}")
        return msg

    def expect(self, key: str):
        msg = self.recv()
        if key not in msg:
            raise ProtocolError(f"se esperaba `{key}` y llegó {sorted(msg)}")
        return msg[key]


def _hello(store: Store) -> dict:
    return {"cairn": "sync", "v": PROTO, "node": store.config.node}


def _check_hello(msg: dict) -> str:
    if msg.get("cairn") != "sync" or msg.get("v") != PROTO:
        raise ProtocolError(f"protocolo incompatible: {msg}")
    node = msg.get("node")
    if not isinstance(node, str) or not node:
        raise ProtocolError("saludo sin nombre de nodo")
    return node


# ──────────────────────────────────────────────────────────────── qué mandar, qué aceptar

def chunks_for(store: Store, theirs: dict) -> tuple[list[dict], list[str]]:
    """Los bloques que al otro le faltan según su inventario. (bloques, conflictos)."""
    out, conflicts = [], []
    if not isinstance(theirs, dict):
        raise ProtocolError("inventario inválido")
    for node in store.log.nodes():
        if node == theirs.get("__self__"):
            continue
        for seg in store.log.segments(node):
            data = store.log.read_bytes(node, seg)
            data = data[:data.rfind(b"\n") + 1]
            size, digest = (theirs.get(node) or {}).get(seg) or [0, hashlib.sha256(b"").hexdigest()]
            if not isinstance(size, int) or size < 0 or size >= len(data):
                continue                        # lo tiene todo (o más: lo manda el otro)
            if hashlib.sha256(data[:size]).hexdigest() != digest:
                conflicts.append(f"{node}/{seg}: la copia del otro lado no es un prefijo de esta "
                                 "(logs distintos con el mismo nombre de nodo)")
                continue
            out.append({"node": node, "segment": seg, "offset": size,
                        "data": base64.b64encode(data[size:]).decode("ascii")})
    return out, conflicts


def _validate(store: Store, node: str, data: bytes) -> int:
    """Cuántos eventos válidos trae el bloque; ProtocolError si algo no se acepta."""
    peer = store.config.peers.get(node)
    if node == store.config.node:
        raise ProtocolError(f"datos para el log de este mismo nodo ({node}): sólo lo escribe él")
    if peer is None:
        raise ProtocolError(f"eventos de {node}, que no está autorizado acá: `cairn authorize {node} "
                            "<clave> --no-ssh`")
    if peer.revoked:
        raise ProtocolError(f"eventos nuevos de {node}, revocado el {peer.revoked}")
    n = 0
    for raw in data.split(b"\n")[:-1]:
        if not raw.strip():
            continue
        try:
            ev = Event.from_json(raw.decode("utf-8"))
        except (EventError, UnicodeDecodeError):
            continue                            # línea rota en el origen: se copia, no se proyecta
        if ev.node != node:
            raise ProtocolError(f"evento de {ev.node} en el log de {node}")
        if why := store.verifier.problem(ev):
            raise ProtocolError(why)
        n += 1
    return n


def receive(store: Store, chunks: list[dict]) -> tuple[int, list[str]]:
    """Valida y agrega los bloques. Los que no se aceptan se reportan; los demás entran."""
    accepted, errors = 0, []
    with store.receiving() as log:
        for c in chunks:
            try:
                node, seg, offset = c["node"], c["segment"], c["offset"]
                data = base64.b64decode(c["data"], validate=True)
                if not isinstance(offset, int):
                    raise ProtocolError("offset inválido")
                n = _validate(store, node, data)
                log.append_raw(node, seg, offset, data)
                accepted += n
            except (KeyError, TypeError, ValueError) as e:
                errors.append(f"bloque inválido: {e}")
            except (ProtocolError, EventError) as e:
                errors.append(f"{c.get('node')}/{c.get('segment')}: {e}")
    return accepted, errors


def _send_chunks(ch: Channel, chunks: list[dict]) -> None:
    for c in chunks:
        ch.send({"chunk": c})
    ch.send({"end": True})


def _recv_chunks(ch: Channel) -> list[dict]:
    out = []
    while True:
        msg = ch.recv()
        if msg.get("end"):
            return out
        if "chunk" not in msg:
            raise ProtocolError(f"se esperaba un bloque y llegó {sorted(msg)}")
        out.append(msg["chunk"])


def _inventory(store: Store) -> dict:
    return {**store.log.inventory(), "__self__": store.config.node}


# ──────────────────────────────────────────────────────────────── los dos lados

def serve(store: Store, ch: Channel, as_node: str | None = None) -> Result:
    """El lado `sync-peer`. `as_node`: la identidad que fijó `authorized_keys`."""
    res = Result(remote="")
    try:
        peer = _check_hello(ch.recv())
        known = store.config.peers.get(peer)
        if as_node and peer != as_node:
            raise ProtocolError(f"la clave es de {as_node} y el saludo dice {peer}")
        if known is None or known.revoked:
            raise ProtocolError(f"{peer} no está autorizado en {store.config.node}")
        res.peer = res.remote = peer
    except ProtocolError as e:
        ch.send({"error": str(e)})
        res.errors.append(str(e))
        return res
    ch.send(_hello(store))
    theirs = ch.expect("inventory")
    ch.send({"inventory": _inventory(store)})
    res.received, errors = receive(store, _recv_chunks(ch))
    res.errors += errors
    chunks, conflicts = chunks_for(store, theirs)
    ch.send({"result": {"accepted": res.received, "errors": errors + conflicts}})
    _send_chunks(ch, chunks)
    res.errors += conflicts
    return res


def client(store: Store, ch: Channel, name: str) -> Result:
    res = Result(remote=name)
    ch.send(_hello(store))
    peer = _check_hello(ch.recv())
    res.peer = peer
    if peer == store.config.node:
        raise ProtocolError(f"el remoto {name} es este mismo nodo ({peer})")
    if peer not in store.config.peers:
        raise ProtocolError(f"el remoto {name} es el nodo {peer}, que no está autorizado acá: "
                            f"`cairn authorize {peer} <su clave> --no-ssh`")
    ch.send({"inventory": _inventory(store)})
    theirs = ch.expect("inventory")
    chunks, conflicts = chunks_for(store, theirs)
    res.errors += conflicts
    _send_chunks(ch, chunks)
    their = ch.expect("result")
    res.sent = int(their.get("accepted") or 0)
    res.errors += [f"{peer} rechazó: {e}" for e in their.get("errors") or []]
    res.received, errors = receive(store, _recv_chunks(ch))
    res.errors += errors
    return res


# ──────────────────────────────────────────────────────────────── transporte ssh

def ssh_command(cfg: config.Config, remote: config.Remote) -> list[str]:
    from cairn.access import parse_url
    dest, port = parse_url(remote.url)
    ssh = shlex.split(os.environ.get("CAIRN_SSH") or "ssh")
    opts = ["-i", str(remote.key or cfg.key_path), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10"]
    if port:
        opts += ["-p", port]
    return [*ssh, *opts, dest, *shlex.split(remote.command or "cairn"), "sync-peer", "--stdio"]


def sync_remote(store: Store, remote: config.Remote, timeout: float | None = None) -> Result:
    argv = ssh_command(store.config, remote)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except OSError as e:
        return Result(remote.name, errors=[f"no se pudo ejecutar {argv[0]}: {e}"])
    killed = threading.Event()

    def kill():
        killed.set()
        proc.kill()
    timer = threading.Timer(timeout, kill) if timeout else None
    if timer:
        timer.start()
    try:
        res = client(store, Channel(proc.stdout, proc.stdin), remote.name)
    except (ProtocolError, OSError) as e:
        res = Result(remote.name, errors=[str(e)])
    finally:
        if timer:
            timer.cancel()
        for f in (proc.stdin, proc.stdout):
            with contextlib.suppress(OSError):
                f.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        proc.stderr.close()
    if killed.is_set():
        res.errors = [f"se cortó por tiempo ({timeout}s)"]
    elif res.errors and err:
        # si ssh no pudo conectar, su mensaje dice más que "el otro lado cortó"
        res.errors = [e for e in res.errors if "cortó la conexión" not in e]
        res.errors.append(err.splitlines()[-1])
    return res


def sync_all(store: Store, names: list[str] | None = None, timeout: float | None = None) -> list[Result]:
    remotes = store.config.remotes
    if names:
        missing = [n for n in names if n not in remotes]
        if missing:
            raise CairnError(f"No hay remoto(s): {', '.join(missing)}")
        remotes = {n: remotes[n] for n in names}
    return [sync_remote(store, r, timeout) for r in remotes.values()]


def serve_stdio(as_node: str | None = None) -> int:
    with Store(config.load()) as store:
        res = serve(store, Channel(sys.stdin.buffer, sys.stdout.buffer), as_node)
    for e in res.errors:
        print(f"cairn sync-peer: {e}", file=sys.stderr)
    return 0 if res.ok else 1


# ──────────────────────────────────────────────────────────────── disparadores

def auto_enabled(cfg: config.Config) -> bool:
    return bool(cfg.remotes) and cfg.sync_auto and not os.environ.get("CAIRN_NO_AUTOSYNC")


def trigger(cfg: config.Config) -> bool:
    """Lanza `cairn sync` en segundo plano (después de crear/cerrar un doc o crear/tomar un
    handoff: en un nodo efímero, lo que no se empuja se puede perder). La salida va a
    `<datos>/sync.log`. True si lo lanzó."""
    if not auto_enabled(cfg):
        return False
    from cairn.integrate import default_command
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.data_dir / "sync.log", "ab") as log:
        subprocess.Popen([*default_command(), "sync", "--quiet"], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True,
                         cwd=str(Path.home()))
    return True
