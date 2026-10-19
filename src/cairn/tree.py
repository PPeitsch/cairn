"""What still lives as files in the projects root: `scripts/` and `validations/` (the
`[docs] file_dirs` folders) under `_wip/`, `_done/` and `_ref/` (they are not docs: cairn does
not store them), and the leftovers of the wip-doc tree.

After migrating, a `.md` at the top of those folders is a doc that did not go through cairn
(someone wrote it by hand, or it was left over from the import), and a `_wip/` inside a repo is
the location used before the single tree. `problems` reports them for `check`; `search_files`
searches the `.md` files in the subfolders, which wip-doc's `search` also covered.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from cairn import docs
from cairn.store import Store

DOC_DIRS = ("_wip", "_done", "_ref")
# the file subfolders (`[docs] file_dirs`), what is not walked (`prune`) and how deep doc folders
# are looked for inside repos (`max_depth`) come from the config


@dataclass
class FileHit:
    path: Path          # relative to the projects root
    lines: list[tuple[int, str]]


def _md_root(d: Path) -> list[Path]:
    return sorted(p for p in d.glob("*.md") if p.is_file() and p.name.lower() != "readme.md")


def doc_dirs_in_repos(root: Path, prune=frozenset(), max_depth: int = 4) -> list[Path]:
    """`_wip/_done/_ref` folders with `.md` files inside a repo (not in the projects root)."""
    out = []
    for dirpath, dirnames, _ in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        for d in dirnames:
            if d in DOC_DIRS and depth > 0:
                c = Path(dirpath) / d
                if any(x.name.lower() != "readme.md" for x in c.rglob("*.md")):
                    out.append(c)
        dirnames[:] = [] if depth >= max_depth else [
            x for x in dirnames if x not in prune and x not in DOC_DIRS and not x.startswith(".")]
    return sorted(out)


def problems(store: Store) -> list[str]:
    """What is out of order in the projects root. Nothing if the root does not exist (a node
    without projects, like the VPS)."""
    cfg = store.config
    root = cfg.projects
    if not root.is_dir():
        return []
    subs = " o ".join(f"{d}/" for d in cfg.file_dirs) or "una subcarpeta (`[docs] file_dirs`)"
    out: list[str] = []
    for c in DOC_DIRS:
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
    for d in doc_dirs_in_repos(root, frozenset(cfg.prune), cfg.max_depth):
        n = sum(1 for x in d.rglob("*.md") if x.name.lower() != "readme.md")
        out.append(f"{d.relative_to(root)}/ tiene {n} .md adentro de un repo — si son docs, "
                   f"`cairn import {d.parent}`; si no, van en {root.name}/{d.name}/ + {subs}")
    return out


def search_files(store: Store, pattern: str, scopes: tuple[str, ...] = docs.SCOPES) -> list[FileHit]:
    """`.md` files in `scripts/` and `validations/` (validation notes, reports) with lines that
    match the regex (case-insensitive)."""
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
