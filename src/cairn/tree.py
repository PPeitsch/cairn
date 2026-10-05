"""Lo que sigue viviendo como archivos en la raíz de proyectos: `scripts/` y `validations/` (las
carpetas de `[docs] file_dirs`) de `_wip/`, `_done/` y `_ref/` (no son docs: cairn no los
guarda), y los restos del árbol de wip-doc.

Después de migrar, un `.md` en la raíz de esas carpetas es un doc que no pasó por cairn (lo
escribió alguien a mano, o quedó del import) y uno de `_wip/` adentro de un repo es la ubicación
de antes del árbol único. `problems` los reporta para `check`; `search_files` busca en los `.md`
de las subcarpetas, que el `search` de wip-doc también barría.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from cairn import docs
from cairn.store import Store

CARPETAS = ("_wip", "_done", "_ref")
# las subcarpetas de archivos (`[docs] file_dirs`), lo que no se recorre (`prune`) y hasta qué
# profundidad se buscan carpetas de docs adentro de repos (`max_depth`) salen de la config


@dataclass
class FileHit:
    path: Path          # relativa a la raíz de proyectos
    lines: list[tuple[int, str]]


def _md_root(d: Path) -> list[Path]:
    return sorted(p for p in d.glob("*.md") if p.is_file() and p.name.lower() != "readme.md")


def carpetas_en_repos(root: Path, prune=frozenset(), max_depth: int = 4) -> list[Path]:
    """Carpetas `_wip/_done/_ref` con `.md` adentro de un repo (no en la raíz de proyectos)."""
    out = []
    for dirpath, dirnames, _ in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        for d in dirnames:
            if d in CARPETAS and depth > 0:
                c = Path(dirpath) / d
                if any(x.name.lower() != "readme.md" for x in c.rglob("*.md")):
                    out.append(c)
        dirnames[:] = [] if depth >= max_depth else [
            x for x in dirnames if x not in prune and x not in CARPETAS and not x.startswith(".")]
    return sorted(out)


def problems(store: Store) -> list[str]:
    """Lo que no está en orden en la raíz de proyectos. Nada si la raíz no existe (un nodo sin
    proyectos, como el VPS)."""
    cfg = store.config
    root = cfg.projects
    if not root.is_dir():
        return []
    subs = " o ".join(f"{d}/" for d in cfg.file_dirs) or "una subcarpeta (`[docs] file_dirs`)"
    out: list[str] = []
    for c in CARPETAS:
        d = root / c
        if not d.is_dir():
            continue
        for p in _md_root(d):
            if docs.name_taken(store, p.stem):
                out.append(f"{c}/{p.name}: el doc ya está en cairn y el archivo quedó de wip-doc "
                           "(borrarlo; `cairn show` lo reproduce)")
            else:
                out.append(f"{c}/{p.name}: un doc fuera de cairn — `cairn import {root}` lo suma")
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix != ".md":
                out.append(f"{c}/{p.name} no es un doc — va en {c}/ + {subs}")
            elif p.is_dir() and p.name not in cfg.file_dirs and not p.name.startswith("."):
                out.append(f"{c}/{p.name}/ no es una carpeta de archivos ({subs})")
    for d in carpetas_en_repos(root, frozenset(cfg.prune), cfg.max_depth):
        n = sum(1 for x in d.rglob("*.md") if x.name.lower() != "readme.md")
        out.append(f"{d.relative_to(root)}/ tiene {n} .md adentro de un repo — si son docs, "
                   f"`cairn import {d.parent}`; si no, van en {root.name}/{d.name}/ + {subs}")
    return out


def search_files(store: Store, pattern: str, scopes: tuple[str, ...] = docs.SCOPES) -> list[FileHit]:
    """`.md` de `scripts/` y `validations/` (notas de validación, reportes) con líneas que
    coinciden con la regex (case-insensitive)."""
    root = store.config.projects
    prune = set(store.config.prune)
    rx = re.compile(pattern, re.I)
    hits = []
    for scope in scopes:
        for sub in store.config.file_dirs:
            d = root / f"_{scope}" / sub
            if not d.is_dir():
                continue
            for p in sorted(d.rglob("*.md")):
                if any(part in prune for part in p.parts):
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
                lines = [(i + 1, ln) for i, ln in enumerate(text.splitlines()) if rx.search(ln)]
                if lines:
                    hits.append(FileHit(p.relative_to(root), lines))
    return hits
