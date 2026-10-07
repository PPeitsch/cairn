"""Operaciones sobre los docs: lo que hacían los comandos de wip-doc, ahora como eventos.

No hay carpetas: dónde "está" un doc (`wip`, `done`, `ref`) se deduce de su tipo y su estado.
Los vínculos entre docs (`origen`, `referencias`, `plan`) se guardan por id, así que cerrar o
reabrir no rompe links: las rutas relativas (`../_ref/x.md`) se calculan al mostrar o exportar.

Cada operación devuelve datos (y avisos) y no imprime nada: la salida es cosa del CLI, y el MCP
de la fase 2 usa estas mismas funciones.
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
from cairn.projection import ABIERTOS, CERRADOS
from cairn.store import CairnError, Store, new_doc_id

TIPOS = ("bug", "infra", "plan", "ref")          # los que crea `new`
TIPOS_DOC = TIPOS + ("handoff",)                 # los que existen (handoff: `handoff new`)
SCOPES = ("wip", "done", "ref")
ICON = {"bug": "[bug]  ", "plan": "[plan] ", "ref": "[ref]  ", "infra": "[infra]", "handoff": "[handoff]"}
LINK_KINDS = ("plan", "origen", "referencias")   # los que van al front-matter como rutas
_NAME_RE = re.compile(r"^[\w.+-]+$")


# ──────────────────────────────────────────────────────────────── el doc

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
        """Dónde estaría en el árbol de wip-doc (y donde lo deja `export`)."""
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
    """Los docs de esos scopes, los tocados más recientemente primero."""
    marks = ",".join("?" * len(scopes))
    rows = store.projection.db.execute(
        f"SELECT * FROM docs WHERE scope IN ({marks}) ORDER BY updated DESC, name", scopes).fetchall()
    return [_doc_from_row(store, r) for r in rows]


def get(store: Store, doc_id: str) -> Doc | None:
    r = store.projection.db.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
    return _doc_from_row(store, r) if r else None


def normalize_ref(ref: str) -> str:
    """`../_done/x.md`, `_wip/x.md`, `x.md` o `x` → `x`."""
    ref = ref.strip().rstrip("/").split("/")[-1]
    return ref[:-3] if ref.endswith(".md") else ref


def find(store: Store, ref: str) -> Doc:
    """Un doc por nombre (con o sin `.md` y carpeta adelante) o por prefijo de id (6+ hex)."""
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
    """Los vínculos como rutas relativas del árbol (`../_ref/x.md`), por tipo. Suma los que
    quedaron como texto en el front-matter (links importados que no apuntaban a ningún doc)."""
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
    """El doc como archivo `.md`: front-matter (con los links como rutas) + cuerpo."""
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
    titulo = f"{area} — {tema.replace('_', ' ')}"
    if tipo == "ref":
        # la referencia no abre ni cierra: sin fecha en el nombre, se busca por tema
        name = f"{area}_{tema}"
        body = templates.body(store.config.templates_dir, "ref", titulo)
        meta = _meta_base(store, "ref", None, repo)
    else:
        infijo = "_BUG_" if tipo == "bug" else "_"
        name = f"{clock.today()}_{area}{infijo}{tema}"
        body = templates.body(store.config.templates_dir, tipo, titulo)
        meta = _meta_base(store, tipo, "en_progreso", repo)
    doc_id = new_doc_id()
    with store.writer() as w:
        if name_taken(store, name):
            raise CairnError(f"Ya existe: {name}")
        w.emit("create", doc_id, {"name": name, "meta": meta, "body": body})
    return Result(get(store, doc_id))


def close(store: Store, ref: str, force: bool = False) -> Result:
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
        # un doc con checklist abierta no está cerrado: cerrarlo lo saca de `list` y lo reporta
        # en `weekly` como trabajo terminado, que es justo lo que no es
        pend = pending_items(doc.body)
        if pend and not force:
            lines = "\n".join(f"    {n}: {t}" for n, t in pend[:10])
            raise CairnError(f"[aviso] {doc.name} tiene {len(pend)} pendiente(s) sin tildar:\n{lines}\n\n"
                             f"Tildalos, borralos, o cerralo igual con: close {doc.name} --force")
        if doc.meta.get("nota"):
            # la nota describía el trabajo abierto ("baja prioridad", "reabierto por…"); cerrado, miente
            warnings.append(f"[nota] Nota descartada al cerrar: \"{doc.meta['nota']}\" — si tenía info "
                            "del cierre, pasala al cuerpo del doc.")
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
            # tomado por error o la conversación se cortó: vuelve a esperar a la próxima
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
    """Un diagnóstico cuenta QUÉ PASÓ; la topología descubierta o los comandos para
    re-diagnosticar dicen CÓMO ES algo. Copia (no mueve) esas secciones a una referencia y deja
    los dos docs vinculados (`origen` / `referencias`)."""
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

        # juntar primero: si no hay nada que llevar, no se crea ni se toca nada
        bloque, llevadas, salteadas = "", [], []
        for s in secciones:
            raw = section_raw(src.body, s)
            if not raw:
                salteadas.append(f"{s} (no existe)")
                continue
            heading, cuerpo = raw
            cuerpo = COMMENT.sub("", cuerpo).strip("\n")
            if is_placeholder(cuerpo):
                salteadas.append(f"{heading} (vacía)")
                continue
            llevadas.append(heading)
            bloque += f"\n---\n\n## {heading}\n\n{cuerpo.strip()}\n"
        if not llevadas:
            raise CairnError(f"Nada para promover de {src.name}: {', '.join(salteadas)}\n"
                             "Elegí secciones con --seccion \"<inicio del título>\".")

        nueva = target is None
        if nueva:
            ref_id = new_doc_id()
            body = templates.body(store.config.templates_dir, "ref_header",
                                  f"{area} — {tema.replace('_', ' ')}")
            w.emit("create", ref_id, {"name": ref_name, "body": body,
                                      "meta": _meta_base(store, "ref", None, src.repo)})
        else:
            ref_id, body = target.id, target.body
        w.emit("promote", ref_id, {"body": body.rstrip("\n") + "\n" + bloque,
                                   "set": {"actualizado": clock.today()},
                                   "link_add": [["origen", src.id]]})
        if ["referencias", ref_id] not in src.links:
            w.emit("link", src.id, {"link_add": [["referencias", ref_id]]})
    if salteadas:
        warnings.append(f"  salteadas: {', '.join(salteadas)}")
    if nueva:
        warnings.append("[nota] Referencia nueva: completá el TL;DR y reescribí lo copiado como "
                        "referencia\n    (lo que ES, no lo que pasó) — sacá el contexto del incidente "
                        "que no aplique.")
    else:
        warnings.append("[nota] Revisá que lo agregado no contradiga lo que la referencia ya decía.")
    return Result(get(store, ref_id), warnings, {"llevadas": llevadas})


# ──────────────────────────────────────────────────────────────── search / weekly

@dataclass
class Hit:
    doc: Doc
    lines: list[tuple[int, str]]


def search(store: Store, pattern: str, scopes: tuple[str, ...] = SCOPES) -> tuple[list[Hit], int]:
    """Regex de Python, case-insensitive, sobre el doc como archivo (front-matter incluido, así
    los números de línea coinciden con `show`). Devuelve (coincidencias, docs revisados)."""
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


def search_fts(store: Store, query: str, scopes: tuple[str, ...] = SCOPES, limit: int = 20) -> list[Doc]:
    """Búsqueda de texto completo (sintaxis FTS5, sin acentos ni mayúsculas), por relevancia."""
    marks = ",".join("?" * len(scopes))
    try:
        rows = store.projection.db.execute(
            f"SELECT d.* FROM docs_fts f JOIN docs d ON d.id = f.doc_id "
            f"WHERE docs_fts MATCH ? AND d.scope IN ({marks}) ORDER BY bm25(docs_fts) LIMIT ?",
            (query, *scopes, limit)).fetchall()
    except sqlite3.OperationalError as e:      # sintaxis FTS5 inválida
        raise CairnError(f"Consulta FTS inválida: {e}") from None
    return [_doc_from_row(store, r) for r in rows]


def weekly(store: Store, dias: int = 7) -> tuple[str, list[Doc]]:
    """Lo cerrado en los últimos `dias` (sin handoffs: no son trabajo cerrado). (desde, docs)."""
    since = (date.fromisoformat(clock.today()) - timedelta(days=dias)).isoformat()
    docs = [d for d in all_docs(store, ("done",)) if d.tipo != "handoff" and d.date_closed() >= since]
    docs.sort(key=lambda d: d.date_closed(), reverse=True)
    return since, docs


# ──────────────────────────────────────────────────────────────── check

def check(store: Store) -> list[str]:
    """Lo que no está en orden: el log (líneas corruptas, firmas inválidas, eventos sin
    proyectar, ramas divergentes) y los docs (estado incoherente, nombres, links rotos, handoffs viejos)."""
    problemas: list[str] = []
    en_log = 0
    problemas += store.verifier.bad_peers
    for entry in store.log.read_all():
        why = entry.error if entry.event is None else store.verifier.problem(entry.event)
        if why:
            problemas.append(f"logs/{entry.node}/{entry.segment}.jsonl byte {entry.offset}: {why}")
        else:
            en_log += 1
    proyectados = store.projection.count_events()
    if proyectados != en_log:
        problemas.append(f"la proyección tiene {proyectados} evento(s) y el log {en_log} — "
                         "correr `cairn rebuild`")
    db = store.projection.db
    for r in db.execute("SELECT status, count(*) n FROM events WHERE status NOT IN ('applied', 'resolved') "
                      "GROUP BY status"):
        que = {"divergent": "en ramas divergentes (dos nodos escribieron sobre el mismo head)",
               "orphan": "huérfanos (falta el evento anterior: sincronizar)"}.get(r["status"], r["status"])
        problemas.append(f"{r['n']} evento(s) {que}")
    for r in db.execute("SELECT name, count(*) n FROM docs GROUP BY name HAVING n > 1"):
        problemas.append(f"{r['name']}: {r['n']} docs con el mismo nombre")
    ids = {r["id"] for r in db.execute("SELECT id FROM docs")}
    for doc in all_docs(store):
        n = doc.name
        if doc.divergent:
            problemas.append(f"{n} tiene ramas divergentes: `cairn resolve {n}` para verlas y elegir")
        if doc.meta.get("tipo") not in TIPOS_DOC:
            problemas.append(f"{n} tiene tipo '{doc.meta.get('tipo', '')}' (válidos: {', '.join(TIPOS_DOC)})")
        if doc.scope != "ref":
            if not re.match(r"^\d{4}-\d{2}-\d{2}_", n):
                problemas.append(f"{n} no arranca con fecha YYYY-MM-DD_")
            if doc.estado not in ABIERTOS | CERRADOS:
                problemas.append(f"{n} tiene estado '{doc.estado}' (desconocido)")
        if doc.tipo == "handoff":
            if doc.scope == "done" and not (doc.meta.get("usada") or doc.meta.get("descartada")):
                problemas.append(f"{n} es un handoff archivado sin `usada:` ni `descartada:`")
            edad = handoff_edad(doc)
            if doc.scope == "wip" and edad is not None and edad > store.config.handoff_stale_days:
                problemas.append(f"{n} es un handoff sin tomar hace {edad} días — "
                                 "`handoff take` o `handoff drop`")
        elif doc.scope == "done" and not doc.meta.get("cerrado"):
            problemas.append(f"{n} está cerrado sin `cerrado:` — weekly cae a la última modificación")
        for kind, dst in doc.links:
            if dst not in ids:
                problemas.append(f"{n}: el link `{kind}` apunta a un doc que no existe ({dst[:8]})")
    return problemas


# ──────────────────────────────────────────────────────────────── handoffs

def _como_lista(v) -> list[str]:
    if not v:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def handoff_para(doc: Doc) -> list[str]:
    """A qué conversaciones le toca: `para:`, o si falta, el `repo:` del doc."""
    return _como_lista(doc.meta.get("para")) or _como_lista(doc.meta.get("repo"))


def handoff_edad(doc: Doc) -> int | None:
    try:
        return (date.fromisoformat(clock.today()) - date.fromisoformat(str(doc.meta["fecha"]))).days
    except (KeyError, TypeError, ValueError):
        return None


def handoffs_pendientes(store: Store, repo: str) -> list[Doc]:
    """Los handoffs pendientes para una conversación en `repo`. Desde la raíz de proyectos
    (repo "") se ven todos; desde un repo, los que lo nombran en `para:`."""
    out = [d for d in all_docs(store, ("wip",))
           if d.tipo == "handoff" and d.estado == "pendiente" and (not repo or repo in handoff_para(d))]
    return sorted(out, key=lambda d: d.name)


def modificado(doc: Doc) -> date:
    """La fecha (local) del último cambio del doc."""
    ts = datetime.fromisoformat(doc.updated)
    tz = clock.now().tzinfo if os.environ.get("CAIRN_NOW") else None
    return ts.astimezone(tz).date()


def modificado_label(doc: Doc) -> str:
    d = modificado(doc)
    dias = (date.fromisoformat(clock.today()) - d).days
    return f"modificado {d.isoformat()}, " + ("hoy" if dias <= 0 else f"hace {dias} día(s)")


def por_repo(lista: list[Doc]) -> list[str]:
    """Las líneas de un listado agrupado por repo, con cuántos hay en cada uno y cuándo se tocó
    cada doc por última vez. `lista` viene del más reciente al más viejo (`all_docs`): los grupos
    quedan en ese orden (el del doc más reciente de cada uno) y los docs adentro también."""
    grupos: dict[str, list[Doc]] = {}
    for d in lista:
        grupos.setdefault(repo_label(d), []).append(d)
    out = [f"{len(lista)} doc(s) en {len(grupos)} repo(s); grupos y docs del modificado más "
           "recientemente al más viejo"]
    for repo, ds in grupos.items():
        out += ["", f"{repo} ({len(ds)})"]
        out += [f"  {d.name}  [{d.tipo} · {estado_label(d)} · {modificado_label(d)}]" for d in ds]
    return out


def handoff_linea(doc: Doc, viejo: int = 14) -> str:
    """`viejo`: a partir de cuántos días se marca como vieja (`[docs] handoff_stale_days`)."""
    edad = handoff_edad(doc)
    partes = [f"para: {', '.join(handoff_para(doc)) or 'cross-repo'}"]
    if edad is not None:
        partes.append("de hoy" if edad == 0 else f"hace {edad} día(s)")
        if edad > viejo:
            partes.append("VIEJA: revisar si sigue valiendo")
    return f"{doc.name}  ({'; '.join(partes)})"


def handoff_new(store: Store, area: str, tema: str, para: list[str] | None = None,
                planes: list[str] | None = None, repo: str = "") -> Result:
    _check_name_parts(area, tema)
    para, planes = para or [], planes or []
    name = f"{clock.today()}_{area}_HANDOFF_{tema}"
    meta = _meta_base(store, "handoff", "pendiente", repo)
    if para:
        meta["para"] = para if len(para) > 1 else para[0]
    doc_id = new_doc_id()
    with store.writer() as w:
        if name_taken(store, name):
            raise CairnError(f"Ya existe: {name}")
        links = [["plan", find(store, p).id] for p in planes]
        body = templates.body(store.config.templates_dir, "handoff",
                              f"{area} — handoff: {tema.replace('_', ' ')}")
        w.emit("handoff_new", doc_id, {"name": name, "meta": meta, "body": body, "links": links})
    warnings = []
    if not (para or repo):
        warnings.append("[nota] Sin `repo:` ni `--para`: el aviso sólo aparece en conversaciones "
                        "abiertas en la raíz de proyectos.")
    return Result(get(store, doc_id), warnings)


def _handoff_elegir(store: Store, ref: str | None, repo: str) -> Doc:
    if ref:
        doc = find(store, ref)
        if doc.tipo != "handoff":
            raise CairnError(f"{doc.name} no es un handoff (tipo: {doc.meta.get('tipo', '?')}).")
        if doc.estado != "pendiente":
            raise CairnError(f"{doc.name} ya no está pendiente: {doc.estado} el {doc.date_closed()} "
                             "(la tomó otra conversación o se descartó). `reopen` la vuelve a pendiente.")
        return doc
    pend = handoffs_pendientes(store, repo)
    donde = repo or "la raíz de proyectos"
    if not pend:
        raise CairnError(f"No hay handoffs pendientes para {donde}.")
    if len(pend) > 1:
        raise CairnError(f"Hay {len(pend)} handoffs pendientes para {donde}; decí cuál:\n"
                         + "\n".join(f"  {handoff_linea(d, store.config.handoff_stale_days)}" for d in pend))
    return pend[0]


def _handoff_archivar(store: Store, ref: str | None, repo: str, estado: str,
                      motivo: str | None) -> tuple[Doc, Doc]:
    """(antes, después). Bajo el lock del writer: si dos conversaciones la toman a la vez, la
    segunda ve que ya no está pendiente y no la usan las dos."""
    with store.writer() as w:
        doc = _handoff_elegir(store, ref, repo)
        fecha = "usada" if estado == "usada" else "descartada"
        change: dict = {"set": {"estado": estado, fecha: clock.today()}, "unset": ["reabierto"]}
        if motivo:
            change["set"]["nota"] = motivo
        else:
            change["unset"].append("nota")
        w.emit("handoff_take" if estado == "usada" else "handoff_drop", doc.id, change)
    return doc, get(store, doc.id)


def handoff_take(store: Store, ref: str | None = None, repo: str = "") -> Result:
    antes, doc = _handoff_archivar(store, ref, repo, "usada", None)
    avisos = []
    edad = handoff_edad(doc)
    if edad is not None and edad > store.config.handoff_stale_days:
        avisos.append(f"La nota tiene {edad} días: puede haber quedado vieja.")
    for kind, dst in doc.links:
        if kind != "plan":
            continue
        plan = get(store, dst)
        if plan is None:
            avisos.append(f"El plan {dst[:8]} no existe.")
            continue
        if plan.scope == "done":
            avisos.append(f"El plan {plan.name} ya está cerrado.")
        if plan.updated > antes.updated:
            avisos.append(f"El plan {plan.name} se modificó después de escrita la nota: "
                          "si se contradicen, vale el plan.")
    for raw in _como_lista(doc.meta.get("plan")):
        avisos.append(f"El plan {raw} no está en la base.")
    return Result(doc, avisos)


def handoff_drop(store: Store, ref: str, motivo: str | None = None) -> Result:
    _, doc = _handoff_archivar(store, ref, "", "descartada", motivo)
    return Result(doc)


def handoff_hook_context(store: Store, repo: str, cmd: str = "cairn") -> str | None:
    """El texto que el hook de SessionStart agrega como contexto, o None si no hay pendientes."""
    pend = handoffs_pendientes(store, repo)
    if not pend:
        return None
    lineas = [f"cairn: hay {len(pend)} handoff(s) pendiente(s) para {repo or 'la raíz de proyectos'} "
              "(notas que dejó una conversación anterior para esta):"]
    lineas += [f"  - {handoff_linea(d, store.config.handoff_stale_days)}" for d in pend]
    lineas += ["No la tomes ni sigas lo que dice por tu cuenta: en tu primera respuesta, avisale "
               "al usuario que está y preguntale si la toma.",
               f"Tomarla: tool `cairn_handoff_take` del MCP de cairn, o `{cmd} handoff take <nota>` "
               "(la devuelve y la archiva). Descartarla: `cairn_handoff_drop`, o "
               f"`{cmd} handoff drop <nota> [motivo]`."]
    return "\n".join(lineas)
