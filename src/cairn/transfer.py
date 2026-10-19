"""Docs in and out as files: `import` from the wip-doc tree and `export` to that tree.

    <root>/_wip/*.md     open docs and pending handoffs
    <root>/_done/*.md    closed docs and archived handoffs
    <root>/_ref/*.md     references

They are inverses: exporting what was imported reproduces the files (front-matter + body), with
links as relative paths. Only the `.md` files at the top of each folder are read; `scripts/`,
`validations/` and the rest are not docs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cairn import docs
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
        raise CairnError(f"{root} no tiene _wip/, _done/ ni _ref/")
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
                rep.skipped.append(f"{folder}/{p.name}: el nombre ya está en {seen[name]}/")
                continue
            if docs.name_taken(store, name):
                rep.skipped.append(f"{folder}/{p.name}: ya está en la base")
                continue
            meta, body = parse_front_matter(p.read_text(encoding="utf-8", errors="replace"))
            if meta is None:
                rep.skipped.append(f"{folder}/{p.name}: sin front-matter (formato viejo de wip-doc: "
                                   "correr `driver.py migrate` antes)")
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
            rep.warnings.append(f"{e.folder}/{e.path.name}: estado '{meta.get('estado', '')}' no "
                                f"corresponde a {e.folder}/ — queda en {scope}")
        payloads.append((e.doc_id, {"name": e.name, "meta": meta, "body": e.body, "links": links,
                                    "source": f"{e.folder}/{e.path.name}", "updated": _mtime(e.path)}))
        rep.imported.append(f"{e.folder}/{e.path.name}")
    if dry_run or not payloads:
        return rep
    with store.writer() as w:
        for doc_id, payload in payloads:
            if docs.name_taken(store, payload["name"]):     # created in the meantime
                raise CairnError(f"{payload['name']} apareció en la base durante el import: reintentar")
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
            raise CairnError(f"{len(different)} archivo(s) ya existen con otro contenido (--force para "
                             "pisarlos):\n" + "\n".join(f"  {d}" for d in different[:10]))
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
