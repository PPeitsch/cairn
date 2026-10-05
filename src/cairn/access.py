"""Quién puede sincronizar con este nodo y con quién sincroniza este nodo.

- `authorize <nodo> <clave>`: confía en la clave del nodo (sus eventos se aceptan) y, salvo
  `--no-ssh`, le da acceso por ssh en `authorized_keys`, restringido a un único comando:

      command="<cairn> sync-peer --stdio --as <nodo>",restrict ssh-ed25519 AAAA… cairn:<nodo>

  `restrict` saca pty, forwarding y todo lo demás; `--as` fija la identidad del que entra, así
  no puede hacerse pasar por otro nodo. Un nodo que nunca se conecta acá (sus eventos llegan a
  través de otro) se autoriza con `--no-ssh`: sólo la confianza en su clave.
- `authorize --replace`: el nodo cambió de clave; la anterior queda en `[peers.<nodo>.old]` y
  verifica sólo lo firmado antes del cambio. `key --rotate` es el cambio del lado del nodo.
- `revoke <nodo>`: le saca el acceso por ssh y deja de aceptar eventos nuevos suyos (los que ya
  están siguen valiendo: se verifican con la clave, que queda marcada como revocada).
- `remote add|remove`: los nodos con los que este sincroniza (`cairn sync`).

`CAIRN_AUTHORIZED_KEYS` cambia la ruta de `~/.ssh/authorized_keys` (para los tests).
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from cairn import clock, config, keys
from cairn.events import NODE_RE
from cairn.store import CairnError

_MARK_RE = re.compile(r"\scairn:([a-z0-9][a-z0-9_-]{0,62})\s*$")


def authorized_keys_path() -> Path:
    v = os.environ.get("CAIRN_AUTHORIZED_KEYS")
    return Path(v) if v else Path.home() / ".ssh" / "authorized_keys"


def forced_command(command: list[str], node: str) -> str:
    return shlex.join([*command, "sync-peer", "--stdio", "--as", node])


def authorized_line(command: list[str], node: str, pub: str) -> str:
    cmd = forced_command(command, node)
    if '"' in cmd or "\\" in cmd:
        raise CairnError(f"El comando de cairn no puede tener comillas ni barras invertidas: {cmd}")
    return f'command="{cmd}",restrict {keys.normalize_public(pub)} cairn:{node}'


def _is_cairn_line(line: str, node: str | None = None) -> bool:
    m = _MARK_RE.search(line)
    return bool(m) and "sync-peer" in line and (node is None or m.group(1) == node)


def _rewrite_authorized_keys(fn) -> bool:
    """Reescribe `authorized_keys` (atómico, 0600, carpeta 0700) con `fn(líneas) -> líneas`.
    True si cambió."""
    path = authorized_keys_path()
    old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new = fn(list(old))
    if new == old:
        return False
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".authorized_keys.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in new))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def _check_node(node: str, own: str) -> None:
    if not NODE_RE.match(node):
        raise CairnError(f"Nombre de nodo inválido: {node!r}")
    if node == own:
        raise CairnError(f"{node} es este mismo nodo")


def authorize(node: str, pub: str, ssh: bool = True, command: list[str] | None = None,
              replace: bool = False) -> list[str]:
    """Devuelve lo que hizo. Si el nodo ya tenía otra clave, `replace` la pasa a sus claves
    anteriores: sigue verificando lo que firmó hasta ahora y nada posterior. Si estaba revocado,
    la vieja queda igual, pero sólo para lo anterior a la revocación."""
    cfg = config.load()
    _check_node(node, cfg.node)
    try:
        key = keys.normalize_public(pub)
    except keys.KeyFileError as e:
        raise CairnError(str(e)) from None
    done = []
    prev = cfg.peers.get(node)
    old = list(prev.old) if prev else []
    if prev and prev.key != key:
        if not prev.revoked and not replace:
            raise CairnError(f"{node} ya está autorizado con otra clave. Si la cambió (`cairn key "
                             f"--rotate`, o se reinstaló), `cairn authorize {node} <clave> --replace`: "
                             "la anterior sigue verificando lo que ya firmó.")
        # revocado: `revoked` es una fecha; vale lo firmado hasta el final de ese día
        until = f"{prev.revoked}T23:59:59.999999+00:00" if prev.revoked else clock.timestamp()
        old.append(config.OldKey(prev.key, until))
        done.append(f"la clave anterior de {node} verifica sólo lo de antes de {until[:19]}")

    def mutate(raw):
        entry = {"key": key, "ssh": "true" if ssh else "false"}
        if old:
            entry["old"] = config.old_keys_table(old)
        raw.setdefault("peers", {})[node] = entry
    config.update(mutate)
    done.insert(0, f"confianza en la clave de {node}: {key[:32]}…")
    if ssh:
        from cairn.integrate import default_command
        line = authorized_line(command or default_command(), node, key)

        def add(lines):
            return [ln for ln in lines if not _is_cairn_line(ln, node)] + [line]
        if _rewrite_authorized_keys(add):
            done.append(f"acceso por ssh en {authorized_keys_path()} (sólo `sync-peer --as {node}`)")
        else:
            done.append(f"el acceso por ssh ya estaba en {authorized_keys_path()}")
    else:
        if _rewrite_authorized_keys(lambda lines: [ln for ln in lines if not _is_cairn_line(ln, node)]):
            done.append(f"sacado el acceso por ssh de {node}")
    return done


def rotate_key(store) -> list[str]:
    """Cambia la clave de este nodo (la de firmar y la de ssh). La anterior pasa a
    `[node.old]`: lo ya firmado con ella sigue valiendo acá. Los pares tienen que aceptar la
    nueva con `authorize <nodo> <clave> --replace`; hasta entonces rechazan lo que firme."""
    cfg = store.config
    old_line = store.public_line()
    until = clock.timestamp()
    path = cfg.key_path
    tmp = path.with_name("new_" + path.name)       # su .pub: new_node_key.pub
    tmp.unlink(missing_ok=True)
    tmp.with_suffix(".pub").unlink(missing_ok=True)
    with store._lock():
        new = keys.generate(tmp, cfg.node)

        def mutate(raw):
            node = raw.setdefault("node", {})
            node["old"] = config.old_keys_table([*cfg.old_keys, config.OldKey(old_line, until)])
        config.update(mutate)
        os.replace(tmp.with_suffix(".pub"), path.with_suffix(".pub"))
        os.replace(tmp, path)
        store.config = config.load()
        store.key = new
        store.verifier = type(store.verifier)(store.config, new.public_key())
    line = keys.public_line(new.public_key(), cfg.node)
    return [f"clave nueva de {cfg.node}: {line}",
            f"la anterior queda en [node.old] para lo firmado hasta {until[:19]}",
            f"en cada nodo que confía en este: `cairn authorize {cfg.node} '{line}' --replace`"]


def revoke(node: str) -> list[str]:
    cfg = config.load()
    _check_node(node, cfg.node)
    done = []
    if node in cfg.peers and not cfg.peers[node].revoked:
        def mutate(raw):
            raw["peers"][node].update({"ssh": "false", "revoked": clock.today()})
        config.update(mutate)
        done.append(f"{node}: revocado (sus eventos nuevos se rechazan; los que ya están siguen)")
    if _rewrite_authorized_keys(lambda lines: [ln for ln in lines if not _is_cairn_line(ln, node)]):
        done.append(f"{node}: sacado de {authorized_keys_path()}")
    if not done:
        raise CairnError(f"{node} no está autorizado")
    return done


def ssh_authorized_nodes() -> list[str]:
    path = authorized_keys_path()
    if not path.exists():
        return []
    return sorted({m.group(1) for ln in path.read_text(encoding="utf-8").splitlines()
                   if _is_cairn_line(ln) and (m := _MARK_RE.search(ln))})


# ──────────────────────────────────────────────────────────────── remotos

def parse_url(url: str) -> tuple[str, str | None]:
    """`ssh://[usuario@]host[:puerto]` → (destino para ssh, puerto)."""
    u = urlsplit(url)
    if u.scheme != "ssh" or not u.hostname or u.path not in ("", "/"):
        raise CairnError(f"URL de remoto inválida: {url!r} (se espera ssh://usuario@host[:puerto])")
    dest = f"{u.username}@{u.hostname}" if u.username else u.hostname
    return dest, str(u.port) if u.port else None


def remote_add(name: str, url: str, key: str | None = None, command: str | None = None) -> config.Config:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise CairnError(f"Nombre de remoto inválido: {name!r}")
    parse_url(url)
    entry = {"url": url}
    if key:
        entry["key"] = key
    if command:
        entry["command"] = command
    return config.update(lambda raw: raw.setdefault("remotes", {}).__setitem__(name, entry))


def remote_remove(name: str) -> config.Config:
    if name not in config.load().remotes:
        raise CairnError(f"No hay un remoto {name}")
    return config.update(lambda raw: raw["remotes"].pop(name))
