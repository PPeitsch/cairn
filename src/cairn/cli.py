"""Entry point of the `cairn` command."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from cairn import (
    __version__,
    access,
    clock,
    config,
    docs,
    edit,
    history,
    integrate,
    keys,
    reading,
    sync,
    transfer,
    tree,
)
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
        fingerprint = keys.fingerprint(store.public_line())
    print(f"  config:    {cfg.path}")
    print(f"  datos:     {cfg.data_dir}")
    print(f"  proyectos: {cfg.projects}")
    print(f"  eventos:   {n}")
    print(f"  huella:    {fingerprint}")
    print(f"  {FINGERPRINT_NOTE}")
    return 0


def cmd_rebuild(args) -> int:
    with _open() as store:
        n = store.rebuild()
        ndocs = len(store.projection.doc_ids())
        _warn_all(f"[aviso] {p}" for p in store.problems)
    print(f"Proyección rehecha desde el log: {n} evento(s), {ndocs} doc(s)")
    return 1 if store.problems else 0


# ──────────────────────────────────────────────────────────────── lifecycle

def cmd_new(args) -> int:
    with _open() as store:
        repo = args.repo if args.repo is not None else _cwd_repo(store)
        r = docs.new(store, args.area, args.tema, args.tipo, repo)
    print(r.doc.name)
    return 0


def cmd_close(args) -> int:
    with _open() as store:
        r = docs.close(store, args.doc, force=args.force, descartar=args.descartar)
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
    print(f"  llevadas: {', '.join(r.extra['carried'])}")
    _warn_all(r.warnings)
    return 0


# ──────────────────────────────────────────────────────────────── queries

def _print_scope(store: Store, scope: str, by_repo: bool = False) -> None:
    """In `wip`, handoffs are listed apart: they are not open work but notes waiting for the next
    conversation."""
    found = docs.all_docs(store, (scope,))
    handoffs = [d for d in found if scope == "wip" and d.tipo == "handoff"]
    work = [d for d in found if d not in handoffs]
    if by_repo:
        if work:
            print("\n".join(docs.grouped_by_repo(work)))
    else:
        for d in work:
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
                _print_scope(store, s, args.por_repo)
        else:
            _print_scope(store, args.scope, args.por_repo)
    return 0


def _summary(doc: docs.Doc) -> str:
    r = first_sentences(doc.tldr, 1)
    return r[:197] + "..." if len(r) > 200 else r


def cmd_search(args) -> int:
    with _open() as store:
        if args.fts:
            found, total = docs.search_fts(store, args.patron, _scopes(args.scope))
            if not found:
                print(f"Búsqueda FTS \"{args.patron}\" — sin coincidencias")
                return 0
            print(docs.fts_header(args.patron, found, total) + "\n")
            for h in found:
                d = h.doc
                print(f"  {docs.ICON[d.tipo]}  {d.title}   ·   {docs.estado_label(d)}   ·   "
                      f"{docs.repo_label(d)}")
                print(f"      {d.path}")
                print(f"      {h.snippet}")
                print()
            return 0
        hits, total = docs.search(store, args.patron, _scopes(args.scope))
        files = tree.search_files(store, args.patron, _scopes(args.scope))
    if not hits and not files:
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
        if r := _summary(d):
            print(f"      {r}")
        for n, line in h.lines[:3]:
            print(f"      │ {n}: {line.strip()[:140]}")
        print()
    if files:
        print(f"━━━━━  archivos de scripts/ y validations/  ({len(files)})  ━━━━━\n")
        for f in files:
            print(f"  {f.path}   ·   {len(f.lines)} coincidencia(s)")
            for n, line in f.lines[:3]:
                print(f"      │ {n}: {line.strip()[:140]}")
            print()
    return 0


def cmd_weekly(args) -> int:
    with _open() as store:
        since, closed = docs.weekly(store, args.dias)
    today = clock.today()
    total = len(closed)
    if args.formato == "full":
        print(f"## Weekly — docs cerrados entre {since} y {today} (últimos {args.dias} días)\n")
        print(f"_{total} doc(s) cerrado(s)_\n")
        for d in closed:
            kind = {"plan": "plan/spec", "bug": "bug"}.get(d.tipo, "diagnóstico")
            print(f"### {d.title}\n_{kind} · {d.meta.get('estado', '—')} · cerrado {d.date_closed()} · "
                  f"{docs.repo_label(d)}_\n")
            print(f"{d.tldr or '_(sin TL;DR)_'}\n\n---\n")
        return 0
    counts = {k: sum(1 for d in closed if d.tipo == k) for k in ("bug", "infra", "plan")}
    parts = [f"{docs.ICON[k]} {n} {lbl}" for k, lbl in (("bug", "bugs"), ("infra", "diagnósticos"),
                                                        ("plan", "planes")) if (n := counts[k])]
    discarded = sum(1 for d in closed if d.estado == "descartado")
    print(f"Semana {fmt_date(since)} – {fmt_date(today)} {today[:4]}  ·  {total} trabajos cerrados"
          + (f" ({discarded} descartado(s))" if discarded else ""))
    if parts:
        print("   " + " · ".join(parts))
    current = None
    for d in sorted(closed, key=docs.repo_label):
        repo = docs.repo_label(d)
        if repo != current:
            n = sum(1 for x in closed if docs.repo_label(x) == repo)
            print(f"\n━━━━━  {repo}  ({n})  ━━━━━\n")
            current = repo
        mark = "   ·   descartado" if d.estado == "descartado" else ""
        print(f"  {docs.ICON[d.tipo]}  {d.title}   ·   {fmt_date(d.date_closed())}{mark}")
        print(f"      {first_sentences(d.tldr, 2) or '(sin TL;DR)'}\n")
    return 0


def cmd_check(args) -> int:
    with _open() as store:
        problems = docs.check(store) + tree.problems(store)
        print(f"cairn check   (nodo: {store.config.node}, datos: {store.config.data_dir})")
    for p in problems:
        print(f"  [aviso] {p}")
    print(f"\n  {len(problems)} problema(s)" if problems else "  [OK] todo en orden")
    return 1 if problems else 0


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
        pend = docs.pending_handoffs(store, repo)
    where = repo or "toda la raíz de proyectos"
    if not pend:
        print(f"Sin handoffs pendientes para {where}.")
        return 0
    print(f"Handoffs pendientes para {where}:")
    for d in pend:
        print(f"  {docs.handoff_line(d, store.config.handoff_stale_days)}")
    return 0


def cmd_handoff_hook(args) -> int:
    """SessionStart: if there are pending handoffs for the conversation's repo, it announces them
    as context. It never blocks startup: on any error it exits silently."""
    try:
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            data = {}
        # on resume or after compaction, the conversation already saw it (or already took it)
        if data.get("source") in ("resume", "compact"):
            return 0
        with _open() as store:
            if sync.auto_enabled(store.config):
                # pull what another machine left before looking (silently: if it fails, stay local)
                sync.sync_all(store, timeout=store.config.sync_hook_timeout)
            repo = repo_of_path(Path(data.get("cwd") or os.getcwd()), store.config.projects)
            text = docs.handoff_hook_context(store, repo)
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                     "additionalContext": text}}, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — a broken hook must not block the conversation
        pass
    return 0


# ──────────────────────────────────────────────────────────────── edit / history

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
    if args.seccion or args.lineas:
        print(reading.view(text, args.seccion, args.lineas))
    else:
        sys.stdout.write(text)
    return 0


def cmd_log(args) -> int:
    with _open() as store:
        lines = history.log(store, args.doc, args.n)
    for ln in lines:
        mark = "" if ln.status == "applied" else f"  [{ln.status}]"
        print(f"{ln.event_id[:10]}  {ln.ts[:19].replace('T', ' ')}  {ln.node:<10} {ln.op:<13} "
              f"{ln.doc_name}{mark}")
    return 0


def cmd_diff(args) -> int:
    with _open() as store:
        out = history.diff(store, args.doc, args.rev_a, args.rev_b)
    sys.stdout.write(out or "Sin diferencias.\n")
    return 0


def cmd_resolve(args) -> int:
    with _open() as store:
        if args.keep:
            doc, n = history.resolve(store, args.doc, args.keep)
            print(f"Resuelto: {doc.name} ({n} evento(s) de ramas divergentes; "
                  f"{'queda la principal' if args.keep == 'main' else f'queda la rama {args.keep}'})")
            return 0
        doc = docs.find(store, args.doc)
        if not doc.divergent:
            print(f"{doc.name} no tiene ramas divergentes.")
            return 0
        branches = history.branches(store, doc)
        print(f"{doc.name}: la principal (head {doc.head[:10]}) y {len(branches)} rama(s) divergente(s).\n")
        for b in branches:
            print(f"rama {b.tip.id[:10]}  {b.tip.ts[:19].replace('T', ' ')}  {b.tip.node}  "
                  f"({len(b.events)} evento(s): {', '.join(e.op for e in b.events)})")
            sys.stdout.write(history.branch_diff(store, doc, b) or "  (mismo contenido que la principal)\n")
            print()
    print(f"Elegir: cairn resolve {doc.name} --keep main   |   --keep <hash de la rama>")
    return 0


# ──────────────────────────────────────────────────────────────── import / export

def cmd_import(args) -> int:
    with _open() as store:
        rep = transfer.import_tree(store, Path(args.raiz), dry_run=args.dry_run)
    verb = "Se importarían" if args.dry_run else "Importados"
    print(f"{verb}: {len(rep.imported)} doc(s), {rep.links} vínculo(s)")
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


# ──────────────────────────────────────────────────────────────── mcp

def cmd_defaults(args) -> int:
    from cairn import mcp_server, templates
    dest = Path(args.carpeta).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    files = {**{f"templates/{k}": v.lstrip("\n") for k, v in templates.defaults().items()},
             "mcp_instructions.md": mcp_server.INSTRUCTIONS}
    for rel, text in files.items():
        p = dest / rel
        if p.exists() and not args.force:
            warn(f"ya existe, no se pisa: {p} (--force)")
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(p)
    print(f"\nPara usarlas, en la config:\n  [docs]\n  templates = \"{dest / 'templates'}\"\n"
          f"  [mcp]\n  instructions = \"{dest / 'mcp_instructions.md'}\"")
    return 0


def cmd_mcp(args) -> int:
    config.load()       # without `init`, say so here and not on the first tool call
    from cairn import mcp_server  # only this command loads `mcp`
    mcp_server.run()
    return 0


def cmd_integrate(args) -> int:
    command = shlex.split(args.command) if args.command else None
    if command == []:
        raise CairnError("--command vacío")
    if args.target == "systemd":
        rep = integrate.systemd(command, config.load().sync_interval, remove=args.remove,
                                dry_run=args.dry_run)
    else:
        rep = integrate.claude_code(command, remove=args.remove, dry_run=args.dry_run,
                                    mcp=not args.no_mcp, hooks=not args.no_hooks,
                                    replace_wip_doc=args.replace_wip_doc)
    prefix = "[dry-run] " if args.dry_run else ""
    for a in rep.actions:
        print(f"{prefix}{a}")
    for u in rep.unchanged:
        print(f"ya estaba: {u}")
    if not rep.actions:
        print("Nada que hacer." if args.remove else "Integración al día.")
    return 0


# ──────────────────────────────────────────────────────────────── sync and access

FINGERPRINT_NOTE = ("anotá la huella fuera de este nodo (p. ej. en el gestor de contraseñas): si "
                    "pierde sus datos, `sync --restore --old-key <huella>` la usa para no aceptar "
                    "a ciegas las claves viejas que mande el par")


def cmd_key(args) -> int:
    with _open() as store:
        if args.rotate:
            for line in access.rotate_key(store):
                print(line)
            print(f"huella nueva: {keys.fingerprint(store.public_line())}")
            print(FINGERPRINT_NOTE)
            return 0
        line = store.public_line()
    if args.fingerprint:
        print(keys.fingerprint(line))
    else:
        print(line)                       # alone on stdout: `authorize <node> "$(cairn key)"`
        warn(f"huella: {keys.fingerprint(line)} ({FINGERPRINT_NOTE})")
    return 0


def cmd_authorize(args) -> int:
    pub = args.clave
    if Path(pub).expanduser().is_file():
        pub = Path(pub).expanduser().read_text(encoding="utf-8").strip()
    command = shlex.split(args.command) if args.command else None
    for line in access.authorize(args.nodo, pub, ssh=not args.no_ssh, command=command,
                                 replace=args.replace):
        print(line)
    return 0


def cmd_revoke(args) -> int:
    for line in access.revoke(args.nodo):
        print(line)
    return 0


def cmd_peers(args) -> int:
    cfg = config.load()
    with_ssh = set(access.ssh_authorized_nodes())
    if not cfg.peers:
        print("Sin nodos autorizados.")
    for p in cfg.peers.values():
        access_ = f"revocado {p.revoked}" if p.revoked else ("ssh" if p.node in with_ssh else "sólo eventos")
        print(f"{p.node:<16} {access_:<18} {keys.fingerprint(p.key)}")
        for k in p.old:
            print(f"{'':<16} {'anterior':<18} {keys.fingerprint(k.key)}  hasta {k.until[:19]}")
    return 0


def cmd_remote(args) -> int:
    if args.remote_cmd == "add":
        access.remote_add(args.nombre, args.url, key=args.key, command=args.command)
        print(f"Remoto {args.nombre}: {args.url}")
    elif args.remote_cmd == "remove":
        access.remote_remove(args.nombre)
        print(f"Remoto {args.nombre} sacado")
    else:
        cfg = config.load()
        if not cfg.remotes:
            print("Sin remotos.")
        for r in cfg.remotes.values():
            print(f"{r.name:<12} {r.url}" + (f"  (clave {r.key})" if r.key else ""))
    return 0


def cmd_sync(args) -> int:
    if args.background:
        sync.trigger(config.load(), delay=0)
        return 0
    if args.after is not None and not sync.wait_turn(config.load(), args.after):
        return 0        # another one is already waiting: it also syncs this write
    with sync.running(config.load()), _open() as store:
        if not store.config.remotes:
            if args.quiet:
                return 0
            raise CairnError("Sin remotos: `cairn remote add <nombre> ssh://usuario@host`")
        timeout = args.timeout or store.config.sync_timeout
        if (args.old_key or args.trust_peer_keys) and not args.restore:
            raise CairnError("--old-key y --trust-peer-keys son para `sync --restore`")
        if args.restore:
            try:
                old_keys = [keys.as_fingerprint(k) for k in args.old_key]
            except keys.KeyFileError as e:
                raise CairnError(f"--old-key: {e}") from None
            if len(args.remotos) != 1:
                raise CairnError("--restore trae el log de este nodo desde un solo remoto: "
                                 "`cairn sync --restore <remoto>`")
            if store.log.segments(store.config.node):
                raise CairnError(f"{store.config.node} ya tiene eventos propios: el restore es para "
                                 "un nodo que perdió sus datos y se reinicializó")
            remote = store.config.remotes.get(args.remotos[0])
            if remote is None:
                raise CairnError(f"No hay remoto: {args.remotos[0]}")
            results = [sync.sync_remote(store, remote, timeout=timeout, restore=True,
                                        old_keys=old_keys, trust_peer_keys=args.trust_peer_keys)]
        else:
            results = sync.sync_all(store, args.remotos, timeout=timeout)
    when = f"{clock.now().astimezone().isoformat(timespec='seconds')} " if args.quiet else ""
    for r in results:
        if not args.quiet or not r.ok:
            print(f"{when}{r.remote} ({r.peer or '?'}): {r.sent} enviado(s), {r.received} recibido(s)"
                  + ("" if r.ok else f", {len(r.errors)} error(es)"))
        sys.stdout.flush()
        for e in r.errors:
            warn(f"  [error] {e}")
    return 0 if all(r.ok for r in results) else 1


def cmd_sync_peer(args) -> int:
    if not args.stdio:
        raise CairnError("sync-peer lo lanza `cairn sync` del otro lado por ssh (--stdio)")
    return sync.serve_stdio(args.as_node)


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
    p.add_argument("--author", help="autor de los docs (default: `git config user.name`, o el usuario)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("new", help="crea un doc: diagnóstico (bug/infra), plan o referencia")
    p.add_argument("area")
    p.add_argument("tema")
    p.add_argument("tipo", nargs="?", default="infra", help="bug | infra | plan | ref (default: infra)")
    p.add_argument("--repo", help="a qué repo pertenece (default: el del PWD; `varios` si es cross-repo)")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("close", help="cierra un doc (estado resuelto/completado/descartado + `cerrado:`)")
    p.add_argument("doc")
    p.add_argument("-f", "--force", action="store_true", help="cerrar aunque queden `- [ ]` sin tildar")
    p.add_argument("--descartar", metavar="MOTIVO", help="cerrarlo sin terminar: estado `descartado`, "
                   "el motivo en `nota:` (no pide --force)")
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
    p.add_argument("--por-repo", action="store_true",
                   help="agrupado por repo, con cuántos hay y cuándo se modificó cada uno")
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
    p.add_argument("--seccion", help="sólo esa sección (el comienzo de su título), numerada")
    p.add_argument("--lineas", help="sólo esas líneas (`40-80`, `40-`, `40`), numeradas")
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

    p = sub.add_parser("resolve", help="muestra las ramas divergentes de un doc y resuelve cuál queda")
    p.add_argument("doc")
    p.add_argument("--keep", metavar="main|HASH", help="`main` (la principal) o el hash de una rama")
    p.set_defaults(func=cmd_resolve)

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

    p = sub.add_parser("defaults", help="exporta las plantillas y las instrucciones del MCP de fábrica "
                       "a una carpeta, para personalizarlas")
    p.add_argument("carpeta")
    p.add_argument("-f", "--force", action="store_true", help="pisar archivos que ya existan")
    p.set_defaults(func=cmd_defaults)

    p = sub.add_parser("mcp", help="servidor MCP por stdio (lo lanza el agente, no se corre a mano)")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("integrate", help="registra el MCP y los hooks de cairn en un agente "
                       "(claude-code) o el timer de sync periódico (systemd)")
    p.add_argument("target", choices=["claude-code", "systemd"])
    p.add_argument("--remove", action="store_true", help="deshace la integración")
    p.add_argument("--dry-run", action="store_true", help="muestra qué haría, sin tocar nada")
    p.add_argument("--command", help="cómo invocar cairn (default: la ruta del comando; "
                   "p. ej. \"ssh vps cairn\" para usar la base de otro nodo)")
    p.add_argument("--no-mcp", action="store_true", help="no tocar el MCP")
    p.add_argument("--no-hooks", action="store_true", help="no tocar los hooks")
    p.add_argument("--replace-wip-doc", action="store_true",
                   help="sacar también el hook de handoffs de wip-doc (al migrar)")
    p.set_defaults(func=cmd_integrate)

    p = sub.add_parser("key", help="la clave pública de este nodo (para `authorize` en el otro)")
    p.add_argument("--fingerprint", action="store_true",
                   help="sólo la huella (`SHA256:…`), para anotarla fuera del nodo")
    p.add_argument("--rotate", action="store_true",
                   help="cambiarla (la anterior sigue verificando lo ya firmado; los pares la "
                        "aceptan con `authorize --replace`)")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("authorize", help="acepta los eventos de un nodo y le da acceso de sync por ssh")
    p.add_argument("nodo")
    p.add_argument("clave", help="su clave pública (`cairn key` en ese nodo), o un archivo con ella")
    p.add_argument("--no-ssh", action="store_true",
                   help="sólo confiar en sus eventos (no se conecta a este nodo)")
    p.add_argument("--command", help="cómo se invoca cairn acá, para authorized_keys "
                   "(default: la ruta del comando)")
    p.add_argument("--replace", action="store_true",
                   help="el nodo cambió de clave: la anterior queda para verificar lo que ya firmó")
    p.set_defaults(func=cmd_authorize)

    p = sub.add_parser("revoke", help="le saca el acceso a un nodo y rechaza sus eventos nuevos")
    p.add_argument("nodo")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("peers", help="los nodos autorizados")
    p.set_defaults(func=cmd_peers)

    r = sub.add_parser("remote", help="los nodos con los que sincroniza este")
    rs = r.add_subparsers(dest="remote_cmd", metavar="<add|remove|list>")
    p = rs.add_parser("add")
    p.add_argument("nombre")
    p.add_argument("url", help="ssh://usuario@host[:puerto]")
    p.add_argument("--key", help="clave ssh (default: la del nodo)")
    p.add_argument("--command", help="cómo se invoca cairn del otro lado (default: cairn)")
    p = rs.add_parser("remove")
    p.add_argument("nombre")
    rs.add_parser("list")
    r.set_defaults(func=cmd_remote, remote_cmd="list")

    p = sub.add_parser("sync", help="intercambia eventos con los remotos")
    p.add_argument("remotos", nargs="*", help="default: todos")
    p.add_argument("--quiet", action="store_true", help="sólo errores")
    p.add_argument("--timeout", type=float, help="segundos por remoto (default: `[sync] timeout`, 60)")
    p.add_argument("--background", action="store_true",
                   help="lanzarlo en segundo plano y volver (salida en <datos>/sync.log)")
    p.add_argument("--after", type=float, metavar="SEG", help=argparse.SUPPRESS)  # the debounce
    p.add_argument("--restore", action="store_true",
                   help="traer también el log de este nodo (perdió sus datos y se reinicializó)")
    p.add_argument("--old-key", action="append", default=[], metavar="HUELLA",
                   help="con --restore: huella (o pública) de una clave anterior de este nodo, "
                        "anotada fuera de él; una por clave. Las que mande el par sin coincidir "
                        "cortan el restore")
    p.add_argument("--trust-peer-keys", action="store_true",
                   help="con --restore: aceptar las claves anteriores que mande el par sin "
                        "controlarlas (si no se anotaron las huellas)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("sync-peer", help="el otro lado de `sync` (lo lanza ssh, no se corre a mano)")
    p.add_argument("--stdio", action="store_true")
    p.add_argument("--as", dest="as_node", help="identidad fijada por authorized_keys")
    p.set_defaults(func=cmd_sync_peer)

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
        sys.stdout.flush()         # so a closed pipe surfaces here and not at exit
        return code
    except (CairnError, config.ConfigError) as e:
        warn(str(e))
        return 1
    except BrokenPipeError:        # `cairn search x | head`: exit quietly, without the flush warning
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
