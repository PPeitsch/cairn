"""Sync between nodes, git style: `cairn sync` opens `ssh <remote> cairn sync-peer --stdio` and the
two sides send each other the events the other is missing.

Protocol: one JSON object per line, strictly taking turns (both sides never write at once):

    client                                    server
    {"cairn": "sync", "v": 1, "node": …}  →
                                          ←   {"cairn": "sync", "v": 1, "node": …}  | {"error": …}
    {"inventory": …}                      →
                                          ←   {"inventory": …}
    {"chunk": …} … {"end": true}          →
                                          ←   {"result": …}
                                          ←   {"chunk": …} … {"end": true}

The inventory is `{node: {segment: [size, sha256]}}`. Since each node is the only one that writes
its log, the copy another node has is always a prefix of the original: it is enough to send the
bytes from the size the other side has, after checking (with the hash) that it really is a prefix.

What arrives is validated before anything is written: no node accepts data for its own log, nor
from an unknown or revoked node, nor events without a valid signature. A line that cannot be read
(a write cut short on the origin node) is copied anyway, so the logs stay prefixes, and the
projection ignores it.

Restore (`sync --restore <remote>`): a node that lost its data and was reinitialized with the
same name asks for its own log. The hello carries `"restore": true`; the server answers with the
keys it has on record for that node (`"keys": {"key", "old"}`) and also sends the chunks of the
client's log. The client only accepts it if it has no events of its own, if the current key the
server has is its own (if it was reinstalled with a new key, the server must have accepted it
first with `authorize --replace`) and if every event verifies with one of its keys.

Previous keys are not taken from the server on faith: a compromised peer could send a made-up one
and, signed with it, fake old events in this node's name. Every previous key the node does not
already have in its config must match a fingerprint the user passes (`--old-key`, noted down
outside the node while the key was current); otherwise the restore stops before receiving
anything. `--trust-peer-keys` accepts the server's keys without checking them (the old behavior).
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
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
    sent: int = 0           # events the other side accepted
    received: int = 0       # events this node accepted
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ──────────────────────────────────────────────────────────────── channel

class Channel:
    """One-line JSON messages over a pair of binary streams."""

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


def _hello(store: Store, **extra) -> dict:
    return {"cairn": "sync", "v": PROTO, "node": store.config.node, **extra}


def _check_hello(msg: dict) -> str:
    if msg.get("cairn") != "sync" or msg.get("v") != PROTO:
        raise ProtocolError(f"protocolo incompatible: {msg}")
    node = msg.get("node")
    if not isinstance(node, str) or not node:
        raise ProtocolError("saludo sin nombre de nodo")
    return node


# ──────────────────────────────────────────────────────────────── what to send, what to accept

def chunks_for(store: Store, theirs: dict, restore: bool = False) -> tuple[list[dict], list[str]]:
    """The chunks the other side is missing according to its inventory. (chunks, conflicts).
    With `restore` it includes the other side's own log."""
    out, conflicts = [], []
    if not isinstance(theirs, dict):
        raise ProtocolError("inventario inválido")
    for node in store.log.nodes():
        if node == theirs.get("__self__") and not restore:
            continue
        for seg in store.log.segments(node):
            data = store.log.read_bytes(node, seg)
            data = data[:data.rfind(b"\n") + 1]
            size, digest = (theirs.get(node) or {}).get(seg) or [0, hashlib.sha256(b"").hexdigest()]
            if not isinstance(size, int) or size < 0 or size >= len(data):
                continue                        # it has everything (or more: the other side sends it)
            if hashlib.sha256(data[:size]).hexdigest() != digest:
                conflicts.append(f"{node}/{seg}: la copia del otro lado no es un prefijo de esta "
                                 "(logs distintos con el mismo nombre de nodo)")
                continue
            out.append({"node": node, "segment": seg, "offset": size,
                        "data": base64.b64encode(data[size:]).decode("ascii")})
    return out, conflicts


def _validate(store: Store, node: str, data: bytes, restore: bool = False) -> int:
    """How many valid events the chunk carries; ProtocolError if anything is not accepted."""
    peer = store.config.peers.get(node)
    if node == store.config.node:
        if not restore:
            raise ProtocolError(f"datos para el log de este mismo nodo ({node}): sólo lo escribe él")
    elif peer is None:
        raise ProtocolError(f"eventos de {node}, que no está autorizado acá: `cairn authorize {node} "
                            "<clave> --no-ssh`")
    if peer is not None and peer.revoked:
        raise ProtocolError(f"eventos nuevos de {node}, revocado el {peer.revoked}")
    n = 0
    for raw in data.split(b"\n")[:-1]:
        if not raw.strip():
            continue
        try:
            ev = Event.from_json(raw.decode("utf-8"))
        except (EventError, UnicodeDecodeError):
            continue                            # broken line at the origin: copied, not projected
        if ev.node != node:
            raise ProtocolError(f"evento de {ev.node} en el log de {node}")
        if why := store.verifier.problem(ev):
            raise ProtocolError(why)
        n += 1
    return n


def receive(store: Store, chunks: list[dict], restore: bool = False) -> tuple[int, list[str]]:
    """Validates and appends the chunks. Those not accepted are reported; the rest go in. With
    `restore` the own log is accepted (the node lost its data and recovers it from a peer)."""
    accepted, errors = 0, []
    with store.receiving() as log:
        for c in chunks:
            try:
                node, seg, offset = c["node"], c["segment"], c["offset"]
                data = base64.b64decode(c["data"], validate=True)
                if not isinstance(offset, int):
                    raise ProtocolError("offset inválido")
                n = _validate(store, node, data, restore)
                log.append_raw(node, seg, offset, data, own=restore)
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


# ──────────────────────────────────────────────────────────────── the two sides

def serve(store: Store, ch: Channel, as_node: str | None = None) -> Result:
    """The `sync-peer` side. `as_node`: the identity pinned by `authorized_keys`."""
    res = Result(remote="")
    try:
        hello = ch.recv()
        peer = _check_hello(hello)
        restore = hello.get("restore") is True
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
    extra = {"keys": {"key": known.key, "old": [[k.key, k.until] for k in known.old]}} if restore else {}
    ch.send(_hello(store, **extra))
    theirs = ch.expect("inventory")
    ch.send({"inventory": _inventory(store)})
    res.received, errors = receive(store, _recv_chunks(ch))
    res.errors += errors
    chunks, conflicts = chunks_for(store, theirs, restore)
    ch.send({"result": {"accepted": res.received, "errors": errors + conflicts}})
    _send_chunks(ch, chunks)
    res.errors += conflicts
    return res


def _adopt_keys(store: Store, record, peer: str, expected: list[str] | None = None,
                trust_peer: bool = False) -> int:
    """Restore: checks that the peer has this node's current key on record and adopts the
    previous ones it has (to verify what this node signed before), each one against the
    `expected` fingerprints the user passed. Returns how many it added; ProtocolError, without
    touching the config, if any of them cannot be checked."""
    from cairn import keys
    if not isinstance(record, dict) or not isinstance(record.get("key"), str):
        raise ProtocolError(f"{peer} no contestó el restore (¿otra versión de cairn?)")
    mine = keys.normalize_public(store.public_line())
    try:
        theirs = keys.normalize_public(record["key"])
        old = [config.OldKey(keys.normalize_public(k), str(u)) for k, u in record.get("old") or []]
    except (keys.KeyFileError, TypeError, ValueError) as e:
        raise ProtocolError(f"{peer} mandó claves inválidas: {e}") from None
    if theirs != mine:
        raise ProtocolError(f"{peer} tiene otra clave para {store.config.node}: si este nodo se "
                            f"reinstaló, en {peer} `cairn authorize {store.config.node} '<cairn key>' "
                            "--replace` y volver a probar")
    known = {k.key for k in store.config.old_keys}
    new_keys = [k for k in old if k.key not in known and k.key != mine]
    if not trust_peer:
        wanted = set(expected or ())
        offered = {keys.fingerprint(k.key) for k in old} | {keys.fingerprint(k) for k in known}
        offered.add(keys.fingerprint(mine))
        if missing := sorted(wanted - offered):
            raise ProtocolError(f"{peer} no tiene registrada la clave anterior {', '.join(missing)} de "
                                f"{store.config.node}: o la huella está mal anotada, o {peer} no es "
                                "de fiar; no se restauró nada")
        if unchecked := [k for k in new_keys if keys.fingerprint(k.key) not in wanted]:
            listing = "\n".join(f"  {keys.fingerprint(k.key)}  (hasta {k.until[:19]})"
                                for k in unchecked)
            raise ProtocolError(
                f"{peer} dice que {store.config.node} firmó antes con estas claves:\n{listing}\n"
                "Compararlas con las huellas que anotaste y pasarlas con `--old-key <huella>` "
                "(una por clave). Si alguna no es tuya, no restaurar desde este par. Sin las "
                "huellas, `--trust-peer-keys` las acepta sin control. No se restauró nada.")
    if new_keys:
        merged = [*store.config.old_keys, *new_keys]
        store.config = config.update(lambda raw: raw.setdefault("node", {}).update(
            {"old": config.old_keys_table(merged)}))
        store.verifier = type(store.verifier)(store.config, store.key.public_key())
    return len(new_keys)


def client(store: Store, ch: Channel, name: str, restore: bool = False,
           old_keys: list[str] | None = None, trust_peer_keys: bool = False) -> Result:
    res = Result(remote=name)
    if restore and store.log.segments(store.config.node):
        raise ProtocolError(f"{store.config.node} ya tiene eventos propios: el restore es para un nodo "
                            "que perdió sus datos y se reinicializó")
    ch.send(_hello(store, **({"restore": True} if restore else {})))
    hello = ch.recv()
    peer = _check_hello(hello)
    res.peer = peer
    if peer == store.config.node:
        raise ProtocolError(f"el remoto {name} es este mismo nodo ({peer})")
    if peer not in store.config.peers:
        raise ProtocolError(f"el remoto {name} es el nodo {peer}, que no está autorizado acá: "
                            f"`cairn authorize {peer} <su clave> --no-ssh`")
    if restore:
        _adopt_keys(store, hello.get("keys"), peer, old_keys, trust_peer_keys)
    ch.send({"inventory": _inventory(store)})
    theirs = ch.expect("inventory")
    chunks, conflicts = chunks_for(store, theirs)
    res.errors += conflicts
    _send_chunks(ch, chunks)
    their = ch.expect("result")
    res.sent = int(their.get("accepted") or 0)
    res.errors += [f"{peer} rechazó: {e}" for e in their.get("errors") or []]
    res.received, errors = receive(store, _recv_chunks(ch), restore)
    res.errors += errors
    return res


# ──────────────────────────────────────────────────────────────── ssh transport

def ssh_command(cfg: config.Config, remote: config.Remote) -> list[str]:
    from cairn.access import parse_url
    dest, port = parse_url(remote.url)
    ssh = shlex.split(os.environ.get("CAIRN_SSH") or "ssh")
    opts = ["-i", str(remote.key or cfg.key_path), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10"]
    if port:
        opts += ["-p", port]
    return [*ssh, *opts, dest, *shlex.split(remote.command or "cairn"), "sync-peer", "--stdio"]


def sync_remote(store: Store, remote: config.Remote, timeout: float | None = None,
                restore: bool = False, old_keys: list[str] | None = None,
                trust_peer_keys: bool = False) -> Result:
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
        res = client(store, Channel(proc.stdout, proc.stdin), remote.name, restore, old_keys,
                     trust_peer_keys)
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
        # if ssh could not connect, its message says more than "the other side hung up"
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
        try:
            res = serve(store, Channel(sys.stdin.buffer, sys.stdout.buffer), as_node)
        except (ProtocolError, OSError) as e:   # the client hung up halfway (it reports that)
            res = Result(remote="", errors=[str(e)])
    for e in res.errors:
        print(f"cairn sync-peer: {e}", file=sys.stderr)
    return 0 if res.ok else 1


# ──────────────────────────────────────────────────────────────── triggers

def auto_enabled(cfg: config.Config) -> bool:
    return bool(cfg.remotes) and cfg.sync_auto and not os.environ.get("CAIRN_NO_AUTOSYNC")


@contextlib.contextmanager
def _flock(path: Path, blocking: bool):
    """Exclusive lock on `path`; without `blocking`, yields False if another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _wait_path(cfg: config.Config) -> Path:
    return cfg.data_dir / "sync-wait.lock"


def running(cfg: config.Config):
    """One sync at a time on the node (those from writes, the hook's and the timer's)."""
    return _flock(cfg.data_dir / "sync-run.lock", blocking=True)


def wait_turn(cfg: config.Config, delay: float) -> bool:
    """The debounce: the process that takes the wait lock waits `delay` seconds, releases it and
    syncs. While someone is waiting, a new write does not launch another process (see
    `trigger`): the one already waiting covers it, because it reads the log only after releasing
    the lock. False if another process is already waiting."""
    with _flock(_wait_path(cfg), blocking=False) as got:
        if got:
            time.sleep(delay)
        return got


def trigger(cfg: config.Config, delay: float | None = None) -> bool:
    """Launches `cairn sync` in the background after every write (`Store.writer`) and from the
    session-end hook: whatever is not pushed stays only on this node. With `delay` (default
    `[sync] debounce`) it waits before syncing, so a burst of writes travels together; if a
    process is already waiting, it does not launch another. Output goes to `<data>/sync.log`.
    True if it launched it."""
    if not auto_enabled(cfg):
        return False
    delay = cfg.sync_debounce if delay is None else delay
    if delay > 0:
        with _flock(_wait_path(cfg), blocking=False) as free:
            if not free:
                return False
    from cairn.integrate import default_command
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    argv = [*default_command(), "sync", "--quiet"] + (["--after", f"{delay:g}"] if delay > 0 else [])
    with open(cfg.data_dir / "sync.log", "ab") as log:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, cwd=str(Path.home()))
    return True
