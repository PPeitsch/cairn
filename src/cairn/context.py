"""Which repo the work is happening in: what `repo:` gets stamped with when a doc is created, and
which handoffs a conversation is shown. A repo is a folder under the projects root (`~/projects`)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(path: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def git_toplevel(path: Path) -> Path | None:
    top = _git(path, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def git_main_toplevel(path: Path) -> Path | None:
    """The root of the main clone even when `path` is in a worktree (which may live outside the
    projects root): git's common directory is the main clone's `.git`."""
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    p = Path(common)
    return p.parent if p.name == ".git" else None


def repo_of_path(path: Path, root: Path) -> str:
    """The repo under `root` that `path` is in, or "" if it is cross-repo (the root itself or outside it)."""
    root = root.expanduser().resolve()
    cwd = path.resolve()
    top = git_toplevel(cwd)
    cand = top.resolve() if top else cwd
    if cand != root and root in cand.parents:
        return str(cand.relative_to(root))
    main = git_main_toplevel(cwd) if top else None
    if main and main.resolve() != root and root in main.resolve().parents:
        return str(main.resolve().relative_to(root))
    # no git: a subdirectory of the root still identifies the project
    if cwd != root and root in cwd.parents:
        return cwd.relative_to(root).parts[0]
    return ""
