"""Who may sync with this node, and which nodes this node syncs with.

- `authorize <node> <key>`: trusts the node's key (its events are accepted) and, unless
  `--no-ssh`, gives it ssh access in `authorized_keys`, restricted to a single command:

      command="<cairn> sync-peer --stdio --as <node>",restrict ssh-ed25519 AAAA… cairn:<node>

  `restrict` removes pty, forwarding and everything else; `--as` pins the identity of whoever
  connects, so it cannot pose as another node. A node that never connects here (its events
  arrive through another one) is authorized with `--no-ssh`: only trust in its key.
- `authorize --replace`: the node changed its key; the previous one goes to
  `[peers.<node>.old]` and verifies only what was signed before the change. `key --rotate` is
  the change on the node's side.
- `revoke <node>`: removes its ssh access and stops accepting new events from it (those already
  here remain valid: they are verified with the key, which is marked as revoked).
- `remote add|remove`: the nodes this one syncs with (`cairn sync`).

`CAIRN_AUTHORIZED_KEYS` changes the path of `~/.ssh/authorized_keys` (for the tests).
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
        raise CairnError(f"The cairn command cannot contain quotes or backslashes: {cmd}")
    return f'command="{cmd}",restrict {keys.normalize_public(pub)} cairn:{node}'


def _is_cairn_line(line: str, node: str | None = None) -> bool:
    m = _MARK_RE.search(line)
    return bool(m) and "sync-peer" in line and (node is None or m.group(1) == node)


def _rewrite_authorized_keys(fn) -> bool:
    """Rewrites `authorized_keys` (atomically, 0600, folder 0700) with `fn(lines) -> lines`.
    True if it changed."""
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
        raise CairnError(f"Invalid node name: {node!r}")
    if node == own:
        raise CairnError(f"{node} is this very node")


def authorize(node: str, pub: str, ssh: bool = True, command: list[str] | None = None,
              replace: bool = False) -> list[str]:
    """Returns what it did. If the node already had another key, `replace` moves it to its
    previous keys: it keeps verifying what it signed up to now and nothing later. If the node
    was revoked, the old key is kept too, but only for what predates the revocation."""
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
            raise CairnError(f"{node} is already authorized with another key. If it changed it (`cairn "
                             f"key --rotate`, or it was reinstalled), `cairn authorize {node} <key> "
                             "--replace`: the previous one keeps verifying what it already signed.")
        # revoked: `revoked` is a date; what was signed until the end of that day is valid
        until = f"{prev.revoked}T23:59:59.999999+00:00" if prev.revoked else clock.timestamp()
        old.append(config.OldKey(prev.key, until))
        done.append(f"the previous key of {node} only verifies what came before {until[:19]}")

    def mutate(raw):
        entry = {"key": key, "ssh": "true" if ssh else "false"}
        if old:
            entry["old"] = config.old_keys_table(old)
        raw.setdefault("peers", {})[node] = entry
    config.update(mutate)
    done.insert(0, f"trusting the key of {node}: {key[:32]}…")
    if ssh:
        from cairn.integrate import default_command
        line = authorized_line(command or default_command(), node, key)

        def add(lines):
            return [ln for ln in lines if not _is_cairn_line(ln, node)] + [line]
        if _rewrite_authorized_keys(add):
            done.append(f"ssh access in {authorized_keys_path()} (only `sync-peer --as {node}`)")
        else:
            done.append(f"the ssh access was already in {authorized_keys_path()}")
    else:
        if _rewrite_authorized_keys(lambda lines: [ln for ln in lines if not _is_cairn_line(ln, node)]):
            done.append(f"removed the ssh access of {node}")
    return done


def rotate_key(store) -> list[str]:
    """Changes this node's key (both the signing one and the ssh one). The previous one moves to
    `[node.old]`: what was already signed with it stays valid here. Peers have to accept the new
    one with `authorize <node> <key> --replace`; until then they reject what it signs."""
    cfg = store.config
    old_line = store.public_line()
    until = clock.timestamp()
    path = cfg.key_path
    tmp = path.with_name("new_" + path.name)       # its .pub: new_node_key.pub
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
    return [f"new key of {cfg.node}: {line}",
            f"the previous one stays in [node.old] for what was signed until {until[:19]}",
            f"on every node that trusts this one: `cairn authorize {cfg.node} '{line}' --replace`"]


def revoke(node: str) -> list[str]:
    cfg = config.load()
    _check_node(node, cfg.node)
    done = []
    if node in cfg.peers and not cfg.peers[node].revoked:
        def mutate(raw):
            raw["peers"][node].update({"ssh": "false", "revoked": clock.today()})
        config.update(mutate)
        done.append(f"{node}: revoked (its new events are rejected; the ones already here stay)")
    if _rewrite_authorized_keys(lambda lines: [ln for ln in lines if not _is_cairn_line(ln, node)]):
        done.append(f"{node}: removed from {authorized_keys_path()}")
    if not done:
        raise CairnError(f"{node} is not authorized")
    return done


def ssh_authorized_nodes() -> list[str]:
    path = authorized_keys_path()
    if not path.exists():
        return []
    return sorted({m.group(1) for ln in path.read_text(encoding="utf-8").splitlines()
                   if _is_cairn_line(ln) and (m := _MARK_RE.search(ln))})


# ──────────────────────────────────────────────────────────────── remotes

def parse_url(url: str) -> tuple[str, str | None]:
    """`ssh://[user@]host[:port]` → (ssh destination, port)."""
    u = urlsplit(url)
    if u.scheme != "ssh" or not u.hostname or u.path not in ("", "/"):
        raise CairnError(f"Invalid remote URL: {url!r} (expected ssh://user@host[:port])")
    dest = f"{u.username}@{u.hostname}" if u.username else u.hostname
    return dest, str(u.port) if u.port else None


def remote_add(name: str, url: str, key: str | None = None, command: str | None = None) -> config.Config:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise CairnError(f"Invalid remote name: {name!r}")
    parse_url(url)
    entry = {"url": url}
    if key:
        entry["key"] = key
    if command:
        entry["command"] = command
    return config.update(lambda raw: raw.setdefault("remotes", {}).__setitem__(name, entry))


def remote_remove(name: str) -> config.Config:
    if name not in config.load().remotes:
        raise CairnError(f"There is no remote {name}")
    return config.update(lambda raw: raw["remotes"].pop(name))
