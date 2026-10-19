"""Files that are not docs: scripts, reports, CSV. cairn does not store them; they live in the
projects root, under `_wip/`, `_done/` and `_ref/`, in the subfolders `[docs] file_dirs` names
(e.g. `scripts`, `validations`). With no `file_dirs` (the default), none of this applies.

`problems` reports, for `check`, what is out of place in those folders: a `.md` at the top (a
doc that did not go through cairn), a file outside the subfolders, a subfolder that is not one
of them. `search_files` searches the `.md` files in the subfolders (validation notes, reports).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cairn import docs
from cairn.store import Store

DOC_DIRS = tuple(f"_{s}" for s in docs.SCOPES)


@dataclass
class FileHit:
    path: Path          # relative to the projects root
    lines: list[tuple[int, str]]


def label(store: Store) -> str:
    """`scripts/ and validations/`: how the file subfolders are named in messages."""
    names = [f"{d}/" for d in store.config.file_dirs]
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


def problems(store: Store) -> list[str]:
    """What is out of place in the projects root. Nothing without `file_dirs` or if the root does
    not exist (a node without projects, like the VPS)."""
    cfg = store.config
    root = cfg.projects
    if not cfg.file_dirs or not root.is_dir():
        return []
    subs = " or ".join(f"{d}/" for d in cfg.file_dirs)
    out: list[str] = []
    for c in DOC_DIRS:
        d = root / c
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix == ".md":
                if p.name.lower() != "readme.md":
                    out.append(f"{c}/{p.name}: a stray .md — docs go in cairn; files, in "
                               f"{c}/ + {subs}")
            elif p.is_file():
                out.append(f"{c}/{p.name} is not a doc — it goes in {c}/ + {subs}")
            elif p.is_dir() and p.name not in cfg.file_dirs and not p.name.startswith("."):
                out.append(f"{c}/{p.name}/ is not a files folder ({subs})")
    return out


def search_files(store: Store, pattern: str, scopes: tuple[str, ...] = docs.SCOPES) -> list[FileHit]:
    """`.md` files in the `file_dirs` subfolders with lines that match the regex
    (case-insensitive)."""
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
