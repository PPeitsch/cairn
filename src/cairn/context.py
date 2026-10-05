"""En qué repo se está trabajando: lo que estampa `repo:` al crear un doc y decide qué handoffs le
tocan a una conversación. Un repo es una carpeta bajo la raíz de proyectos (`~/projects`)."""

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
    """La raíz del clon principal aunque `path` esté en un worktree (que puede vivir fuera de la
    raíz de proyectos): el directorio común de git es el `.git` del clon principal."""
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    p = Path(common)
    return p.parent if p.name == ".git" else None


def repo_of_path(path: Path, root: Path) -> str:
    """El repo bajo `root` en el que está `path`, o "" si es cross-repo (la raíz misma o afuera)."""
    root = root.expanduser().resolve()
    cwd = path.resolve()
    top = git_toplevel(cwd)
    cand = top.resolve() if top else cwd
    if cand != root and root in cand.parents:
        return str(cand.relative_to(root))
    main = git_main_toplevel(cwd) if top else None
    if main and main.resolve() != root and root in main.resolve().parents:
        return str(main.resolve().relative_to(root))
    # sin git: un subdirectorio de la raíz igual identifica el proyecto
    if cwd != root and root in cwd.parents:
        return cwd.relative_to(root).parts[0]
    return ""
