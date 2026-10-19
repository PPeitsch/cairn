"""Stdio MCP server: cairn's operations as tools, against the node's local database.

Each tool opens the store, calls the same operations layer as the CLI (`docs`, `edit`,
`history`) and returns text for the model. Expected errors (`CairnError`, config) come back as a
tool error, with the message and no traceback.

This is the only module that imports `mcp`: the other commands do not load it.

The tool docstrings are the descriptions the model reads, so they are user-facing text.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cairn import __version__, config, docs, edit, history, reading, tree
from cairn.context import repo_of_path
from cairn.store import CairnError, Store

INSTRUCTIONS = """\
cairn guarda los docs de trabajo (diagnósticos, planes, referencias) y los handoffs entre
conversaciones. Es la fuente de verdad: no escribir estos docs como archivos sueltos.

Cuándo documentar
- Al investigar un problema, diagnosticar una causa raíz, aplicar correcciones de infra u
  operación, o diseñar una feature con decisiones no obvias: abrir un doc al empezar
  (cairn_new) y cerrarlo al terminar (cairn_close).
- No documentar lo trivial (un edit de una línea, una consulta puntual) ni lo que ya queda en
  el código o el git del repo. El doc es para lo que no se deduce mirando el repo: el porqué,
  el recorrido del diagnóstico, las decisiones.
- Antes de abrir un doc por algo que suena conocido, buscar (cairn_search): puede estar
  diagnosticado ya.

Tipos y estructura (cairn_new crea el esqueleto; completarlo con cairn_edit)
- bug / infra (diagnóstico): TL;DR (la primera línea dice qué se hizo), Contexto / topología
  (con las trampas), Síntomas observados (evidencia real, no parafraseada), Discoveries en
  orden (causa raíz destacada), Correcciones aplicadas, Pendientes (`- [ ]`), Comandos útiles.
- plan: TL;DR, Contexto / problema, Alcance (qué incluye y qué no), Diseño / propuesta (fases
  si es incremental), Decisiones tomadas (alternativas descartadas y por qué), Pendientes.
- ref: material de consulta escrito en presente (cómo ES algo hoy). No se abre ni se cierra:
  se actualiza. Un doc cerrado cuenta qué pasó; una referencia, cómo es algo. Al cerrar,
  preguntarse si algo se va a volver a consultar aunque el problema no vuelva: si sí,
  cairn_promote lleva esas secciones a una referencia y deja los dos docs vinculados.
  Si una referencia es un relevamiento con hallazgos (un review), marcar el estado de cada uno
  (OK / parcial / pendiente) y, al re-verificarlo, contra qué commit o versión.

Buscar y leer sin gastar contexto
- Para una pregunta por tema ("¿qué tenemos sobre X?"): cairn_search con fts=true, que ordena
  por relevancia y muestra el fragmento que coincide. La regex, para nombres o identificadores
  exactos; una regex amplia (palabras comunes con `|`) trae mucho y sirve poco.
- Leer por partes: cairn_show de un doc largo devuelve su índice; pedir la sección
  (`seccion=`) o las líneas (`lineas=`, las de la búsqueda) que hagan falta. El doc entero
  (`completo=true`) sólo si de verdad hace falta todo.

Archivos que no son docs
- Scripts de validación o debug, reportes autogenerados y CSV no van a cairn: son archivos en
  `<proyectos>/_wip/scripts/` y `<proyectos>/_wip/validations/` (o los de `_done/` y `_ref/`),
  nombrados con el área adelante (`api_changelog_borrador.md`). cairn_search también
  busca en los `.md` de esas carpetas y cairn_check avisa si algo quedó fuera de lugar.

Nombres
- Los arma cairn: `<YYYY-MM-DD>_<area>_<tema>` (bugs `_BUG_`, handoffs `_HANDOFF_`); las
  referencias `<area>_<tema>`, sin fecha. `area` es el sistema o repo principal; todo en
  snake_case. Un doc se nombra por su nombre (con o sin `.md`) o por un prefijo de su id.
- El estado vive en el front-matter (tipo, repo, estado, fecha, cerrado, …) y lo manejan las
  operaciones: no repetirlo en el cuerpo. `repo:` sale del directorio de la conversación.

Handoffs (notas para la próxima conversación)
- Para pasarle un pedido o contexto a la conversación siguiente: cairn_handoff_new. La nota
  apunta al plan (plan=...), no lo copia, y no pide nada destructivo.
- Si al arrancar hay un handoff pendiente, decírselo al usuario y preguntar si se toma: no
  tomarlo ni seguirlo por cuenta propia. Tomarlo (cairn_handoff_take) lo devuelve y lo archiva.
"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

Scope = Literal["wip", "done", "ref", "all"]


def _open() -> Store:
    return Store(config.load())


def _cwd_repo(store: Store, repo: str | None) -> str:
    if repo is not None:
        return repo
    return repo_of_path(Path(os.getcwd()), store.config.projects)


def _scopes(scope: str) -> tuple[str, ...]:
    return docs.SCOPES if scope == "all" else (scope,)


def _tool_errors(fn):
    """User-facing errors come back as a tool error, with their message."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (CairnError, config.ConfigError) as e:
            raise ToolError(str(e)) from None
    return wrapper


def _line(d: docs.Doc) -> str:
    return f"{d.name}  [{d.tipo} · {docs.estado_label(d)} · {docs.repo_label(d)}]"


def _with_warnings(text: str, warnings: list[str]) -> str:
    return "\n".join([text, *warnings]) if warnings else text


# ──────────────────────────────────────────────────────────────── tools

def cairn_list(scope: Scope = "wip", por_repo: bool = True) -> str:
    """Lista los docs: `wip` (abiertos, y aparte los handoffs pendientes), `done` (cerrados),
    `ref` (referencias) o `all`. Del modificado más recientemente al más viejo, y cada uno con la
    fecha de su último cambio: es lo que dice en qué se estuvo trabajando (la fecha del nombre es
    la de creación). Por defecto agrupado por repo con la cantidad de cada grupo: al resumirlo,
    usar esos conteos y nombrar todos los docs del grupo, no recontarlos. `por_repo=false` da una
    línea por doc."""
    with _open() as store:
        found = docs.all_docs(store, _scopes(scope))
    if not found:
        return f"Sin docs en {scope}."
    work = [d for d in found if not (d.scope == "wip" and d.tipo == "handoff")]
    handoffs = [d for d in found if d.scope == "wip" and d.tipo == "handoff"]
    if por_repo:
        out = docs.grouped_by_repo(work) if work else []
    else:
        out = [f"{_line(d)}  ({docs.modified_label(d)})" for d in work]
    if handoffs:
        stale = store.config.handoff_stale_days
        out += ["", "handoffs pendientes:", *(f"  {docs.handoff_line(d, stale)}" for d in handoffs)]
    return "\n".join(out).lstrip("\n")


def cairn_search(pattern: str, scope: Scope = "all", fts: bool = False) -> str:
    """Busca en todos los docs. Por defecto `pattern` es una regex (case-insensitive) sobre el doc
    como archivo, y devuelve las líneas que coinciden (con su número: sirve para cairn_show con
    `lineas`), de los docs con más coincidencias primero y con un tope; los demás, sólo por
    nombre. La regex es para nombres o identificadores exactos; para un tema, `fts=true`: una
    consulta de texto completo (sintaxis FTS5, sin acentos ni mayúsculas) por relevancia; cada resultado
    trae el fragmento donde coincide, con lo encontrado entre «»; si hay más de 20, lo dice."""
    with _open() as store:
        if fts:
            found, total = docs.search_fts(store, pattern, _scopes(scope))
            if not found:
                return f'Búsqueda FTS "{pattern}": sin coincidencias.'
            return "\n".join([docs.fts_header(pattern, found, total),
                              *(f"{_line(h.doc)}\n    {h.snippet}" for h in found)])
        hits, total = docs.search(store, pattern, _scopes(scope))
        files = tree.search_files(store, pattern, _scopes(scope))
    if not hits and not files:
        return f'Búsqueda "{pattern}": sin coincidencias en {total} docs.'
    max_docs, max_lines = store.config.search_max_docs, store.config.search_max_lines
    ranked = sorted(hits, key=lambda h: -len(h.lines))     # stable: ties keep repo and date order
    shown, rest = ranked[:max_docs], ranked[max_docs:]
    out = [f'Búsqueda "{pattern}": {len(hits)} doc(s) de {total}'
           + (f"; se muestran los {len(shown)} con más coincidencias" if rest else "")]
    for h in shown:
        out.append(_line(h.doc))
        out += [f"    {n}: {line.strip()[:160]}" for n, line in h.lines[:max_lines]]
        if len(h.lines) > max_lines:
            out.append(f"    (+{len(h.lines) - max_lines} más)")
    if rest:
        names = ", ".join(f"{h.doc.name} ({len(h.lines)})" for h in rest[:25])
        extra = f" y {len(rest) - 25} más" if len(rest) > 25 else ""
        out += ["", f"Otros {len(rest)} doc(s) con menos coincidencias: {names}{extra}.",
                "Para acotar: un patrón más preciso, fts=true (por relevancia) o "
                "cairn_show con lineas= alrededor de una coincidencia."]
    if files:
        out += ["", f"archivos de scripts/ y validations/ ({len(files)}):"]
        for f in files:
            out.append(f"  {f.path}")
            out += [f"    {n}: {line.strip()[:160]}" for n, line in f.lines[:5]]
    return "\n".join(out)


def cairn_show(doc: str, seccion: str | None = None, lineas: str | None = None,
               completo: bool = False) -> str:
    """Muestra un doc como archivo (front-matter + cuerpo). Un doc largo (más de
    `[mcp] show_max_chars`, 8000 por defecto) viene como índice: front-matter, título, TL;DR y
    las secciones con sus líneas y tamaño. Para leer una parte: `seccion` (el comienzo del título,
    p. ej. "Decisiones") o `lineas` ("40-80", con los números que da cairn_search), que vienen
    numeradas; `completo=true` da el doc entero. `doc@<hash>` o `doc@~N` muestra una revisión
    anterior (ver cairn_log)."""
    with _open() as store:
        _, text = history.show(store, doc)
        max_chars = store.config.show_max_chars
    return reading.view(text, seccion, lineas, completo, max_chars)


def cairn_new(area: str, tema: str, tipo: Literal["bug", "infra", "plan", "ref"] = "infra",
              body: str | None = None, repo: str | None = None) -> str:
    """Crea un doc: diagnóstico (`bug` o `infra`), `plan` o referencia (`ref`), con el esqueleto
    de su tipo. `area` y `tema` en snake_case. `body` (opcional) reemplaza el esqueleto: el
    cuerpo en markdown sin front-matter, empezando por el título `# ...`. `repo`: a qué repo
    pertenece (default: el del directorio de la conversación; `varios` si es cross-repo).
    Devuelve el nombre del doc y su contenido."""
    with _open() as store:
        r = docs.new(store, area, tema, tipo, _cwd_repo(store, repo))
        if body is not None:
            r = edit.apply(store, r.doc.id, body=body)
        return f"Creado: {r.doc.name}\n\n{docs.render(store, r.doc)}"


def cairn_edit(doc: str, old_text: str | None = None, new_text: str | None = None,
               body: str | None = None, set_fields: dict[str, str] | None = None,
               unset_fields: list[str] | None = None) -> str:
    """Edita un doc. Para cambiar una parte: `old_text` (tiene que aparecer exactamente una vez en
    el cuerpo) y `new_text`. Para reemplazar el cuerpo entero: `body` (markdown sin front-matter).
    `set_fields` / `unset_fields`: campos del front-matter (p. ej. `nota`, `estado` dentro del
    mismo grupo abierto/cerrado). Abrir, cerrar y vincular no se hace editando: para eso están
    cairn_close, cairn_reopen, cairn_promote y los handoffs."""
    if (old_text is None) != (new_text is None):
        raise ToolError("old_text y new_text van juntos")
    if old_text is not None and body is not None:
        raise ToolError("old_text/new_text o body, no los dos")
    with _open() as store:
        d = docs.find(store, doc)
        new_body = body
        if old_text is not None:
            n = d.body.count(old_text)
            if n != 1:
                raise ToolError(f"old_text aparece {n} veces en el cuerpo de {d.name}: tiene que "
                                "aparecer exactamente una (sumá contexto)")
            new_body = d.body.replace(old_text, new_text, 1)
        r = edit.apply(store, d.id, body=new_body, set_=set_fields, unset=unset_fields,
                       expected_head=d.head)
    outcome = "Guardado" if r.extra.get("changed") else "Sin cambios"
    return _with_warnings(f"{outcome}: {r.doc.name}", r.warnings)


def cairn_close(doc: str, force: bool = False, descartar: str | None = None) -> str:
    """Cierra un diagnóstico o plan (estado resuelto/completado + fecha de cierre). Falla si quedan
    `- [ ]` sin tildar, salvo `force=true`. Si se deja sin terminar (se abandona, deja de tener
    sentido), `descartar` con el motivo: queda `descartado`, el motivo en `nota:`, y no pide
    `force`. Las referencias no se cierran y los handoffs se archivan con cairn_handoff_take /
    cairn_handoff_drop."""
    with _open() as store:
        r = docs.close(store, doc, force=force, descartar=descartar)
    return _with_warnings(f"Cerrado: {r.doc.name} ({r.doc.estado})", r.warnings)


def cairn_reopen(doc: str, motivo: str | None = None) -> str:
    """Reabre un doc cerrado (o vuelve un handoff archivado a pendiente). `motivo` queda en
    `nota:`."""
    with _open() as store:
        r = docs.reopen(store, doc, motivo)
    return _with_warnings(f"Reabierto: {r.doc.name}", r.warnings)


def cairn_promote(doc: str, area: str, tema: str, secciones: list[str] | None = None,
                  force: bool = False) -> str:
    """Copia secciones de un doc a la referencia `<area>_<tema>` (la crea si no existe) y deja
    los dos vinculados. `secciones`: inicio de los títulos (default: Contexto + Comandos útiles
    en un diagnóstico, Diseño + Decisiones en un plan). Después hay que reescribir lo copiado
    como referencia: lo que ES, no lo que pasó."""
    with _open() as store:
        r = docs.promote(store, doc, area, tema, secciones, force=force)
    return _with_warnings(f"{r.doc.name}\n  llevadas: {', '.join(r.extra['carried'])}", r.warnings)


def cairn_weekly(dias: int = 7) -> str:
    """Lo cerrado en los últimos `dias` días, con su TL;DR (sin handoffs)."""
    with _open() as store:
        since, closed = docs.weekly(store, dias)
    if not closed:
        return f"Nada cerrado desde {since}."
    out = [f"{len(closed)} doc(s) cerrado(s) desde {since}:"]
    for d in closed:
        how = "descartado" if d.estado == "descartado" else "cerrado"
        out += ["", f"{d.title}  [{d.tipo} · {how} {d.date_closed()} · {docs.repo_label(d)}]",
                d.tldr or "(sin TL;DR)"]
    return "\n".join(out)


def cairn_check() -> str:
    """Verifica que la base esté en orden: log, proyección, docs (estado, nombres, links,
    handoffs viejos) y la raíz de proyectos (docs sueltos fuera de cairn, archivos mal ubicados)."""
    with _open() as store:
        problems = docs.check(store) + tree.problems(store)
    if not problems:
        return "Todo en orden."
    return "\n".join([f"{len(problems)} problema(s):", *(f"  - {p}" for p in problems)])


def cairn_log(doc: str | None = None, n: int = 20) -> str:
    """Historial de eventos de un doc (o de toda la base), los más nuevos primero. Cada línea
    empieza con el hash de la revisión, que sirve para cairn_show `doc@<hash>` y cairn_diff."""
    with _open() as store:
        lines = history.log(store, doc, n)
    if not lines:
        return "Sin eventos."
    return "\n".join(
        f"{ln.event_id[:10]}  {ln.ts[:19].replace('T', ' ')}  {ln.node}  {ln.op}  {ln.doc_name}"
        + ("" if ln.status == "applied" else f"  [{ln.status}]") for ln in lines)


def cairn_diff(doc: str, rev_a: str | None = None, rev_b: str | None = None) -> str:
    """Diff unificado entre dos revisiones de un doc (hash o `~N`). Sin revisiones: el último
    cambio; con una: desde ahí hasta el estado actual."""
    with _open() as store:
        out = history.diff(store, doc, rev_a, rev_b)
    return out or "Sin diferencias."


def cairn_handoff_new(area: str, tema: str, body: str | None = None, para: list[str] | None = None,
                      plan: list[str] | None = None, repo: str | None = None) -> str:
    """Deja una nota para la próxima conversación. `para`: los repos a los que les toca (default:
    el `repo:` de la nota, que sale del directorio de la conversación). `plan`: los docs a los
    que apunta. `body`: la nota en markdown sin front-matter (título `# ...`, TL;DR, qué leer
    primero, orden de trabajo, qué preguntar). La nota apunta al plan, no lo copia."""
    with _open() as store:
        r = docs.handoff_new(store, area, tema, para, plan, _cwd_repo(store, repo))
        warnings = r.warnings
        if body is not None:
            r = edit.apply(store, r.doc.id, body=body)
        return _with_warnings(f"Handoff creado: {r.doc.name}\n\n{docs.render(store, r.doc)}", warnings)


def cairn_handoff_list(all_repos: bool = False, repo: str | None = None) -> str:
    """Los handoffs pendientes para el repo de la conversación (o `repo`), o todos con
    `all_repos=true`."""
    with _open() as store:
        r = "" if all_repos else _cwd_repo(store, repo)
        pend = docs.pending_handoffs(store, r)
    where = r or "toda la raíz de proyectos"
    if not pend:
        return f"Sin handoffs pendientes para {where}."
    stale = store.config.handoff_stale_days
    return "\n".join([f"Handoffs pendientes para {where}:",
                      *(f"  {docs.handoff_line(d, stale)}" for d in pend)])


def cairn_handoff_take(nota: str | None = None, repo: str | None = None) -> str:
    """Toma un handoff: lo devuelve y lo archiva (estado usada). Sólo cuando el usuario dijo que
    se toma. Sin `nota`, la única pendiente para el repo de la conversación."""
    with _open() as store:
        r = docs.handoff_take(store, nota, _cwd_repo(store, repo))
    warnings = [f"[aviso] {a}" for a in r.warnings]
    return _with_warnings(f"Handoff tomado: {r.doc.name} (archivado, estado: usada)", warnings) \
        + "\n\n" + r.doc.body


def cairn_handoff_drop(nota: str, motivo: str | None = None) -> str:
    """Descarta un handoff sin usarlo (lo archiva, estado descartada). `motivo` queda en `nota:`."""
    with _open() as store:
        r = docs.handoff_drop(store, nota, motivo)
    return f"Handoff descartado: {r.doc.name} (archivado)"


TOOLS = [
    (cairn_list, READ), (cairn_search, READ), (cairn_show, READ), (cairn_weekly, READ),
    (cairn_check, READ), (cairn_log, READ), (cairn_diff, READ), (cairn_handoff_list, READ),
    (cairn_new, WRITE), (cairn_edit, WRITE), (cairn_close, WRITE), (cairn_reopen, WRITE),
    (cairn_promote, WRITE), (cairn_handoff_new, WRITE), (cairn_handoff_take, WRITE),
    (cairn_handoff_drop, WRITE),
]


def instructions() -> str:
    """The ones in `[mcp] instructions` if the config sets them; otherwise the built-in ones."""
    try:
        path = config.load().mcp_instructions
    except config.ConfigError:
        return INSTRUCTIONS
    if path is None:
        return INSTRUCTIONS
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise CairnError(f"[mcp] instructions: no se pudo leer {path}: {e}") from None


def build_server() -> MCPServer:
    server = MCPServer("cairn", instructions=instructions(), version=__version__)
    for fn, hints in TOOLS:
        server.add_tool(_tool_errors(fn), annotations=hints, structured_output=False)
    return server


def run() -> None:
    build_server().run("stdio")
