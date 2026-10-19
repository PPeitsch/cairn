"""Node configuration and paths.

    ~/.config/cairn/config.toml     config: node, data, projects tree, author, remotes,
                                    authorized peers and sync
    ~/.local/share/cairn/           data: logs/, the SQLite database and the node key

    [node]           name
    [node.old.<n>]   key, until: previous keys of this node (`key --rotate`, `sync --restore`)
    [data]           path
    [docs]           projects, author (default: `git config user.name`, or the system user),
                     handoff_stale_days (default 14), templates (folder with custom templates:
                     bug.md, infra.md, plan.md, ref.md, ref_header.md, handoff.md),
                     file_dirs (default "scripts, validations"), prune (folders that are not
                     walked), max_depth (default 4)
    [mcp]            instructions = file with the server instructions (replaces the built-in
                     ones; `cairn defaults <folder>` exports them as a starting point),
                     show_max_chars (default 8000: cairn_show returns a longer doc as an outline),
                     search_max_docs (default 15) and search_max_lines (default 3): the cap on
                     what cairn_search returns with a regex
    [claude_code]    mcp_name (default "cairn"), start_timeout (default 30), end_timeout (10)
    [sync]           auto = "true" | "false"   (background sync and sync in the hooks; default
                     true), timeout (seconds per remote, default 60), hook_timeout (the pull in
                     the handoffs hook, default 15), debounce (seconds the sync triggered by a
                     write waits, to batch a burst into a single trip; default 5), interval
                     (minutes between syncs of the `integrate systemd` timer, default 15)
    [remotes.<n>]    url = "ssh://user@host[:port]", key (optional), command (optional)
    [peers.<node>]   key = "ssh-ed25519 AAAA…", ssh = "true" | "false", revoked = "<date>"
    [peers.<node>.old.<n>]  key, until: previous keys (`authorize --replace`); each one
                     verifies only the events from before `until`

Environment variables (mostly for the tests):

    CAIRN_CONFIG_DIR   config folder (default: $XDG_CONFIG_HOME/cairn or ~/.config/cairn)
    CAIRN_DATA_DIR     data folder; overrides the config's
                       (default: $XDG_DATA_HOME/cairn or ~/.local/share/cairn)
    CAIRN_PROJECTS     projects root; overrides the config's (default: ~/projects)
"""

from __future__ import annotations

import getpass
import os
import re
import socket
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cairn.events import NODE_RE

CONFIG_NAME = "config.toml"
DEFAULT_FILE_DIRS = ("scripts", "validations")
DEFAULT_PRUNE = ("node_modules", ".git", "venv", ".venv", "__pycache__", ".tox", ".mypy_cache",
                 "site-packages")
_HEADER = "# Configuración de cairn para este nodo. Ver `cairn --help`.\n\n"


class ConfigError(Exception):
    pass


def _xdg(var: str, fallback: str) -> Path:
    v = os.environ.get(var)
    return Path(v) if v else Path.home() / fallback


def config_dir() -> Path:
    v = os.environ.get("CAIRN_CONFIG_DIR")
    return Path(v) if v else _xdg("XDG_CONFIG_HOME", ".config") / "cairn"


def config_path() -> Path:
    return config_dir() / CONFIG_NAME


def default_data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "cairn"


def default_author() -> str:
    """Who signs the docs if the config does not say: git's `user.name`, or the system user."""
    try:
        out = subprocess.run(["git", "config", "--get", "user.name"], capture_output=True, text=True,
                             timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "autor"


def default_node_name() -> str:
    """The hostname, turned into a valid node name."""
    name = re.sub(r"[^a-z0-9_-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-_")
    return name if NODE_RE.match(name) else "nodo"


@dataclass(frozen=True)
class Remote:
    name: str
    url: str
    key: Path | None = None        # ssh key; default: the node's
    command: str | None = None     # how cairn is invoked on the other side (default: `cairn`)


@dataclass(frozen=True)
class OldKey:
    """A key the node used before changing it: it verifies only events prior to `until`."""
    key: str                       # `ssh-ed25519 AAAA…`
    until: str                     # timestamp (like the events') of the replacement


@dataclass(frozen=True)
class Peer:
    node: str
    key: str                       # `ssh-ed25519 AAAA…`
    ssh: bool = True               # whether it has ssh access to this node (authorized_keys)
    revoked: str | None = None
    old: tuple[OldKey, ...] = ()


@dataclass(frozen=True)
class Config:
    path: Path              # the config.toml
    node: str
    data_dir: Path
    projects: Path
    author: str
    remotes: dict[str, Remote] = field(default_factory=dict)
    peers: dict[str, Peer] = field(default_factory=dict)
    sync_auto: bool = True
    old_keys: tuple[OldKey, ...] = ()     # the previous keys of this same node
    sync_timeout: float = 60
    sync_hook_timeout: float = 15
    sync_debounce: float = 5
    sync_interval: int = 15
    handoff_stale_days: int = 14
    templates_dir: Path | None = None
    file_dirs: tuple[str, ...] = DEFAULT_FILE_DIRS
    prune: tuple[str, ...] = DEFAULT_PRUNE
    max_depth: int = 4
    mcp_instructions: Path | None = None
    show_max_chars: int = 8000
    search_max_docs: int = 15
    search_max_lines: int = 3
    mcp_name: str = "cairn"
    start_timeout: int = 30
    end_timeout: int = 10

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cairn.sqlite"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "lock"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "node_key"


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


def _table(raw: dict, name: str, path: Path) -> dict:
    t = raw.get(name, {})
    if not isinstance(t, dict):
        raise ConfigError(f"{path}: [{name}] tiene que ser una tabla")
    return t


def load_raw() -> tuple[Path, dict]:
    path = config_path()
    if not path.is_file():
        raise ConfigError(f"cairn no está inicializado en este nodo (falta {path}): correr `cairn init`")
    try:
        return path, tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None


def _old_keys(t, where: str, path: Path) -> tuple[OldKey, ...]:
    """`[<where>.old.<n>]` key, until: the previous keys, in the order they were replaced."""
    if t is None:
        return ()
    if not isinstance(t, dict) or not all(isinstance(v, dict) and v.get("key") and v.get("until")
                                          for v in t.values()):
        raise ConfigError(f"{path}: [{where}.old.<n>] necesita `key` y `until`")
    return tuple(OldKey(str(v["key"]), str(v["until"]))
                 for _, v in sorted(t.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0))


def old_keys_table(old: tuple[OldKey, ...] | list[OldKey]) -> dict:
    return {str(i): {"key": k.key, "until": k.until} for i, k in enumerate(old, 1)}


def _num(t: dict, key: str, default, kind, where: str, path: Path):
    v = t.get(key)
    if v is None or v == "":
        return default
    try:
        n = kind(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{path}: [{where}] {key} tiene que ser un número: {v!r}") from None
    if n <= 0:
        raise ConfigError(f"{path}: [{where}] {key} tiene que ser mayor que 0: {v!r}")
    return n


def _names(t: dict, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """A list as comma-separated text (the config only stores strings) or as a list."""
    v = t.get(key)
    if v is None:
        return default
    items = v if isinstance(v, list) else str(v).split(",")
    return tuple(x.strip() for x in map(str, items) if x.strip())


def load() -> Config:
    path, raw = load_raw()
    node = _table(raw, "node", path).get("name", "")
    if not NODE_RE.match(str(node)):
        raise ConfigError(f"{path}: [node] name inválido: {node!r}")
    data = os.environ.get("CAIRN_DATA_DIR") or _table(raw, "data", path).get("path") \
        or str(default_data_dir())
    docs = _table(raw, "docs", path)
    projects = os.environ.get("CAIRN_PROJECTS") or docs.get("projects") or "~/projects"
    remotes = {}
    for name, r in _table(raw, "remotes", path).items():
        if not isinstance(r, dict) or not r.get("url"):
            raise ConfigError(f"{path}: [remotes.{name}] necesita `url`")
        remotes[name] = Remote(name, str(r["url"]), _expand(r["key"]) if r.get("key") else None,
                               str(r["command"]) if r.get("command") else None)
    peers = {}
    for name, p in _table(raw, "peers", path).items():
        if not NODE_RE.match(name) or not isinstance(p, dict) or not p.get("key"):
            raise ConfigError(f"{path}: [peers.{name}] inválido (nombre de nodo y `key`)")
        peers[name] = Peer(name, str(p["key"]), str(p.get("ssh", "true")) != "false",
                           str(p["revoked"]) if p.get("revoked") else None,
                           _old_keys(p.get("old"), f"peers.{name}", path))
    sync = _table(raw, "sync", path)
    mcp = _table(raw, "mcp", path)
    cc = _table(raw, "claude_code", path)
    auto = str(sync.get("auto", "true")) != "false"
    mcp_name = str(cc.get("mcp_name") or "cairn")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", mcp_name):
        raise ConfigError(f"{path}: [claude_code] mcp_name inválido: {mcp_name!r}")
    return Config(
        path=path, node=node, data_dir=_expand(data), projects=_expand(projects),
        author=str(docs.get("author") or default_author()), remotes=remotes, peers=peers,
        sync_auto=auto, old_keys=_old_keys(_table(raw, "node", path).get("old"), "node", path),
        sync_timeout=_num(sync, "timeout", 60.0, float, "sync", path),
        sync_hook_timeout=_num(sync, "hook_timeout", 15.0, float, "sync", path),
        sync_debounce=_num(sync, "debounce", 5.0, float, "sync", path),
        sync_interval=_num(sync, "interval", 15, int, "sync", path),
        handoff_stale_days=_num(docs, "handoff_stale_days", 14, int, "docs", path),
        templates_dir=_expand(docs["templates"]) if docs.get("templates") else None,
        file_dirs=_names(docs, "file_dirs", DEFAULT_FILE_DIRS),
        prune=_names(docs, "prune", DEFAULT_PRUNE),
        max_depth=_num(docs, "max_depth", 4, int, "docs", path),
        mcp_instructions=_expand(mcp["instructions"]) if mcp.get("instructions") else None,
        show_max_chars=_num(mcp, "show_max_chars", 8000, int, "mcp", path),
        search_max_docs=_num(mcp, "search_max_docs", 15, int, "mcp", path),
        search_max_lines=_num(mcp, "search_max_lines", 3, int, "mcp", path),
        mcp_name=mcp_name,
        start_timeout=_num(cc, "start_timeout", 30, int, "claude_code", path),
        end_timeout=_num(cc, "end_timeout", 10, int, "claude_code", path),
    )


# ──────────────────────────────────────────────────────────────── writing

def _toml_str(v: str) -> str:
    out = v.replace("\\", "\\\\").replace('"', '\\"')
    out = "".join(c if c >= " " or c == "\t" else f"\\u{ord(c):04x}" for c in out)
    return f'"{out}"'


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else _toml_str(k)


def dump_toml(data: dict) -> str:
    """TOML of tables with string values, and tables of tables (`[remotes.vps]`): enough for the
    config, and it avoids a dependency."""
    out = []

    def emit(prefix: str, table: dict) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        subs = {k: v for k, v in table.items() if isinstance(v, dict)}
        if scalars or not subs:
            out.append(f"[{prefix}]")
            out.extend(f"{_toml_key(k)} = {_toml_str(str(v))}" for k, v in scalars.items())
            out.append("")
        for k, v in subs.items():
            emit(f"{prefix}.{_toml_key(k)}", v)

    for table, values in data.items():
        if isinstance(values, dict) and (values or table in ("node", "data", "docs")):
            emit(_toml_key(table), values)
    return "\n".join(out)


def save_raw(raw: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_HEADER + dump_toml(raw), encoding="utf-8")
    os.replace(tmp, path)
    return path


def update(fn) -> Config:
    """Loads the raw config, lets `fn(raw)` modify it and saves it. If the result is not a valid
    config, it goes back to the previous one and says so."""
    path, raw = load_raw()
    before = path.read_text(encoding="utf-8")
    fn(raw)
    save_raw(raw)
    try:
        return load()
    except ConfigError:
        path.write_text(before, encoding="utf-8")
        raise


def write(node: str, data_dir: str | None = None, projects: str | None = None,
          author: str | None = None) -> Path:
    if not NODE_RE.match(node):
        raise ConfigError(f"nombre de nodo inválido: {node!r} (minúsculas, dígitos, `-` y `_`)")
    return save_raw({
        "node": {"name": node},
        "data": {"path": data_dir or str(default_data_dir())},
        "docs": {"projects": projects or "~/projects", "author": author or default_author()},
    })
