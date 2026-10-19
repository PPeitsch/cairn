"""Operations on docs: what wip-doc's commands did, now as events.

There are no folders: where a doc "is" (`wip`, `done`, `ref`) follows from its type and status.
Links between docs (`origen`, `referencias`, `plan`) are stored by id, so closing or reopening
does not break them: the relative paths (`../_ref/x.md`) are computed when showing or exporting.

Every operation returns data (and warnings) and prints nothing: output is the CLI's business,
and the MCP server uses these same functions.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from cairn import clock, templates
from cairn.markdown import (
    COMMENT,
    dump_front_matter,
    fmt_date,
    is_placeholder,
    pending_items,
    section_raw,
    section_text,
)
from cairn.projection import CLOSED_STATES, OPEN_STATES
from cairn.store import CairnError, Store, new_doc_id

TIPOS = ("bug", "infra", "plan", "ref")          # the ones `new` creates
TIPOS_DOC = TIPOS + ("handoff",)                 # the ones that exist (handoff: `handoff new`)
SCOPES = ("wip", "done", "ref")
ICON = {"bug": "[bug]  ", "plan": "[plan] ", "ref": "[ref]  ", "infra": "[infra]", "handoff": "[handoff]"}
LINK_KINDS = ("plan", "origen", "referencias")   # the ones that go into the front-matter as paths
_NAME_RE = re.compile(r"^[\w.+-]+$")


# ──────────────────────────────────────────────────────────────── the doc

@dataclass
class Doc:
    id: str
    name: str
    meta: dict
    body: str
    scope: str
    head: str
    created: str
    updated: str
    divergent: bool
    links: list[list[str]] = field(default_factory=list)

    @property
    def tipo(self) -> str:
        t = self.meta.get("tipo")
        return t if t in TIPOS_DOC else "infra"

    @property
    def estado(self) -> str:
        return str(self.meta.get("estado", ""))

    @property
    def repo(self) -> str:
        return str(self.meta.get("repo") or "")

    @property
    def title(self) -> str:
        m = re.search(r"^# (.+)$", self.body, re.M)
        return m.group(1).strip() if m else self.name

    @property
    def tldr(self) -> str:
        return section_text(self.body, "TL;DR")

    @property
    def path(self) -> str:
        """Where it would be in the wip-doc tree (and where `export` puts it)."""
        return f"_{self.scope}/{self.name}.md"

    def date_closed(self) -> str:
        for k in ("cerrado", "usada", "descartada"):
            if self.meta.get(k):
                return str(self.meta[k])
        return self.updated[:10]


def _doc_from_row(store: Store, r) -> Doc:
    links = [[x["kind"], x["dst"]] for x in store.projection.db.execute(
        "SELECT kind, dst FROM links WHERE src = ? ORDER BY rowid", (r["id"],))]
    return Doc(id=r["id"], name=r["name"], meta=json.loads(r["meta"]), body=r["body"],
               scope=r["scope"], head=r["head"], created=r["created"], updated=r["updated"],
               divergent=bool(r["divergent"]), links=links)


def all_docs(store: Store, scopes: tuple[str, ...] = SCOPES) -> list[Doc]:
    """The docs in those scopes, most recently touched first."""
    marks = ",".join("?" * len(scopes))
    rows = store.projection.db.execute(
        f"SELECT * FROM docs WHERE scope IN ({marks}) ORDER BY updated DESC, name", scopes).fetchall()
    return [_doc_from_row(store, r) for r in rows]


def get(store: Store, doc_id: str) -> Doc | None:
    r = store.projection.db.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
    return _doc_from_row(store, r) if r else None


def normalize_ref(ref: str) -> str:
    """`../_done/x.md`, `_wip/x.md`, `x.md` or `x` → `x`."""
    ref = ref.strip().rstrip("/").split("/")[-1]
    return ref[:-3] if ref.endswith(".md") else ref


def find(store: Store, ref: str) -> Doc:
    """A doc by name (with or without `.md` and a leading folder) or by id prefix (6+ hex)."""
    name = normalize_ref(ref)
    rows = store.projection.db.execute("SELECT * FROM docs WHERE name = ?", (name,)).fetchall()
    if not rows and re.fullmatch(r"[0-9a-f]{6,32}", name):
        rows = store.projection.db.execute("SELECT * FROM docs WHERE id LIKE ?", (name + "%",)).fetchall()
    if not rows:
        raise CairnError(f"No encontrado: {ref}")
    if len(rows) > 1:
        raise CairnError(f"{ref} es ambiguo: " + ", ".join(f"{r['name']} ({r['id'][:8]})" for r in rows))
    return _doc_from_row(store, rows[0])


def name_taken(store: Store, name: str) -> bool:
    return store.projection.db.execute("SELECT 1 FROM docs WHERE name = ?", (name,)).fetchone() is not None


def _check_name_parts(*parts: str) -> None:
    for p in parts:
        if not _NAME_RE.match(p):
            raise CairnError(f"Nombre inválido: {p!r} (letras, dígitos, `_`, `-`, `.`, `+`)")


# ──────────────────────────────────────────────────────────────── render

def link_paths(store: Store, doc: Doc) -> dict[str, list[str]]:
    """Links as relative paths in the tree (`../_ref/x.md`), by kind. Adds the ones left as text
    in the front-matter (imported links that did not point at any doc)."""
    out: dict[str, list[str]] = {}
    for kind in LINK_KINDS:
        raw = doc.meta.get(kind)
        out[kind] = [str(x) for x in raw] if isinstance(raw, list) else ([str(raw)] if raw else [])
    for kind, dst in doc.links:
        target = get(store, dst)
        if target is not None and kind in out:
            p = f"../{target.path}"
            if p not in out[kind]:
                out[kind].append(p)
    return out


def render(store: Store, doc: Doc) -> str:
    """The doc as a `.md` file: front-matter (with links as paths) + body."""
    meta = {k: v for k, v in doc.meta.items() if k not in LINK_KINDS}
    meta.update({k: v for k, v in link_paths(store, doc).items() if v})
    return dump_front_matter(meta) + doc.body


def repo_label(doc: Doc) -> str:
    return doc.repo or "(cross-repo)"


def estado_label(doc: Doc) -> str:
    if doc.scope == "wip":
        return "pendiente" if doc.tipo == "handoff" else "abierto"
    if doc.scope == "ref":
        return "referencia"
    if doc.tipo == "handoff":
        return f"{doc.meta.get('estado', 'usada')} {fmt_date(doc.date_closed())}"
    return f"cerrado {fmt_date(doc.date_closed())}"


# ──────────────────────────────────────────────────────────────── new / close / reopen

@dataclass
class Result:
    doc: Doc
    warnings: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def _meta_base(store: Store, tipo: str, estado: str | None, repo: str) -> dict:
    meta = {"tipo": tipo}
    if repo:
        meta["repo"] = repo
    if estado:
        meta["estado"] = estado
    meta["fecha"] = clock.today()
    if tipo == "ref":
        meta["actualizado"] = clock.today()
    meta["autor"] = store.config.author
    return meta


def new(store: Store, area: str, tema: str, tipo: str = "infra", repo: str = "") -> Result:
    if tipo == "handoff":
        raise CairnError("Un handoff se crea con: handoff new <area> <tema> [--para R]... [--plan <doc>]...")
    if tipo not in TIPOS:
        raise CairnError(f"Tipo inválido: {tipo}. Opciones: {', '.join(TIPOS)}")
    _check_name_parts(area, tema)
    title = f"{area} — {tema.replace('_', ' ')}"
    if tipo == "ref":
        # a reference is neither opened nor closed: no date in the name, it is looked up by topic
        name = f"{area}_{tema}"
        body = templates.body(store.config.templates_dir, "ref", title)
        meta = _meta_base(store, "ref", None, repo)
    else:
        infix = "_BUG_" if tipo == "bug" else "_"
        name = f"{clock.today()}_{area}{infix}{tema}"
        body = templates.body(store.config.templates_dir, tipo, title)
        meta = _meta_base(store, tipo, "en_progreso", repo)
    doc_id = new_doc_id()
    with store.writer() as w:
        if name_taken(store, name):
            raise CairnError(f"Ya existe: {name}")
        w.emit("create", doc_id, {"name": name, "meta": meta, "body": body})
    return Result(get(store, doc_id))


def close(store: Store, ref: str, force: bool = False, descartar: str | None = None) -> Result:
    """Closes a finished doc (resuelto/completado) or, with `descartar` (the reason), one that is
    left unfinished: it becomes `descartado` with the reason in `nota:`, and needs no --force."""
    if descartar is not None and not descartar.strip():
        raise CairnError("Descartar pide un motivo: es lo único que va a explicar por qué quedó así.")
    warnings: list[str] = []
    with store.writer() as w:
        doc = find(store, ref)
        if doc.tipo == "handoff":
            raise CairnError(f"{doc.name} es un handoff: se archiva con `handoff take` (usarlo) o "
                             "`handoff drop` (descartarlo).")
        if doc.scope == "ref":
            raise CairnError(f"{doc.name} es una referencia: no se cierra, se actualiza.")
        if doc.scope == "done":
            raise CairnError(f"{doc.name} ya está cerrado ({doc.date_closed()}).")
        # a doc with an open checklist is not done: closing it removes it from `list` and reports
        # it in `weekly` as finished work, which is exactly what it is not
        pend = pending_items(doc.body)
        if descartar:
            if doc.meta.get("nota"):
                warnings.append(f"[nota] Nota reemplazada por el motivo: \"{doc.meta['nota']}\"")
            w.emit("close", doc.id, {"set": {"estado": "descartado", "cerrado": clock.today(),
                                             "nota": descartar.strip()},
                                     "unset": ["reabierto"]})
        elif pend and not force:
            lines = "\n".join(f"    {n}: {t}" for n, t in pend[:10])
            raise CairnError(f"[aviso] {doc.name} tiene {len(pend)} pendiente(s) sin tildar:\n{lines}\n\n"
                             f"Tildalos, borralos, cerralo igual con: close {doc.name} --force, o, si "
                             f"se deja sin terminar: close {doc.name} --descartar \"motivo\"")
        else:
            if doc.meta.get("nota"):
                # the note described the open work ("low priority", "reopened because…"); once closed, it lies
                warnings.append(f"[nota] Nota descartada al cerrar: \"{doc.meta['nota']}\" — si tenía "
                                "info del cierre, pasala al cuerpo del doc.")
            if pend:
                warnings.append(f"[aviso] Cerrado con {len(pend)} pendiente(s) sin tildar (--force)")
            estado = "completado" if doc.tipo == "plan" else "resuelto"
            w.emit("close", doc.id, {"set": {"estado": estado, "cerrado": clock.today()},
                                     "unset": ["nota", "reabierto"]})
    return Result(get(store, doc.id), warnings)


def reopen(store: Store, ref: str, motivo: str | None = None) -> Result:
    warnings: list[str] = []
    with store.writer() as w:
        doc = find(store, ref)
        if doc.scope != "done":
            raise CairnError(f"{doc.name} no está cerrado (está en {doc.scope}).")
        if doc.tipo == "handoff":
            # taken by mistake, or the conversation was cut short: it waits for the next one again
            change = {"set": {"estado": "pendiente"}, "unset": ["usada", "descartada", "cerrado"]}
        else:
            change = {"set": {"estado": "en_progreso"}, "unset": ["cerrado"]}
        change["set"]["reabierto"] = clock.today()
        if motivo:
            change["set"]["nota"] = motivo
        else:
            change["unset"].append("nota")
            warnings.append("[nota] Sin motivo — pasalo como 2º argumento para que quede en la nota")
        w.emit("reopen", doc.id, change)
    return Result(get(store, doc.id), warnings)


# ──────────────────────────────────────────────────────────────── promote

def promote(store: Store, ref: str, area: str, tema: str, secciones: list[str] | None = None,
            force: bool = False) -> Result:
    """A diagnosis tells WHAT HAPPENED; the topology it uncovered or the commands to diagnose
    again say HOW SOMETHING IS. Copies (does not move) those sections to a reference and links
    the two docs (`origen` / `referencias`)."""
    _check_name_parts(area, tema)
    warnings: list[str] = []
    with store.writer() as w:
        src = find(store, ref)
        if src.scope == "ref":
            raise CairnError(f"{src.name} ya es una referencia: promote toma docs abiertos o cerrados.")
        ref_name = f"{area}_{tema}"
        secciones = secciones or (["Diseño", "Decisiones"] if src.tipo == "plan"
                                  else ["Contexto", "Comandos útiles"])
        target = None
        if name_taken(store, ref_name):
            target = find(store, ref_name)
            if target.scope != "ref":
                raise CairnError(f"{ref_name} existe y no es una referencia.")
            if not force and ["origen", src.id] in target.links:
                raise CairnError(f"{ref_name} ya tiene secciones promovidas de {src.name}. "
                                 "--force para agregar igual.")

        # gather first: if there is nothing to carry over, nothing is created or touched
        block, carried, skipped = "", [], []
        for s in secciones:
            raw = section_raw(src.body, s)
            if not raw:
                skipped.append(f"{s} (no existe)")
                continue
            heading, content = raw
            content = COMMENT.sub("", content).strip("\n")
            if is_placeholder(content):
                skipped.append(f"{heading} (vacía)")
                continue
            carried.append(heading)
            block += f"\n---\n\n## {heading}\n\n{content.strip()}\n"
        if not carried:
            raise CairnError(f"Nada para promover de {src.name}: {', '.join(skipped)}\n"
                             "Elegí secciones con --seccion \"<inicio del título>\".")

        created = target is None
        if created:
            ref_id = new_doc_id()
            body = templates.body(store.config.templates_dir, "ref_header",
                                  f"{area} — {tema.replace('_', ' ')}")
            w.emit("create", ref_id, {"name": ref_name, "body": body,
                                      "meta": _meta_base(store, "ref", None, src.repo)})
        else:
            ref_id, body = target.id, target.body
        w.emit("promote", ref_id, {"body": body.rstrip("\n") + "\n" + block,
                                   "set": {"actualizado": clock.today()},
                                   "link_add": [["origen", src.id]]})
        if ["referencias", ref_id] not in src.links:
            w.emit("link", src.id, {"link_add": [["referencias", ref_id]]})
    if skipped:
        warnings.append(f"  salteadas: {', '.join(skipped)}")
    if created:
        warnings.append("[nota] Referencia nueva: completá el TL;DR y reescribí lo copiado como "
                        "referencia\n    (lo que ES, no lo que pasó) — sacá el contexto del incidente "
                        "que no aplique.")
    else:
        warnings.append("[nota] Revisá que lo agregado no contradiga lo que la referencia ya decía.")
    return Result(get(store, ref_id), warnings, {"carried": carried})


# ──────────────────────────────────────────────────────────────── search / weekly

@dataclass
class Hit:
    doc: Doc
    lines: list[tuple[int, str]]


def search(store: Store, pattern: str, scopes: tuple[str, ...] = SCOPES) -> tuple[list[Hit], int]:
    """A Python regex, case-insensitive, over the doc as a file (front-matter included, so the
    line numbers match `show`). Returns (hits, docs searched)."""
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        raise CairnError(f"Regex inválida: {e}") from None
    docs = all_docs(store, scopes)
    hits = []
    for doc in docs:
        text = render(store, doc)
        lines = [(i + 1, line) for i, line in enumerate(text.splitlines()) if rx.search(line)]
        if lines:
            hits.append(Hit(doc, lines))
    hits.sort(key=lambda h: h.doc.date_closed(), reverse=True)
    hits.sort(key=lambda h: repo_label(h.doc))
    return hits, len(docs)


@dataclass
class FtsHit:
    doc: Doc
    snippet: str        # the part of the doc where it matches, with the match between «»


def search_fts(store: Store, query: str, scopes: tuple[str, ...] = SCOPES,
               limit: int = 20) -> tuple[list[FtsHit], int]:
    """Full-text search (FTS5 syntax, ignoring accents and case), by relevance. Returns up to
    `limit` results, each with the snippet where it matches, and how many docs match in total
    (to say so if the list was cut)."""
    marks = ",".join("?" * len(scopes))
    where = f"FROM docs_fts f JOIN docs d ON d.id = f.doc_id WHERE docs_fts MATCH ? AND d.scope IN ({marks})"
    try:
        rows = store.projection.db.execute(
            f"SELECT d.*, snippet(docs_fts, -1, '«', '»', ' … ', 20) AS snippet {where} "
            "ORDER BY bm25(docs_fts) LIMIT ?", (query, *scopes, limit)).fetchall()
        total = store.projection.db.execute(f"SELECT count(*) {where}", (query, *scopes)).fetchone()[0]
    except sqlite3.OperationalError as e:      # invalid FTS5 syntax
        raise CairnError(f"Consulta FTS inválida: {e}") from None
    return [FtsHit(_doc_from_row(store, r), " ".join(r["snippet"].split())) for r in rows], total


def fts_header(query: str, hits: list[FtsHit], total: int) -> str:
    cut = f"; se muestran los {len(hits)} más relevantes" if total > len(hits) else ""
    return f'Búsqueda FTS "{query}": {total} doc(s), por relevancia{cut}'


def weekly(store: Store, dias: int = 7) -> tuple[str, list[Doc]]:
    """What was closed in the last `dias` days (no handoffs: they are not closed work). (since, docs)."""
    since = (date.fromisoformat(clock.today()) - timedelta(days=dias)).isoformat()
    docs = [d for d in all_docs(store, ("done",)) if d.tipo != "handoff" and d.date_closed() >= since]
    docs.sort(key=lambda d: d.date_closed(), reverse=True)
    return since, docs


# ──────────────────────────────────────────────────────────────── check

def check(store: Store) -> list[str]:
    """What is out of order: the log (corrupt lines, invalid signatures, unprojected events,
    divergent branches) and the docs (inconsistent status, names, broken links, stale handoffs)."""
    problems: list[str] = []
    in_log = 0
    problems += store.verifier.bad_peers
    for entry in store.log.read_all():
        why = entry.error if entry.event is None else store.verifier.problem(entry.event)
        if why:
            problems.append(f"logs/{entry.node}/{entry.segment}.jsonl byte {entry.offset}: {why}")
        else:
            in_log += 1
    projected = store.projection.count_events()
    if projected != in_log:
        problems.append(f"la proyección tiene {projected} evento(s) y el log {in_log} — "
                        "correr `cairn rebuild`")
    db = store.projection.db
    for r in db.execute("SELECT status, count(*) n FROM events WHERE status NOT IN ('applied', 'resolved') "
                      "GROUP BY status"):
        what = {"divergent": "en ramas divergentes (dos nodos escribieron sobre el mismo head)",
                "orphan": "huérfanos (falta el evento anterior: sincronizar)"}.get(r["status"], r["status"])
        problems.append(f"{r['n']} evento(s) {what}")
    for r in db.execute("SELECT name, count(*) n FROM docs GROUP BY name HAVING n > 1"):
        problems.append(f"{r['name']}: {r['n']} docs con el mismo nombre")
    ids = {r["id"] for r in db.execute("SELECT id FROM docs")}
    for doc in all_docs(store):
        n = doc.name
        if doc.divergent:
            problems.append(f"{n} tiene ramas divergentes: `cairn resolve {n}` para verlas y elegir")
        if doc.meta.get("tipo") not in TIPOS_DOC:
            problems.append(f"{n} tiene tipo '{doc.meta.get('tipo', '')}' (válidos: {', '.join(TIPOS_DOC)})")
        if doc.scope != "ref":
            if not re.match(r"^\d{4}-\d{2}-\d{2}_", n):
                problems.append(f"{n} no arranca con fecha YYYY-MM-DD_")
            if doc.estado not in OPEN_STATES | CLOSED_STATES:
                problems.append(f"{n} tiene estado '{doc.estado}' (desconocido)")
        if doc.tipo == "handoff":
            if doc.scope == "done" and not (doc.meta.get("usada") or doc.meta.get("descartada")):
                problems.append(f"{n} es un handoff archivado sin `usada:` ni `descartada:`")
            age = handoff_age(doc)
            if doc.scope == "wip" and age is not None and age > store.config.handoff_stale_days:
                problems.append(f"{n} es un handoff sin tomar hace {age} días — "
                                "`handoff take` o `handoff drop`")
        elif doc.scope == "done" and not doc.meta.get("cerrado"):
            problems.append(f"{n} está cerrado sin `cerrado:` — weekly cae a la última modificación")
        for kind, dst in doc.links:
            if dst not in ids:
                problems.append(f"{n}: el link `{kind}` apunta a un doc que no existe ({dst[:8]})")
    return problems


# ──────────────────────────────────────────────────────────────── handoffs

def _as_list(v) -> list[str]:
    if not v:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def handoff_para(doc: Doc) -> list[str]:
    """Which conversations it is meant for: `para:`, or if missing, the doc's `repo:`."""
    return _as_list(doc.meta.get("para")) or _as_list(doc.meta.get("repo"))


def handoff_age(doc: Doc) -> int | None:
    try:
        return (date.fromisoformat(clock.today()) - date.fromisoformat(str(doc.meta["fecha"]))).days
    except (KeyError, TypeError, ValueError):
        return None


def pending_handoffs(store: Store, repo: str) -> list[Doc]:
    """The pending handoffs for a conversation in `repo`. From the projects root (repo "") all
    of them are shown; from a repo, those that name it in `para:`."""
    out = [d for d in all_docs(store, ("wip",))
           if d.tipo == "handoff" and d.estado == "pendiente" and (not repo or repo in handoff_para(d))]
    return sorted(out, key=lambda d: d.name)


def modified_date(doc: Doc) -> date:
    """The (local) date of the doc's last change."""
    ts = datetime.fromisoformat(doc.updated)
    tz = clock.now().tzinfo if os.environ.get("CAIRN_NOW") else None
    return ts.astimezone(tz).date()


def modified_label(doc: Doc) -> str:
    d = modified_date(doc)
    days = (date.fromisoformat(clock.today()) - d).days
    return f"modificado {d.isoformat()}, " + ("hoy" if days <= 0 else f"hace {days} día(s)")


def grouped_by_repo(docs_: list[Doc]) -> list[str]:
    """The lines of a listing grouped by repo, with how many docs each group has and when each
    doc was last touched. `docs_` goes from newest to oldest (`all_docs`): the groups keep that
    order (that of each group's newest doc) and so do the docs inside them."""
    groups: dict[str, list[Doc]] = {}
    for d in docs_:
        groups.setdefault(repo_label(d), []).append(d)
    out = [f"{len(docs_)} doc(s) en {len(groups)} repo(s); grupos y docs del modificado más "
           "recientemente al más viejo"]
    for repo, ds in groups.items():
        out += ["", f"{repo} ({len(ds)})"]
        out += [f"  {d.name}  [{d.tipo} · {estado_label(d)} · {modified_label(d)}]" for d in ds]
    return out


def handoff_line(doc: Doc, stale: int = 14) -> str:
    """`stale`: after how many days it is flagged as old (`[docs] handoff_stale_days`)."""
    age = handoff_age(doc)
    parts = [f"para: {', '.join(handoff_para(doc)) or 'cross-repo'}"]
    if age is not None:
        parts.append("de hoy" if age == 0 else f"hace {age} día(s)")
        if age > stale:
            parts.append("VIEJA: revisar si sigue valiendo")
    return f"{doc.name}  ({'; '.join(parts)})"


def handoff_new(store: Store, area: str, tema: str, para: list[str] | None = None,
                plans: list[str] | None = None, repo: str = "") -> Result:
    _check_name_parts(area, tema)
    para, plans = para or [], plans or []
    name = f"{clock.today()}_{area}_HANDOFF_{tema}"
    meta = _meta_base(store, "handoff", "pendiente", repo)
    if para:
        meta["para"] = para if len(para) > 1 else para[0]
    doc_id = new_doc_id()
    with store.writer() as w:
        if name_taken(store, name):
            raise CairnError(f"Ya existe: {name}")
        links = [["plan", find(store, p).id] for p in plans]
        body = templates.body(store.config.templates_dir, "handoff",
                              f"{area} — handoff: {tema.replace('_', ' ')}")
        w.emit("handoff_new", doc_id, {"name": name, "meta": meta, "body": body, "links": links})
    warnings = []
    if not (para or repo):
        warnings.append("[nota] Sin `repo:` ni `--para`: el aviso sólo aparece en conversaciones "
                        "abiertas en la raíz de proyectos.")
    return Result(get(store, doc_id), warnings)


def _pick_handoff(store: Store, ref: str | None, repo: str) -> Doc:
    if ref:
        doc = find(store, ref)
        if doc.tipo != "handoff":
            raise CairnError(f"{doc.name} no es un handoff (tipo: {doc.meta.get('tipo', '?')}).")
        if doc.estado != "pendiente":
            raise CairnError(f"{doc.name} ya no está pendiente: {doc.estado} el {doc.date_closed()} "
                             "(la tomó otra conversación o se descartó). `reopen` la vuelve a pendiente.")
        return doc
    pend = pending_handoffs(store, repo)
    where = repo or "la raíz de proyectos"
    if not pend:
        raise CairnError(f"No hay handoffs pendientes para {where}.")
    if len(pend) > 1:
        raise CairnError(f"Hay {len(pend)} handoffs pendientes para {where}; decí cuál:\n"
                         + "\n".join(f"  {handoff_line(d, store.config.handoff_stale_days)}" for d in pend))
    return pend[0]


def _archive_handoff(store: Store, ref: str | None, repo: str, estado: str,
                     motivo: str | None) -> tuple[Doc, Doc]:
    """(before, after). Under the writer's lock: if two conversations take it at once, the
    second one sees it is no longer pending, so they do not both use it."""
    with store.writer() as w:
        doc = _pick_handoff(store, ref, repo)
        date_field = "usada" if estado == "usada" else "descartada"
        change: dict = {"set": {"estado": estado, date_field: clock.today()}, "unset": ["reabierto"]}
        if motivo:
            change["set"]["nota"] = motivo
        else:
            change["unset"].append("nota")
        w.emit("handoff_take" if estado == "usada" else "handoff_drop", doc.id, change)
    return doc, get(store, doc.id)


def handoff_take(store: Store, ref: str | None = None, repo: str = "") -> Result:
    before, doc = _archive_handoff(store, ref, repo, "usada", None)
    warnings = []
    age = handoff_age(doc)
    if age is not None and age > store.config.handoff_stale_days:
        warnings.append(f"La nota tiene {age} días: puede haber quedado vieja.")
    for kind, dst in doc.links:
        if kind != "plan":
            continue
        plan = get(store, dst)
        if plan is None:
            warnings.append(f"El plan {dst[:8]} no existe.")
            continue
        if plan.scope == "done":
            warnings.append(f"El plan {plan.name} ya está cerrado.")
        if plan.updated > before.updated:
            warnings.append(f"El plan {plan.name} se modificó después de escrita la nota: "
                            "si se contradicen, vale el plan.")
    for raw in _as_list(doc.meta.get("plan")):
        warnings.append(f"El plan {raw} no está en la base.")
    return Result(doc, warnings)


def handoff_drop(store: Store, ref: str, motivo: str | None = None) -> Result:
    _, doc = _archive_handoff(store, ref, "", "descartada", motivo)
    return Result(doc)


def handoff_hook_context(store: Store, repo: str, cmd: str = "cairn") -> str | None:
    """The text the SessionStart hook adds as context, or None if nothing is pending."""
    pend = pending_handoffs(store, repo)
    if not pend:
        return None
    lines = [f"cairn: hay {len(pend)} handoff(s) pendiente(s) para {repo or 'la raíz de proyectos'} "
             "(notas que dejó una conversación anterior para esta):"]
    lines += [f"  - {handoff_line(d, store.config.handoff_stale_days)}" for d in pend]
    lines += ["No la tomes ni sigas lo que dice por tu cuenta: en tu primera respuesta, avisale "
              "al usuario que está y preguntale si la toma.",
              f"Tomarla: tool `cairn_handoff_take` del MCP de cairn, o `{cmd} handoff take <nota>` "
              "(la devuelve y la archiva). Descartarla: `cairn_handoff_drop`, o "
              f"`{cmd} handoff drop <nota> [motivo]`."]
    return "\n".join(lines)
