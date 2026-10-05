"""`cairn integrate claude-code`: registra el MCP y los hooks de cairn en Claude Code.

- El MCP se registra con `claude mcp add --scope user` (el CLI de Claude Code): su config de
  usuario (`~/.claude.json`) es estado interno que Claude Code reescribe seguido, así que acá
  sólo se lee, para saber si ya está.
- Los hooks van a `settings.json` (no hay CLI para eso): se agregan, se reemplazan o se sacan
  sólo las entradas de cairn, sin tocar las demás (p. ej. el hook de wip-doc mientras conviven).

Es idempotente (correrlo dos veces no duplica nada) y `remove` lo deshace. Respeta
`CLAUDE_CONFIG_DIR`, lo que permite probarlo contra una config aislada.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cairn.store import CairnError

MCP_NAME = "cairn"
HOOK_TIMEOUT = 10


@dataclass(frozen=True)
class HookSpec:
    event: str
    matcher: str | None
    args: tuple[str, ...]          # lo que va después del comando de cairn
    timeout: int = HOOK_TIMEOUT


# Los hooks que cairn registra. La fase 3 suma los de sync.
HOOKS = (
    HookSpec("SessionStart", "startup|clear", ("handoff", "hook")),
)


@dataclass
class Report:
    actions: list[str] = field(default_factory=list)   # lo que se hizo (o se haría, en dry-run)
    unchanged: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────── rutas y comando

def claude_dir() -> Path:
    v = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(v) if v else Path.home() / ".claude"


def settings_path() -> Path:
    return claude_dir() / "settings.json"


def user_config_path() -> Path:
    """Donde Claude Code guarda los MCP de scope user: `~/.claude.json`, o adentro de
    `CLAUDE_CONFIG_DIR` si está definida."""
    v = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(v) / ".claude.json" if v else Path.home() / ".claude.json"


def default_command() -> list[str]:
    """Cómo se invoca cairn desde Claude Code: la ruta absoluta del comando (los hooks no
    heredan el PATH de una shell interactiva) o, si no está instalado, el módulo."""
    found = shutil.which("cairn")
    if found:
        return [str(Path(found).resolve())]
    return [sys.executable, "-m", "cairn"]


def _is_cairn_hook(command: str, spec: HookSpec) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    tail = list(spec.args)
    if parts[-len(tail):] != tail:
        return False
    head = parts[:-len(tail)]
    # `.../cairn`, `python -m cairn` o `ssh host cairn`
    return any(Path(p).name == "cairn" for p in head)


# ──────────────────────────────────────────────────────────────── settings.json

def _load_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as e:
        raise CairnError(f"{path} no es JSON válido ({e}): no se tocó nada") from None
    if not isinstance(data, dict):
        raise CairnError(f"{path} no es un objeto JSON: no se tocó nada")
    return data


def _write_settings(path: Path, data: dict) -> None:
    """Escritura atómica: un temporal en la misma carpeta y rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _strip_cairn_hooks(groups: list, spec: HookSpec) -> tuple[list, list[str]]:
    """Saca los hooks de cairn de ese tipo. (grupos que quedan, comandos sacados)."""
    out, removed = [], []
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
            out.append(g)
            continue
        keep = []
        for h in g["hooks"]:
            if isinstance(h, dict) and _is_cairn_hook(str(h.get("command", "")), spec):
                removed.append(str(h["command"]))
            else:
                keep.append(h)
        if keep or not g["hooks"]:
            out.append({**g, "hooks": keep})
    return out, removed


def _apply_hooks(data: dict, command: list[str], install: bool, report: Report) -> bool:
    """Deja `data` con los hooks de cairn instalados (o sin ellos). True si cambió algo."""
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise CairnError("settings.json: `hooks` no es un objeto: no se tocó nada")
    hooks = dict(hooks or {})
    changed = False
    for spec in HOOKS:
        groups = hooks.get(spec.event) or []
        if not isinstance(groups, list):
            raise CairnError(f"settings.json: `hooks.{spec.event}` no es una lista: no se tocó nada")
        want = shlex.join([*command, *spec.args])
        current = [str(h.get("command")) for g in groups if isinstance(g, dict)
                   for h in (g.get("hooks") or []) if isinstance(h, dict)
                   and _is_cairn_hook(str(h.get("command", "")), spec)]
        label = f"hook {spec.event}: {want}"
        if install and current == [want]:
            report.unchanged.append(label)
            continue
        groups, removed = _strip_cairn_hooks(groups, spec)
        for r in removed:
            if not install or r != want:
                report.actions.append(f"sacar hook {spec.event}: {r}")
        if install:
            entry: dict = {"hooks": [{"type": "command", "command": want, "timeout": spec.timeout}]}
            if spec.matcher:
                entry = {"matcher": spec.matcher, **entry}
            groups.append(entry)
            report.actions.append(f"agregar {label}")
        if groups:
            hooks[spec.event] = groups
        else:
            hooks.pop(spec.event, None)
        changed = changed or bool(removed) or install
    if changed:
        if hooks:
            data["hooks"] = hooks
        else:
            data.pop("hooks", None)
    return changed


# ──────────────────────────────────────────────────────────────── MCP

def registered_mcp() -> dict | None:
    path = user_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    entry = servers.get(MCP_NAME) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def _claude() -> str:
    found = shutil.which("claude")
    if not found:
        raise CairnError("No se encontró el comando `claude` (Claude Code) en el PATH.")
    return found


def _run_claude(*args: str) -> None:
    r = subprocess.run([_claude(), *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        raise CairnError(f"`claude {' '.join(args)}` falló ({r.returncode}): {msg}")


def _apply_mcp(command: list[str], install: bool, dry_run: bool, report: Report) -> None:
    cur = registered_mcp()
    want = {"command": command[0], "args": [*command[1:], "mcp"]}
    label = f"MCP {MCP_NAME} (scope user): {shlex.join([*command, 'mcp'])}"
    if install and cur is not None and cur.get("command") == want["command"] \
            and list(cur.get("args") or []) == want["args"]:
        report.unchanged.append(label)
        return
    if cur is not None:
        report.actions.append(f"sacar MCP {MCP_NAME} (scope user)")
        if not dry_run:
            _run_claude("mcp", "remove", "--scope", "user", MCP_NAME)
    if install:
        report.actions.append(f"agregar {label}")
        if not dry_run:
            _run_claude("mcp", "add", "--scope", "user", MCP_NAME, "--", *command, "mcp")


# ──────────────────────────────────────────────────────────────── entrada

def claude_code(command: list[str] | None = None, remove: bool = False, dry_run: bool = False,
                mcp: bool = True, hooks: bool = True) -> Report:
    command = command or default_command()
    report = Report()
    if hooks:
        path = settings_path()
        data = _load_settings(path)
        if _apply_hooks(data, command, not remove, report) and not dry_run:
            _write_settings(path, data)
    if mcp:
        _apply_mcp(command, not remove, dry_run, report)
    return report
