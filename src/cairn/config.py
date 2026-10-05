"""Configuración del nodo y rutas.

    ~/.config/cairn/config.toml     config: nodo, datos, árbol de proyectos, autor, remotos,
                                    pares autorizados y sync
    ~/.local/share/cairn/           datos: logs/, la base SQLite y la clave del nodo

    [node]           name
    [data]           path
    [docs]           projects, author
    [sync]           auto = "true" | "false"   (sync en segundo plano y en los hooks; default true)
    [remotes.<n>]    url = "ssh://usuario@host[:puerto]", key (opcional), command (opcional)
    [peers.<nodo>]   key = "ssh-ed25519 AAAA…", ssh = "true" | "false", revoked = "<fecha>"

Variables de entorno (sobre todo para los tests):

    CAIRN_CONFIG_DIR   carpeta de la config (default: $XDG_CONFIG_HOME/cairn o ~/.config/cairn)
    CAIRN_DATA_DIR     carpeta de datos; pisa la de la config
                       (default: $XDG_DATA_HOME/cairn o ~/.local/share/cairn)
    CAIRN_PROJECTS     raíz de los proyectos; pisa la de la config (default: ~/projects)
"""

from __future__ import annotations

import os
import re
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cairn.events import NODE_RE

CONFIG_NAME = "config.toml"
DEFAULT_AUTHOR = "Pablo + Claude"
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


def default_node_name() -> str:
    """El hostname, llevado al formato de nombre de nodo."""
    name = re.sub(r"[^a-z0-9_-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-_")
    return name if NODE_RE.match(name) else "nodo"


@dataclass(frozen=True)
class Remote:
    name: str
    url: str
    key: Path | None = None        # clave ssh; default: la del nodo
    command: str | None = None     # cómo se invoca cairn del otro lado (default: `cairn`)


@dataclass(frozen=True)
class Peer:
    node: str
    key: str                       # `ssh-ed25519 AAAA…`
    ssh: bool = True               # si tiene acceso por ssh a este nodo (authorized_keys)
    revoked: str | None = None


@dataclass(frozen=True)
class Config:
    path: Path              # el config.toml
    node: str
    data_dir: Path
    projects: Path
    author: str
    remotes: dict[str, Remote] = field(default_factory=dict)
    peers: dict[str, Peer] = field(default_factory=dict)
    sync_auto: bool = True

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
                           str(p["revoked"]) if p.get("revoked") else None)
    auto = str(_table(raw, "sync", path).get("auto", "true")) != "false"
    return Config(path=path, node=node, data_dir=_expand(data), projects=_expand(projects),
                  author=str(docs.get("author") or DEFAULT_AUTHOR), remotes=remotes, peers=peers,
                  sync_auto=auto)


# ──────────────────────────────────────────────────────────────── escritura

def _toml_str(v: str) -> str:
    out = v.replace("\\", "\\\\").replace('"', '\\"')
    out = "".join(c if c >= " " or c == "\t" else f"\\u{ord(c):04x}" for c in out)
    return f'"{out}"'


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else _toml_str(k)


def dump_toml(data: dict) -> str:
    """TOML de tablas con valores string, y tablas de tablas (`[remotes.vps]`): alcanza para la
    config y evita una dependencia."""
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
    """Carga la config cruda, la deja modificar a `fn(raw)` y la guarda."""
    _, raw = load_raw()
    fn(raw)
    save_raw(raw)
    return load()


def write(node: str, data_dir: str | None = None, projects: str | None = None,
          author: str | None = None) -> Path:
    if not NODE_RE.match(node):
        raise ConfigError(f"nombre de nodo inválido: {node!r} (minúsculas, dígitos, `-` y `_`)")
    return save_raw({
        "node": {"name": node},
        "data": {"path": data_dir or str(default_data_dir())},
        "docs": {"projects": projects or "~/projects", "author": author or DEFAULT_AUTHOR},
    })
