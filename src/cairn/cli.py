"""Punto de entrada del comando `cairn`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cairn import __version__, clock, config, docs, edit, history, transfer
from cairn.context import repo_of_path
from cairn.markdown import first_sentences, fmt_date
from cairn.store import CairnError, Store


def _open() -> Store:
    return Store(config.load())


def warn(msg: str) -> None:
    print(msg, file=sys.stderr)


def _warn_all(lines: list[str]) -> None:
    for line in lines:
        warn(line)


def _cwd_repo(store: Store) -> str:
    return repo_of_path(Path.cwd(), store.config.projects)


def _scopes(scope: str) -> tuple[str, ...]:
    return docs.SCOPES if scope == "all" else (scope,)


# ──────────────────────────────────────────────────────────────── init / rebuild

def cmd_init(args) -> int:
    path = config.config_dir() / config.CONFIG_NAME
    if path.is_file():
        cfg = config.load()
        print(f"cairn ya está inicializado en este nodo: {cfg.node}")
    else:
        node = args.node or config.default_node_name()
        config.write(node, data_dir=args.data, projects=args.projects, author=args.author)
        cfg = config.load()
        print(f"Nodo inicializado: {cfg.node}")
    with Store(cfg) as store:
        n = store.projection.count_events()
    print(f"  config:    {cfg.path}")
    print(f"  datos:     {cfg.data_dir}")
    print(f"  proyectos: {cfg.projects}")
    print(f"  eventos:   {n}")
    return 0


def cmd_rebuild(args) -> int:
    with _open() as store:
        n = store.rebuild()
        ndocs = len(store.projection.doc_ids())
        _warn_all(f"[aviso] {p}" for p in store.problems)
    print(f"Proyección rehecha desde el log: {n} evento(s), {ndocs} doc(s)")
    return 1 if store.problems else 0


# ──────────────────────────────────────────────────────────────── ciclo de vida

def cmd_new(args) -> int:
    with _open() as store:
        repo = args.repo if args.repo is not None else _cwd_repo(store)
        r = docs.new(store, args.area, args.tema, args.tipo, repo)
    print(r.doc.name)
    return 0


def cmd_close(args) -> int:
    with _open() as store:
        r = docs.close(store, args.doc, force=args.force)
    print(f"Cerrado: {r.doc.name} ({r.doc.estado})")
    _warn_all(r.warnings)
    return 0


def cmd_reopen(args) -> int:
    with _open() as store:
        r = docs.reopen(store, args.doc, args.motivo)
    print(f"Reabierto: {r.doc.name}")
    _warn_all(r.warnings)
    return 0


def cmd_promote(args) -> int:
    with _open() as store:
        r = docs.promote(store, args.doc, args.area, args.tema, args.seccion, force=args.force)
    print(r.doc.name)
    print(f"  llevadas: {', '.join(r.extra['llevadas'])}")
    _warn_all(r.warnings)
    return 0


# ──────────────────────────────────────────────────────────────── consultas

def _print_scope(store: Store, scope: str) -> None:
    """En `wip` los handoffs van aparte: no son trabajo abierto sino notas que esperan a la
    próxima conversación."""
    lista = docs.all_docs(store, (scope,))
    handoffs = [d for d in lista if scope == "wip" and d.tipo == "handoff"]
    for d in lista:
        if d not in handoffs:
            print(d.name)
    if handoffs:
        print("\nhandoffs pendientes:")
        for d in handoffs:
            print(f"  {d.name}")


def cmd_list(args) -> int:
    with _open() as store:
        if args.scope == "all":
            for s in docs.SCOPES:
                print(f"=== {s} ===")
                _print_scope(store, s)
        else:
            _print_scope(store, args.scope)
    return 0


def _resumen(doc: docs.Doc) -> str:
    r = first_sentences(doc.tldr, 1)
    return r[:197] + "..." if len(r) > 200 else r


def cmd_search(args) -> int:
    with _open() as store:
        if args.fts:
            found = docs.search_fts(store, args.patron, _scopes(args.scope))
            if not found:
                print(f"Búsqueda FTS \"{args.patron}\" — sin coincidencias")
                return 0
            print(f"Búsqueda FTS \"{args.patron}\" — {len(found)} doc(s), por relevancia\n")
            for d in found:
                print(f"  {docs.ICON[d.tipo]}  {d.title}   ·   {docs.estado_label(d)}   ·   "
                      f"{docs.repo_label(d)}")
                print(f"      {d.path}")
                if r := _resumen(d):
                    print(f"      {r}")
                print()
            return 0
        hits, total = docs.search(store, args.patron, _scopes(args.scope))
    if not hits:
        print(f"Búsqueda \"{args.patron}\" — sin coincidencias en {total} docs")
        return 0
    print(f"Búsqueda \"{args.patron}\" — {len(hits)} doc(s) de {total}")
    current = None
    for h in hits:
        repo = docs.repo_label(h.doc)
        if repo != current:
            n = sum(1 for x in hits if docs.repo_label(x.doc) == repo)
            print(f"\n━━━━━  {repo}  ({n})  ━━━━━\n")
            current = repo
        d = h.doc
        print(f"  {docs.ICON[d.tipo]}  {d.title}   ·   {docs.estado_label(d)}   ·   "
              f"{len(h.lines)} coincidencia(s)")
        print(f"      {d.path}")
        if r := _resumen(d):
            print(f"      {r}")
        for n, line in h.lines[:3]:
            print(f"      │ {n}: {line.strip()[:140]}")
        print()
    return 0


def cmd_weekly(args) -> int:
    with _open() as store:
        since, cerrados = docs.weekly(store, args.dias)
    hoy = clock.today()
    total = len(cerrados)
    if args.formato == "full":
        print(f"## Weekly — docs cerrados entre {since} y {hoy} (últimos {args.dias} días)\n")
        print(f"_{total} doc(s) cerrado(s)_\n")
        for d in cerrados:
            tipo = {"plan": "plan/spec", "bug": "bug"}.get(d.tipo, "diagnóstico")
            print(f"### {d.title}\n_{tipo} · {d.meta.get('estado', '—')} · cerrado {d.date_closed()} · "
                  f"{docs.repo_label(d)}_\n")
            print(f"{d.tldr or '_(sin TL;DR)_'}\n\n---\n")
        return 0
    cuenta = {k: sum(1 for d in cerrados if d.tipo == k) for k in ("bug", "infra", "plan")}
    partes = [f"{docs.ICON[k]} {n} {lbl}" for k, lbl in (("bug", "bugs"), ("infra", "diagnósticos"),
                                                         ("plan", "planes")) if (n := cuenta[k])]
    print(f"Semana {fmt_date(since)} – {fmt_date(hoy)} {hoy[:4]}  ·  {total} trabajos cerrados")
    if partes:
        print("   " + " · ".join(partes))
    current = None
    for d in sorted(cerrados, key=docs.repo_label):
        repo = docs.repo_label(d)
        if repo != current:
            n = sum(1 for x in cerrados if docs.repo_label(x) == repo)
            print(f"\n━━━━━  {repo}  ({n})  ━━━━━\n")
            current = repo
        print(f"  {docs.ICON[d.tipo]}  {d.title}   ·   {fmt_date(d.date_closed())}")
        print(f"      {first_sentences(d.tldr, 2) or '(sin TL;DR)'}\n")
    return 0


def cmd_check(args) -> int:
    with _open() as store:
        problemas = docs.check(store)
        print(f"cairn check   (nodo: {store.config.node}, datos: {store.config.data_dir})")
    for p in problemas:
        print(f"  [aviso] {p}")
    print(f"\n  {len(problemas)} problema(s)" if problemas else "  [OK] todo en orden")
    return 1 if problemas else 0


# ──────────────────────────────────────────────────────────────── handoffs

def cmd_handoff_new(args) -> int:
    with _open() as store:
        repo = args.repo if args.repo is not None else _cwd_repo(store)
        r = docs.handoff_new(store, args.area, args.tema, args.para, args.plan, repo)
    print(r.doc.name)
    _warn_all(r.warnings)
    return 0


def cmd_handoff_take(args) -> int:
    with _open() as store:
        r = docs.handoff_take(store, args.nota, _cwd_repo(store))
    print(f"Handoff tomado: {r.doc.name} (archivado, estado: usada)")
    for a in r.warnings:
        print(f"[aviso] {a}")
    print("\n" + "=" * 72 + r.doc.body.rstrip("\n") + "\n" + "=" * 72)
    return 0


def cmd_handoff_drop(args) -> int:
    with _open() as store:
        r = docs.handoff_drop(store, args.nota, args.motivo)
    print(f"Handoff descartado: {r.doc.name} (archivado)")
    return 0


def cmd_handoff_list(args) -> int:
    with _open() as store:
        repo = "" if args.all else _cwd_repo(store)
        pend = docs.handoffs_pendientes(store, repo)
    donde = repo or "toda la raíz de proyectos"
    if not pend:
        print(f"Sin handoffs pendientes para {donde}.")
        return 0
    print(f"Handoffs pendientes para {donde}:")
    for d in pend:
        print(f"  {docs.handoff_linea(d)}")
    return 0


def cmd_handoff_hook(args) -> int:
    """SessionStart: si hay handoffs pendientes para el repo de la conversación, lo avisa como
    contexto. Nunca frena el arranque: ante cualquier error sale callado."""
    try:
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            data = {}
        # al retomar o después de compactar, la conversación ya lo vio (o ya la tomó)
        if data.get("source") in ("resume", "compact"):
            return 0
        with _open() as store:
            repo = repo_of_path(Path(data.get("cwd") or os.getcwd()), store.config.projects)
            texto = docs.handoff_hook_context(store, repo)
        if texto:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                     "additionalContext": texto}}, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — un hook roto no puede trabar la conversación
        pass
    return 0


# ──────────────────────────────────────────────────────────────── edit / historial

def cmd_edit(args) -> int:
    with _open() as store:
        if args.file or args.stdin or args.set or args.unset:
            text = None
            if args.stdin:
                text = sys.stdin.read()
            elif args.file:
                text = Path(args.file).read_text(encoding="utf-8")
            r = edit.apply(store, args.doc, text=text, set_=edit.parse_assignments(args.set or []),
                           unset=args.unset)
        else:
            r = edit.interactive(store, args.doc)
    print(f"Guardado: {r.doc.name}" if r.extra.get("changed") else f"Sin cambios: {r.doc.name}")
    _warn_all(r.warnings)
    return 0


def cmd_show(args) -> int:
    with _open() as store:
        _, text = history.show(store, args.doc)
    sys.stdout.write(text)
    return 0


def cmd_log(args) -> int:
    with _open() as store:
        lines = history.log(store, args.doc, args.n)
    for ln in lines:
        marca = "" if ln.status == "applied" else f"  [{ln.status}]"
        print(f"{ln.event_id[:10]}  {ln.ts[:19].replace('T', ' ')}  {ln.node:<10} {ln.op:<13} "
              f"{ln.doc_name}{marca}")
    return 0


def cmd_diff(args) -> int:
    with _open() as store:
        out = history.diff(store, args.doc, args.rev_a, args.rev_b)
    sys.stdout.write(out or "Sin diferencias.\n")
    return 0


# ──────────────────────────────────────────────────────────────── import / export

def cmd_import(args) -> int:
    with _open() as store:
        rep = transfer.import_tree(store, Path(args.raiz), dry_run=args.dry_run)
    verbo = "Se importarían" if args.dry_run else "Importados"
    print(f"{verbo}: {len(rep.imported)} doc(s), {rep.links} vínculo(s)")
    if args.verbose:
        for x in rep.imported:
            print(f"  {x}")
    for x in rep.skipped:
        warn(f"[salteado] {x}")
    for x in rep.unresolved:
        warn(f"[link sin resolver, queda como texto] {x}")
    _warn_all(f"[aviso] {x}" for x in rep.warnings)
    if rep.ignored_files:
        warn(f"[nota] {rep.ignored_files} archivo(s) que no son docs (scripts/, validations/, …) "
             "no se importan")
    return 0


def cmd_export(args) -> int:
    with _open() as store:
        rep = transfer.export_tree(store, Path(args.destino), _scopes(args.scope), force=args.force)
    print(f"Exportados: {len(rep.written)} escrito(s), {rep.unchanged} sin cambios, en {args.destino}")
    for x in rep.stale:
        warn(f"[aviso] {x} no corresponde a ningún doc exportado (¿cambió de carpeta?): no se borró")
    return 0


# ──────────────────────────────────────────────────────────────── parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn", description="Base local-first para docs de trabajo y handoffs.")
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<comando>")

    p = sub.add_parser("init", help="inicializa el nodo: config, carpeta de datos y base vacía")
    p.add_argument("--node", help="nombre del nodo (default: el hostname)")
    p.add_argument("--data", help="carpeta de datos (default: ~/.local/share/cairn)")
    p.add_argument("--projects", help="raíz de los proyectos (default: ~/projects)")
    p.add_argument("--author", help=f"autor de los docs (default: {config.DEFAULT_AUTHOR})")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("new", help="crea un doc: diagnóstico (bug/infra), plan o referencia")
    p.add_argument("area")
    p.add_argument("tema")
    p.add_argument("tipo", nargs="?", default="infra", help="bug | infra | plan | ref (default: infra)")
    p.add_argument("--repo", help="a qué repo pertenece (default: el del PWD; `varios` si es cross-repo)")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("close", help="cierra un doc (estado resuelto/completado + `cerrado:`)")
    p.add_argument("doc")
    p.add_argument("-f", "--force", action="store_true", help="cerrar aunque queden `- [ ]` sin tildar")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("reopen", help="reabre un doc cerrado (o vuelve un handoff a pendiente)")
    p.add_argument("doc")
    p.add_argument("motivo", nargs="?", help="queda en `nota:`")
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("promote", help="copia secciones de un doc a una referencia y los vincula")
    p.add_argument("doc")
    p.add_argument("area")
    p.add_argument("tema")
    p.add_argument("--seccion", action="append", help="inicio del título de la sección (repetible); "
                   "default: Contexto + Comandos útiles | Diseño + Decisiones")
    p.add_argument("-f", "--force", action="store_true", help="promover aunque ya se haya promovido")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("list", help="lista los docs (default: los abiertos)")
    p.add_argument("scope", nargs="?", default="wip", choices=["wip", "done", "ref", "all"])
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("search", help="busca en todos los docs (regex, case-insensitive)")
    p.add_argument("patron")
    p.add_argument("scope", nargs="?", default="all", choices=["wip", "done", "ref", "all"])
    p.add_argument("--fts", action="store_true", help="búsqueda de texto completo (FTS5) por relevancia")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("weekly", help="digest de lo cerrado en los últimos días")
    p.add_argument("dias", nargs="?", type=int, default=7)
    p.add_argument("formato", nargs="?", default="resumen", choices=["resumen", "full"])
    p.set_defaults(func=cmd_weekly)

    p = sub.add_parser("check", help="¿está en orden? log, proyección y docs (sale 1 si hay problemas)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("edit", help="edita un doc en $EDITOR (o con --file/--stdin/--set/--unset)")
    p.add_argument("doc")
    p.add_argument("--file", help="el doc entero desde un archivo (sin front-matter: sólo el cuerpo)")
    p.add_argument("--stdin", action="store_true", help="el doc entero desde stdin")
    p.add_argument("--set", action="append", metavar="CLAVE=VALOR", help="campo del front-matter")
    p.add_argument("--unset", action="append", metavar="CLAVE", help="saca un campo del front-matter")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("show", help="muestra un doc como archivo; `doc@rev` muestra una revisión")
    p.add_argument("doc", help="nombre o id, con @<hash> o @~N para una revisión")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("log", help="historial de eventos de un doc (o de toda la base)")
    p.add_argument("doc", nargs="?")
    p.add_argument("-n", type=int, help="cantidad máxima")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("diff", help="diff entre revisiones (default: el último cambio)")
    p.add_argument("doc")
    p.add_argument("rev_a", nargs="?", help="desde (hash o ~N)")
    p.add_argument("rev_b", nargs="?", help="hasta (default: el head)")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("import", help="importa los docs de <raíz>/{_wip,_done,_ref} (una vez)")
    p.add_argument("raiz", help="la carpeta que tiene _wip/, _done/ y _ref/ (p. ej. ~/projects)")
    p.add_argument("--dry-run", action="store_true", help="mostrar qué se importaría sin escribir")
    p.add_argument("-v", "--verbose", action="store_true", help="listar cada doc")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export", help="escribe los docs como archivos en <destino>/{_wip,_done,_ref}")
    p.add_argument("destino")
    p.add_argument("scope", nargs="?", default="all", choices=["wip", "done", "ref", "all"])
    p.add_argument("-f", "--force", action="store_true", help="pisar archivos con otro contenido")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("rebuild", help="rehace la proyección SQLite desde el log de eventos")
    p.set_defaults(func=cmd_rebuild)

    h = sub.add_parser("handoff", help="notas para la próxima conversación")
    hs = h.add_subparsers(dest="handoff_cmd", metavar="<new|take|drop|list|hook>", required=True)
    p = hs.add_parser("new", help="deja una nota pendiente")
    p.add_argument("area")
    p.add_argument("tema")
    p.add_argument("--para", action="append", help="a qué repo le toca (repetible; default: `repo:`)")
    p.add_argument("--plan", action="append", help="el plan al que apunta (repetible)")
    p.add_argument("--repo", help="a qué repo pertenece (default: el del PWD)")
    p.set_defaults(func=cmd_handoff_new)
    p = hs.add_parser("take", help="la imprime y la archiva (estado usada)")
    p.add_argument("nota", nargs="?", help="sin nombre: la única pendiente para el repo del PWD")
    p.set_defaults(func=cmd_handoff_take)
    p = hs.add_parser("drop", help="la archiva sin usarla (estado descartada)")
    p.add_argument("nota")
    p.add_argument("motivo", nargs="?")
    p.set_defaults(func=cmd_handoff_drop)
    p = hs.add_parser("list", help="pendientes para el repo del PWD")
    p.add_argument("--all", action="store_true", help="todas")
    p.set_defaults(func=cmd_handoff_list)
    p = hs.add_parser("hook", help="para el hook SessionStart de Claude Code: avisa si hay pendientes")
    p.set_defaults(func=cmd_handoff_hook)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        code = args.func(args) or 0
        sys.stdout.flush()         # que un pipe cerrado salte acá y no al salir
        return code
    except (CairnError, config.ConfigError) as e:
        warn(str(e))
        return 1
    except BrokenPipeError:        # `cairn search x | head`: salir callado, sin el aviso de flush
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
