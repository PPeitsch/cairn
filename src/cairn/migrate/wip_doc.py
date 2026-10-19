"""Migrating from wip-doc, the file-based tool cairn replaces: its docs lived as `.md` files
with front-matter in a single tree,

    <root>/_wip/*.md     open docs and pending handoffs
    <root>/_done/*.md    closed docs and archived handoffs
    <root>/_ref/*.md     references

and a `SessionStart` hook announced the handoffs. The steps, each one its own command
(`cairn migrate wip-doc ...`):

- `import` reads the tree into cairn and `export` writes cairn's docs back as that tree. They
  are inverses: exporting what was imported reproduces the files (front-matter + body), with
  links as relative paths. Only the `.md` files at the top of each folder are read; `scripts/`,
  `validations/` and the rest are not docs.
- `check` reports what is left over after importing: files already in cairn, `.md` files that
  were not imported, and `_wip/` folders inside repos (the location used before the single tree).
- `unhook` removes wip-doc's handoffs hook from Claude Code's settings: with both active, every
  handoff would be announced twice.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cairn import docs, integrate
from cairn.docs import LINK_KINDS, TIPOS_DOC
from cairn.markdown import parse_front_matter
from cairn.projection import DocState
from cairn.store import CairnError, Store, new_doc_id

DOC_DIRS = {"_wip": "wip", "_done": "done", "_ref": "ref"}


@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)       # "name: reason"
    warnings: list[str] = field(default_factory=list)
    links: int = 0
    unresolved: list[str] = field(default_factory=list)    # "name: kind → path"
    ignored_files: int = 0


@dataclass
class _Entry:
    path: Path
    folder: str
    name: str
    meta: dict
    body: str
    doc_id: str


def _inferred_type(folder: str, name: str) -> str:
    if folder == "_ref":
        return "ref"
    if "_HANDOFF_" in name:
        return "handoff"
    return "bug" if "_BUG_" in name else "infra"


def _mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat(timespec="microseconds")


def _as_list(v) -> list[str]:
    if not v:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def import_tree(store: Store, root: Path, dry_run: bool = False) -> ImportReport:
    """Imports the docs in `root/{_wip,_done,_ref}`. Whatever already exists (by name) is
    skipped, so running it twice duplicates nothing. Everything goes in in a single write."""
    root = root.expanduser()
    if not any((root / c).is_dir() for c in DOC_DIRS):
        raise CairnError(f"{root} has no _wip/, _done/ or _ref/")
    rep = ImportReport()
    entries: list[_Entry] = []
    seen: dict[str, str] = {}
    for folder in DOC_DIRS:
        d = root / folder
        if not d.is_dir():
            continue
        rep.ignored_files += sum(1 for p in d.rglob("*") if p.is_file() and p.parent != d)
        for p in sorted(d.iterdir()):
            if not p.is_file():
                continue
            if p.suffix != ".md" or p.name.lower() == "readme.md":
                rep.ignored_files += 1
                continue
            name = p.stem
            if name in seen:
                rep.skipped.append(f"{folder}/{p.name}: the name is already in {seen[name]}/")
                continue
            if docs.name_taken(store, name):
                rep.skipped.append(f"{folder}/{p.name}: already in the database")
                continue
            meta, body = parse_front_matter(p.read_text(encoding="utf-8", errors="replace"))
            if meta is None:
                rep.skipped.append(f"{folder}/{p.name}: no front-matter (old wip-doc format: run "
                                   "`driver.py migrate` first)")
                continue
            seen[name] = folder
            entries.append(_Entry(p, folder, name, meta, body, new_doc_id()))

    ids = {e.name: e.doc_id for e in entries}
    payloads = []
    for e in entries:
        meta = dict(e.meta)
        if meta.get("tipo") not in TIPOS_DOC:
            tipo = _inferred_type(e.folder, e.name)
            rep.warnings.append(f"{e.folder}/{e.path.name}: tipo '{meta.get('tipo', '')}' → {tipo}")
            meta["tipo"] = tipo
        links: list[list[str]] = []
        for kind in LINK_KINDS:
            raw = _as_list(meta.pop(kind, None))
            unresolved = []
            for path in raw:
                target = docs.normalize_ref(path)
                dst = ids.get(target)
                if dst is None and docs.name_taken(store, target):
                    dst = docs.find(store, target).id
                if dst is None:
                    unresolved.append(path)
                    rep.unresolved.append(f"{e.name}: {kind} → {path}")
                elif [kind, dst] not in links:
                    links.append([kind, dst])
            if unresolved:
                meta[kind] = unresolved        # kept as text: nothing is lost
        rep.links += len(links)
        scope = DocState(e.name, meta, e.body).scope
        expected = DOC_DIRS[e.folder]
        if scope != expected:
            rep.warnings.append(f"{e.folder}/{e.path.name}: status '{meta.get('estado', '')}' does "
                                f"not match {e.folder}/ — it goes to {scope}")
        payloads.append((e.doc_id, {"name": e.name, "meta": meta, "body": e.body, "links": links,
                                    "source": f"{e.folder}/{e.path.name}", "updated": _mtime(e.path)}))
        rep.imported.append(f"{e.folder}/{e.path.name}")
    if dry_run or not payloads:
        return rep
    with store.writer() as w:
        for doc_id, payload in payloads:
            if docs.name_taken(store, payload["name"]):     # created in the meantime
                raise CairnError(f"{payload['name']} appeared in the database during the import: try again")
            w.emit("import", doc_id, payload)
    return rep


# ──────────────────────────────────────────────────────────────── export

@dataclass
class ExportReport:
    written: list[str] = field(default_factory=list)
    unchanged: int = 0
    stale: list[str] = field(default_factory=list)   # .md files already there that belong to no doc


def export_tree(store: Store, root: Path, scopes: tuple[str, ...] = docs.SCOPES,
                force: bool = False) -> ExportReport:
    """Writes the docs as files in `root/{_wip,_done,_ref}`, with their last change as the
    modification time. It does not overwrite a different file unless `force`."""
    root = root.expanduser()
    plan = []
    for doc in docs.all_docs(store, scopes):
        dest = root / doc.path
        text = docs.render(store, doc)
        plan.append((doc, dest, text))
    if not force:
        different = [str(dest) for _, dest, text in plan
                     if dest.exists() and dest.read_text(encoding="utf-8", errors="replace") != text]
        if different:
            raise CairnError(f"{len(different)} file(s) already exist with other content (--force to "
                             "overwrite them):\n" + "\n".join(f"  {d}" for d in different[:10]))
    rep = ExportReport()
    for doc, dest, text in plan:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.read_text(encoding="utf-8", errors="replace") == text:
            rep.unchanged += 1
        else:
            dest.write_text(text, encoding="utf-8")
            rep.written.append(doc.path)
        try:
            t = datetime.fromisoformat(doc.updated).timestamp()
            os.utime(dest, (t, t))
        except ValueError:
            pass
    # a doc that changed folder leaves the old file in the other one: report it, do not delete it
    ours = {dest for _, dest, _ in plan}
    for scope in scopes:
        d = root / f"_{scope}"
        rep.stale += sorted(f"_{scope}/{p.name}" for p in d.glob("*.md")
                            if p not in ours and p.name.lower() != "readme.md")
    return rep


# ──────────────────────────────────────────────────────────────── leftovers

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


def leftovers(store: Store, root: Path | None = None) -> list[str]:
    """What the wip-doc tree under `root` (default: the projects root) still has that is not in
    cairn, or that cairn already has. Nothing if the root does not exist."""
    cfg = store.config
    root = (root or cfg.projects).expanduser()
    if not root.is_dir():
        return []
    out: list[str] = []
    for c in DOC_DIRS:
        d = root / c
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if not p.is_file() or p.name.lower() == "readme.md":
                continue
            if docs.name_taken(store, p.stem):
                out.append(f"{c}/{p.name}: the doc is already in cairn and the file is a wip-doc "
                           "leftover (delete it; `cairn show` reproduces it)")
            else:
                out.append(f"{c}/{p.name}: a doc outside cairn — `cairn migrate wip-doc import "
                           f"{root}` adds it")
    for d in doc_dirs_in_repos(root, frozenset(cfg.prune), cfg.max_depth):
        n = sum(1 for x in d.rglob("*.md") if x.name.lower() != "readme.md")
        out.append(f"{d.relative_to(root)}/ has {n} .md file(s) inside a repo — if they are docs, "
                   f"`cairn migrate wip-doc import {d.parent}`; if not, they go in {root.name}/{d.name}/")
    return out


# ──────────────────────────────────────────────────────────────── hook

def is_hook(command: str) -> bool:
    """wip-doc's handoffs hook (`.../wip-doc/driver.py handoff hook`), which cairn's replaces."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return parts[-2:] == ["handoff", "hook"] and any(
        Path(p).name == "driver.py" and Path(p).parent.name == "wip-doc" for p in parts[:-2])


def unhook(dry_run: bool = False) -> integrate.Report:
    """Removes wip-doc's handoffs hook from Claude Code's `settings.json`, without touching the
    rest. Idempotent."""
    report = integrate.Report()
    path = integrate.settings_path()
    data = integrate._load_settings(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get("SessionStart"), list):
        return report
    groups, removed = integrate._strip_hooks(hooks["SessionStart"], is_hook)
    report.actions += [f"remove wip-doc's SessionStart hook: {r}" for r in removed]
    if not removed:
        return report
    hooks = dict(hooks)
    if groups:
        hooks["SessionStart"] = groups
    else:
        hooks.pop("SessionStart")
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks")
    if not dry_run:
        integrate._write_settings(path, data)
    return report
