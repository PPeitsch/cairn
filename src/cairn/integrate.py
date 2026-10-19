"""`cairn integrate claude-code`: registers cairn's MCP server and hooks in Claude Code.
`cairn integrate systemd`: installs the user timer that syncs every so often (see `systemd`).

- The MCP server is registered with `claude mcp add --scope user` (Claude Code's CLI): its user
  config (`~/.claude.json`) is internal state that Claude Code rewrites often, so here it is
  only read, to find out whether the server is already there.
- The hooks go into `settings.json` (there is no CLI for that): only cairn's entries are added,
  replaced or removed, without touching the rest.

It is idempotent (running it twice duplicates nothing) and `remove` undoes it. It honors
`CLAUDE_CONFIG_DIR`, which allows testing it against an isolated config.
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

MCP_NAME = "cairn"          # default for `[claude_code] mcp_name`
HOOK_TIMEOUT = 10           # default for `[claude_code] end_timeout`
START_TIMEOUT = 30          # default for `[claude_code] start_timeout`


@dataclass(frozen=True)
class HookSpec:
    event: str
    matcher: str | None
    args: tuple[str, ...]          # what goes after the cairn command
    timeout: int = HOOK_TIMEOUT


# The hooks cairn registers. The handoffs one first pulls from the remotes (if any), so it gets
# more time; when the session ends, the push runs in the background without delaying the exit.
HOOKS = (
    HookSpec("SessionStart", "startup|clear", ("handoff", "hook"), timeout=START_TIMEOUT),
    HookSpec("SessionEnd", None, ("sync", "--background")),
)


@dataclass(frozen=True)
class Settings:
    """What is configurable about the integration (`[claude_code]`): the MCP server name and
    how long Claude Code gives each hook before killing it."""
    mcp_name: str = MCP_NAME
    start_timeout: int = START_TIMEOUT
    end_timeout: int = HOOK_TIMEOUT

    @classmethod
    def from_config(cls) -> Settings:
        from cairn import config
        try:
            cfg = config.load()
        except config.ConfigError:
            return cls()
        return cls(cfg.mcp_name, cfg.start_timeout, cfg.end_timeout)

    def hooks(self) -> tuple[HookSpec, ...]:
        timeouts = {"SessionStart": self.start_timeout, "SessionEnd": self.end_timeout}
        return tuple(HookSpec(h.event, h.matcher, h.args, timeouts[h.event]) for h in HOOKS)


@dataclass
class Report:
    actions: list[str] = field(default_factory=list)   # what was done (or would be, in dry-run)
    unchanged: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────── paths and command

def claude_dir() -> Path:
    v = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(v) if v else Path.home() / ".claude"


def settings_path() -> Path:
    return claude_dir() / "settings.json"


def user_config_path() -> Path:
    """Where Claude Code keeps user-scope MCP servers: `~/.claude.json`, or inside
    `CLAUDE_CONFIG_DIR` if it is set."""
    v = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(v) / ".claude.json" if v else Path.home() / ".claude.json"


def default_command() -> list[str]:
    """How Claude Code invokes cairn: the command's absolute path (hooks do not inherit an
    interactive shell's PATH) or, if it is not installed, the module."""
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
    # `.../cairn`, `python -m cairn` or `ssh host cairn`
    return any(Path(p).name == "cairn" for p in head)


# ──────────────────────────────────────────────────────────────── settings.json

def _load_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as e:
        raise CairnError(f"{path} is not valid JSON ({e}): nothing was touched") from None
    if not isinstance(data, dict):
        raise CairnError(f"{path} is not a JSON object: nothing was touched")
    return data


def _write_settings(path: Path, data: dict) -> None:
    """Atomic write: a temporary file in the same folder, then rename."""
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


def _strip_hooks(groups: list, match) -> tuple[list, list[str]]:
    """Removes the hooks whose command satisfies `match`. (remaining groups, removed commands)."""
    out, removed = [], []
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
            out.append(g)
            continue
        keep = []
        for h in g["hooks"]:
            if isinstance(h, dict) and match(str(h.get("command", ""))):
                removed.append(str(h["command"]))
            else:
                keep.append(h)
        if keep or not g["hooks"]:
            out.append({**g, "hooks": keep})
    return out, removed


def _apply_hooks(data: dict, command: list[str], install: bool, report: Report,
                 specs: tuple[HookSpec, ...] = HOOKS) -> bool:
    """Leaves `data` with cairn's hooks installed (or without them). True if anything changed."""
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise CairnError("settings.json: `hooks` is not an object: nothing was touched")
    hooks = dict(hooks or {})
    changed = False
    for spec in specs:
        groups = hooks.get(spec.event) or []
        if not isinstance(groups, list):
            raise CairnError(f"settings.json: `hooks.{spec.event}` is not a list: nothing was touched")
        want = shlex.join([*command, *spec.args])
        current = [(str(h.get("command")), h.get("timeout")) for g in groups if isinstance(g, dict)
                   for h in (g.get("hooks") or []) if isinstance(h, dict)
                   and _is_cairn_hook(str(h.get("command", "")), spec)]
        label = f"hook {spec.event}: {want}"
        if install and current == [(want, spec.timeout)]:
            report.unchanged.append(label)
            continue
        groups, removed = _strip_hooks(groups, lambda c, spec=spec: _is_cairn_hook(c, spec))
        for r in removed:
            if not install or r != want:
                report.actions.append(f"remove hook {spec.event}: {r}")
        if install:
            entry: dict = {"hooks": [{"type": "command", "command": want, "timeout": spec.timeout}]}
            if spec.matcher:
                entry = {"matcher": spec.matcher, **entry}
            groups.append(entry)
            report.actions.append(f"add {label}")
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

def registered_mcp(name: str = MCP_NAME) -> dict | None:
    path = user_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    entry = servers.get(name) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def _claude() -> str:
    found = shutil.which("claude")
    if not found:
        raise CairnError("The `claude` command (Claude Code) was not found in the PATH.")
    return found


def _run_claude(*args: str) -> None:
    r = subprocess.run([_claude(), *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        raise CairnError(f"`claude {' '.join(args)}` failed ({r.returncode}): {msg}")


def _apply_mcp(command: list[str], install: bool, dry_run: bool, report: Report,
               name: str = MCP_NAME) -> None:
    cur = registered_mcp(name)
    want = {"command": command[0], "args": [*command[1:], "mcp"]}
    label = f"MCP {name} (scope user): {shlex.join([*command, 'mcp'])}"
    if install and cur is not None and cur.get("command") == want["command"] \
            and list(cur.get("args") or []) == want["args"]:
        report.unchanged.append(label)
        return
    if cur is not None:
        report.actions.append(f"remove MCP {name} (scope user)")
        if not dry_run:
            _run_claude("mcp", "remove", "--scope", "user", name)
    if install:
        report.actions.append(f"add {label}")
        if not dry_run:
            _run_claude("mcp", "add", "--scope", "user", name, "--", *command, "mcp")


# ──────────────────────────────────────────────────────────────── entry point

def claude_code(command: list[str] | None = None, remove: bool = False, dry_run: bool = False,
                mcp: bool = True, hooks: bool = True, settings: Settings | None = None) -> Report:
    command = command or default_command()
    settings = settings or Settings.from_config()
    report = Report()
    if hooks:
        path = settings_path()
        data = _load_settings(path)
        changed = _apply_hooks(data, command, not remove, report, settings.hooks())
        if changed and not dry_run:
            _write_settings(path, data)
    if mcp:
        _apply_mcp(command, not remove, dry_run, report, settings.mcp_name)
    return report


# ──────────────────────────────────────────────────────────────── systemd timer

UNIT = "cairn-sync"


def systemd_dir() -> Path:
    v = os.environ.get("CAIRN_SYSTEMD_DIR")
    if v:
        return Path(v)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def systemd_units(command: list[str], interval: int) -> dict[str, str]:
    return {
        f"{UNIT}.service": (
            "[Unit]\n"
            "Description=cairn: sync with the remotes\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"ExecStart={shlex.join([*command, 'sync', '--quiet'])}\n"),
        f"{UNIT}.timer": (
            "[Unit]\n"
            "Description=cairn: periodic sync (backup for the sync after each write)\n\n"
            "[Timer]\n"
            "OnBootSec=2min\n"
            f"OnUnitActiveSec={interval}min\n"
            "RandomizedDelaySec=30\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"),
    }


def _systemctl(*args: str) -> None:
    found = shutil.which("systemctl")
    if not found:
        raise CairnError("`systemctl` was not found: the timer needs systemd.")
    r = subprocess.run([found, "--user", *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        raise CairnError(f"`systemctl --user {' '.join(args)}` failed ({r.returncode}): {msg}")


def systemd(command: list[str] | None = None, interval: int = 15, remove: bool = False,
            dry_run: bool = False) -> Report:
    """The backup for the automatic triggers: a user timer that runs `cairn sync` every
    `interval` minutes (`[sync] interval`). It covers a push that failed (remote down, no
    network), a session-end hook that did not run, and the machine being off in between.
    Idempotent; `remove` disables it and deletes the units. `CAIRN_SYSTEMD_DIR` changes where
    they are written."""
    command = command or default_command()
    d = systemd_dir()
    report = Report()
    if remove:
        present = [n for n in systemd_units(command, interval) if (d / n).exists()]
        if not present:
            return report
        report.actions.append(f"disable {UNIT}.timer and delete {', '.join(present)}")
        if not dry_run:
            _systemctl("disable", "--now", f"{UNIT}.timer")
            for n in present:
                (d / n).unlink()
            _systemctl("daemon-reload")
        return report
    changed = False
    for name, text in systemd_units(command, interval).items():
        path = d / name
        cur = path.read_text(encoding="utf-8") if path.exists() else None
        if cur == text:
            report.unchanged.append(str(path))
            continue
        changed = True
        report.actions.append(f"{'rewrite' if cur is not None else 'write'} {path}")
        if not dry_run:
            d.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if changed:
        report.actions.append(f"enable {UNIT}.timer (every {interval} min)")
        if not dry_run:
            _systemctl("daemon-reload")
            _systemctl("enable", "--now", f"{UNIT}.timer")
    return report
