"""Configuración del nodo y rutas.

    ~/.config/cairn/config.toml     config (nombre del nodo, datos, árbol de proyectos, autor)
    ~/.local/share/cairn/           datos: logs/, la base SQLite (y en la fase 3, la clave)

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
from dataclasses import dataclass
from pathlib import Path

from cairn.events import NODE_RE

CONFIG_NAME = "config.toml"
DEFAULT_AUTHOR = "Pablo + Claude"


class ConfigError(Exception):
    pass


def _xdg(var: str, fallback: str) -> Path:
    v = os.environ.get(var)
    return Path(v) if v else Path.home() / fallback


def config_dir() -> Path:
    v = os.environ.get("CAIRN_CONFIG_DIR")
    return Path(v) if v else _xdg("XDG_CONFIG_HOME", ".config") / "cairn"


def default_data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "cairn"


def default_node_name() -> str:
    """El hostname, llevado al formato de nombre de nodo."""
    name = re.sub(r"[^a-z0-9_-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-_")
    return name if NODE_RE.match(name) else "nodo"


@dataclass(frozen=True)
class Config:
    path: Path              # el config.toml
    node: str
    data_dir: Path
    projects: Path
    author: str

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cairn.sqlite"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "lock"


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


def load() -> Config:
    path = config_dir() / CONFIG_NAME
    if not path.is_file():
        raise ConfigError(f"cairn no está inicializado en este nodo (falta {path}): correr `cairn init`")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
    node = raw.get("node", {}).get("name", "")
    if not NODE_RE.match(str(node)):
        raise ConfigError(f"{path}: [node] name inválido: {node!r}")
    data = os.environ.get("CAIRN_DATA_DIR") or raw.get("data", {}).get("path") or str(default_data_dir())
    docs = raw.get("docs", {})
    projects = os.environ.get("CAIRN_PROJECTS") or docs.get("projects") or "~/projects"
    return Config(path=path, node=node, data_dir=_expand(data), projects=_expand(projects),
                  author=str(docs.get("author") or DEFAULT_AUTHOR))


# ──────────────────────────────────────────────────────────────── escritura

def _toml_str(v: str) -> str:
    out = v.replace("\\", "\\\\").replace('"', '\\"')
    out = "".join(c if c >= " " or c == "\t" else f"\\u{ord(c):04x}" for c in out)
    return f'"{out}"'


def dump_toml(data: dict[str, dict[str, str]]) -> str:
    """TOML de tablas con valores string: alcanza para la config y evita una dependencia."""
    out = []
    for table, values in data.items():
        out.append(f"[{table}]")
        out.extend(f"{k} = {_toml_str(str(v))}" for k, v in values.items())
        out.append("")
    return "\n".join(out)


def write(node: str, data_dir: str | None = None, projects: str | None = None,
          author: str | None = None) -> Path:
    if not NODE_RE.match(node):
        raise ConfigError(f"nombre de nodo inválido: {node!r} (minúsculas, dígitos, `-` y `_`)")
    path = config_dir() / CONFIG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Configuración de cairn para este nodo. Ver `cairn --help`.\n\n"
        + dump_toml({
            "node": {"name": node},
            "data": {"path": data_dir or str(default_data_dir())},
            "docs": {"projects": projects or "~/projects", "author": author or DEFAULT_AUTHOR},
        })
    )
    path.write_text(content, encoding="utf-8")
    return path
