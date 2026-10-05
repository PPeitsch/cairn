"""Entrada y salida de docs como archivos: `import` del árbol de wip-doc y `export` a ese árbol.

    <raíz>/_wip/*.md     abiertos y handoffs pendientes
    <raíz>/_done/*.md    cerrados y handoffs archivados
    <raíz>/_ref/*.md     referencias

Son inversos: exportar lo importado reproduce los archivos (front-matter + cuerpo), con los
vínculos como rutas relativas. Sólo se leen los `.md` de la raíz de cada carpeta; `scripts/`,
`validations/` y el resto no son docs.
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

CARPETAS = {"_wip": "wip", "_done": "done", "_ref": "ref"}


@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)       # "nombre: motivo"
    warnings: list[str] = field(default_factory=list)
    links: int = 0
    unresolved: list[str] = field(default_factory=list)    # "nombre: kind → ruta"
    ignored_files: int = 0


@dataclass
class _Entrada:
    path: Path
    carpeta: str
    name: str
    meta: dict
    body: str
    doc_id: str


def _tipo_inferido(carpeta: str, name: str) -> str:
    if carpeta == "_ref":
        return "ref"
    if "_HANDOFF_" in name:
        return "handoff"
    return "bug" if "_BUG_" in name else "infra"


def _mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat(timespec="microseconds")


def _lista(v) -> list[str]:
    if not v:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def import_tree(store: Store, root: Path, dry_run: bool = False) -> ImportReport:
    """Importa los docs de `root/{_wip,_done,_ref}`. Lo que ya existe (por nombre) se saltea, así
    que correrlo dos veces no duplica nada. Todo entra en una sola escritura."""
    root = root.expanduser()
    if not any((root / c).is_dir() for c in CARPETAS):
        raise CairnError(f"{root} no tiene _wip/, _done/ ni _ref/")
    rep = ImportReport()
    entradas: list[_Entrada] = []
    vistos: dict[str, str] = {}
    for carpeta in CARPETAS:
        d = root / carpeta
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
            if name in vistos:
                rep.skipped.append(f"{carpeta}/{p.name}: el nombre ya está en {vistos[name]}/")
                continue
            if docs.name_taken(store, name):
                rep.skipped.append(f"{carpeta}/{p.name}: ya está en la base")
                continue
            meta, body = parse_front_matter(p.read_text(encoding="utf-8", errors="replace"))
            if meta is None:
                rep.skipped.append(f"{carpeta}/{p.name}: sin front-matter (formato viejo de wip-doc: "
                                   "correr `driver.py migrate` antes)")
                continue
            vistos[name] = carpeta
            entradas.append(_Entrada(p, carpeta, name, meta, body, new_doc_id()))

    ids = {e.name: e.doc_id for e in entradas}
    payloads = []
    for e in entradas:
        meta = dict(e.meta)
        if meta.get("tipo") not in TIPOS_DOC:
            tipo = _tipo_inferido(e.carpeta, e.name)
            rep.warnings.append(f"{e.carpeta}/{e.path.name}: tipo '{meta.get('tipo', '')}' → {tipo}")
            meta["tipo"] = tipo
        links: list[list[str]] = []
        for kind in LINK_KINDS:
            raw = _lista(meta.pop(kind, None))
            pendientes = []
            for ruta in raw:
                target = docs.normalize_ref(ruta)
                dst = ids.get(target)
                if dst is None and docs.name_taken(store, target):
                    dst = docs.find(store, target).id
                if dst is None:
                    pendientes.append(ruta)
                    rep.unresolved.append(f"{e.name}: {kind} → {ruta}")
                elif [kind, dst] not in links:
                    links.append([kind, dst])
            if pendientes:
                meta[kind] = pendientes        # se conserva como texto: no se pierde
        rep.links += len(links)
        scope = DocState(e.name, meta, e.body).scope
        esperado = CARPETAS[e.carpeta]
        if scope != esperado:
            rep.warnings.append(f"{e.carpeta}/{e.path.name}: estado '{meta.get('estado', '')}' no "
                                f"corresponde a {e.carpeta}/ — queda en {scope}")
        payloads.append((e.doc_id, {"name": e.name, "meta": meta, "body": e.body, "links": links,
                                    "source": f"{e.carpeta}/{e.path.name}", "updated": _mtime(e.path)}))
        rep.imported.append(f"{e.carpeta}/{e.path.name}")
    if dry_run or not payloads:
        return rep
    with store.writer() as w:
        for doc_id, payload in payloads:
            if docs.name_taken(store, payload["name"]):     # creado mientras tanto
                raise CairnError(f"{payload['name']} apareció en la base durante el import: reintentar")
            w.emit("import", doc_id, payload)
    return rep


# ──────────────────────────────────────────────────────────────── export

@dataclass
class ExportReport:
    written: list[str] = field(default_factory=list)
    unchanged: int = 0
    stale: list[str] = field(default_factory=list)   # .md que ya estaban y no son de ningún doc


def export_tree(store: Store, root: Path, scopes: tuple[str, ...] = docs.SCOPES,
                force: bool = False) -> ExportReport:
    """Escribe los docs como archivos en `root/{_wip,_done,_ref}`, con la fecha de modificación
    de su último cambio. No pisa un archivo distinto salvo con `force`."""
    root = root.expanduser()
    plan = []
    for doc in docs.all_docs(store, scopes):
        dest = root / doc.path
        text = docs.render(store, doc)
        plan.append((doc, dest, text))
    if not force:
        distintos = [str(dest) for _, dest, text in plan
                     if dest.exists() and dest.read_text(encoding="utf-8", errors="replace") != text]
        if distintos:
            raise CairnError(f"{len(distintos)} archivo(s) ya existen con otro contenido (--force para "
                             "pisarlos):\n" + "\n".join(f"  {d}" for d in distintos[:10]))
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
    # un doc que cambió de carpeta deja el archivo viejo en la otra: se avisa, no se borra
    nuestros = {dest for _, dest, _ in plan}
    for scope in scopes:
        d = root / f"_{scope}"
        rep.stale += sorted(f"_{scope}/{p.name}" for p in d.glob("*.md")
                            if p not in nuestros and p.name.lower() != "readme.md")
    return rep
