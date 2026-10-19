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
        print(f"cairn is already initialized on this node: {cfg.node}")
    else:
        node = args.node or config.default_node_name()
        config.write(node, data_dir=args.data, projects=args.projects, author=args.author)
        cfg = config.load()
        print(f"Node initialized: {cfg.node}")
    with Store(cfg) as store:
        n = store.projection.count_events()
        fingerprint = keys.fingerprint(store.public_line())
    print(f"  config:      {cfg.path}")
    print(f"  data:        {cfg.data_dir}")
    print(f"  projects:    {cfg.projects}")
    print(f"  events:      {n}")
    print(f"  fingerprint: {fingerprint}")
    print(f"  {FINGERPRINT_NOTE}")
    return 0


def cmd_rebuild(args) -> int:
    with _open() as store:
        n = store.rebuild()
        ndocs = len(store.projection.doc_ids())
        _warn_all(f"[warning] {p}" for p in store.problems)
    print(f"Projection rebuilt from the log: {n} event(s), {ndocs} doc(s)")
    return 1 if store.problems else 0


# ──────────────────────────────────────────────────────────────── lifecycle

def cmd_new(args) -> int:
    with _open() as store:
        repo = args.repo if args.repo is not None else _cwd_repo(store)
        r = docs.new(store, args.area, args.topic, args.type, repo)
    print(r.doc.name)
    return 0


def cmd_close(args) -> int:
    with _open() as store:
        r = docs.close(store, args.doc, force=args.force, discard=args.discard)
    print(f"Closed: {r.doc.name} ({r.doc.status})")
    _warn_all(r.warnings)
    return 0


def cmd_reopen(args) -> int:
    with _open() as store:
        r = docs.reopen(store, args.doc, args.reason)
    print(f"Reopened: {r.doc.name}")
    _warn_all(r.warnings)
    return 0


def cmd_promote(args) -> int:
    with _open() as store:
        r = docs.promote(store, args.doc, args.area, args.topic, args.section, force=args.force)
    print(r.doc.name)
    print(f"  carried over: {', '.join(r.extra['carried'])}")
    _warn_all(r.warnings)
    return 0


def cmd_link(args) -> int:
    with _open() as store:
        r = docs.link(store, args.doc, args.kind, args.other, remove=args.remove)
    if r.extra["changed"]:
        print(f"{'Unlinked' if args.remove else 'Linked'}: {r.doc.name} {args.kind} → {r.extra['other']}")
    _warn_all(r.warnings)
    return 0


# ──────────────────────────────────────────────────────────────── queries

def _print_scope(store: Store, scope: str, by_repo: bool = False) -> None:
    """In `wip`, handoffs are listed apart: they are not open work but notes waiting for the next
    conversation."""
    found = docs.all_docs(store, (scope,))
    handoffs = [d for d in found if scope == "wip" and d.type == "handoff"]
    work = [d for d in found if d not in handoffs]
    if by_repo:
        if work:
            print("\n".join(docs.grouped_by_repo(work)))
    else:
        for d in work:
            print(d.name)
    if handoffs:
        print("\npending handoffs:")
        for d in handoffs:
            print(f"  {d.name}")


def cmd_list(args) -> int:
    with _open() as store:
        if args.scope == "all":
            for s in docs.SCOPES:
                print(f"=== {s} ===")
                _print_scope(store, s, args.by_repo)
        else:
            _print_scope(store, args.scope, args.by_repo)
    return 0


def _summary(doc: docs.Doc) -> str:
    r = first_sentences(doc.tldr, 1)
    return r[:197] + "..." if len(r) > 200 else r


def cmd_search(args) -> int:
    with _open() as store:
        if args.fts:
            found, total = docs.search_fts(store, args.pattern, _scopes(args.scope))
            if not found:
                print(f"FTS search \"{args.pattern}\": no matches")
                return 0
            print(docs.fts_header(args.pattern, found, total) + "\n")
            for h in found:
                d = h.doc
                print(f"  {docs.ICON[d.type]}  {d.title}   ·   {docs.status_label(d)}   ·   "
                      f"{docs.repo_label(d)}")
                print(f"      {d.path}")
                print(f"      {h.snippet}")
                print()
            return 0
        hits, total = docs.search(store, args.pattern, _scopes(args.scope))
        files = tree.search_files(store, args.pattern, _scopes(args.scope))
    if not hits and not files:
        print(f"Search \"{args.pattern}\": no matches in {total} docs")
        return 0
    print(f"Search \"{args.pattern}\": {len(hits)} doc(s) of {total}")
    current = None
    for h in hits:
        repo = docs.repo_label(h.doc)
        if repo != current:
            n = sum(1 for x in hits if docs.repo_label(x.doc) == repo)
            print(f"\n━━━━━  {repo}  ({n})  ━━━━━\n")
            current = repo
        d = h.doc
        print(f"  {docs.ICON[d.type]}  {d.title}   ·   {docs.status_label(d)}   ·   "
              f"{len(h.lines)} match(es)")
        print(f"      {d.path}")
        if r := _summary(d):
            print(f"      {r}")
        for n, line in h.lines[:3]:
            print(f"      │ {n}: {line.strip()[:140]}")
        print()
    if files:
        print(f"━━━━━  files in {tree.label(store)}  ({len(files)})  ━━━━━\n")
        for f in files:
            print(f"  {f.path}   ·   {len(f.lines)} match(es)")
            for n, line in f.lines[:3]:
                print(f"      │ {n}: {line.strip()[:140]}")
            print()
    return 0


def cmd_weekly(args) -> int:
    with _open() as store:
        since, closed = docs.weekly(store, args.days)
    today = clock.today()
    total = len(closed)
    if args.format == "full":
        print(f"## Weekly — docs closed between {since} and {today} (last {args.days} days)\n")
        print(f"_{total} doc(s) closed_\n")
        for d in closed:
            kind = {"plan": "plan/spec", "bug": "bug"}.get(d.type, "diagnosis")
            print(f"### {d.title}\n_{kind} · {d.meta.get('status', '—')} · closed {d.date_closed()} · "
                  f"{docs.repo_label(d)}_\n")
            print(f"{d.tldr or '_(no TL;DR)_'}\n\n---\n")
        return 0
    counts = {k: sum(1 for d in closed if d.type == k) for k in ("bug", "infra", "plan")}
    parts = [f"{docs.ICON[k]} {n} {lbl}" for k, lbl in (("bug", "bugs"), ("infra", "diagnoses"),
                                                        ("plan", "plans")) if (n := counts[k])]
    discarded = sum(1 for d in closed if d.status == "discarded")
    print(f"Week {fmt_date(since)} – {fmt_date(today)} {today[:4]}  ·  {total} pieces of work closed"
          + (f" ({discarded} discarded)" if discarded else ""))
    if parts:
        print("   " + " · ".join(parts))
    current = None
    for d in sorted(closed, key=docs.repo_label):
        repo = docs.repo_label(d)
        if repo != current:
            n = sum(1 for x in closed if docs.repo_label(x) == repo)
            print(f"\n━━━━━  {repo}  ({n})  ━━━━━\n")
            current = repo
        mark = "   ·   discarded" if d.status == "discarded" else ""
        print(f"  {docs.ICON[d.type]}  {d.title}   ·   {fmt_date(d.date_closed())}{mark}")
        print(f"      {first_sentences(d.tldr, 2) or '(no TL;DR)'}\n")
    return 0


def cmd_check(args) -> int:
    with _open() as store:
        problems = docs.check(store) + tree.problems(store)
        print(f"cairn check   (node: {store.config.node}, data: {store.config.data_dir})")
    for p in problems:
        print(f"  [warning] {p}")
    print(f"\n  {len(problems)} problem(s)" if problems else "  [OK] all in order")
    return 1 if problems else 0


# ──────────────────────────────────────────────────────────────── handoffs

def cmd_handoff_new(args) -> int:
    with _open() as store:
        repo = args.repo if args.repo is not None else _cwd_repo(store)
        r = docs.handoff_new(store, args.area, args.topic, args.to, args.plan, repo)
    print(r.doc.name)
    _warn_all(r.warnings)
    return 0


def cmd_handoff_take(args) -> int:
    with _open() as store:
        r = docs.handoff_take(store, args.note, _cwd_repo(store))
    print(f"Handoff taken: {r.doc.name} (archived, status: taken)")
    for a in r.warnings:
        print(f"[warning] {a}")
    print("\n" + "=" * 72 + r.doc.body.rstrip("\n") + "\n" + "=" * 72)
    return 0


def cmd_handoff_drop(args) -> int:
    with _open() as store:
        r = docs.handoff_drop(store, args.note, args.reason)
    print(f"Handoff discarded: {r.doc.name} (archived)")
    return 0


def cmd_handoff_list(args) -> int:
    with _open() as store:
        repo = "" if args.all else _cwd_repo(store)
        pend = docs.pending_handoffs(store, repo)
    where = repo or "the whole projects root"
    if not pend:
        print(f"No pending handoffs for {where}.")
        return 0
    print(f"Pending handoffs for {where}:")
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
    print(f"Saved: {r.doc.name}" if r.extra.get("changed") else f"No changes: {r.doc.name}")
    _warn_all(r.warnings)
    return 0


def cmd_show(args) -> int:
    with _open() as store:
        _, text = history.show(store, args.doc)
    if args.section or args.lines:
        print(reading.view(text, args.section, args.lines))
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
    sys.stdout.write(out or "No differences.\n")
    return 0


def cmd_resolve(args) -> int:
    with _open() as store:
        if args.keep:
            doc, n = history.resolve(store, args.doc, args.keep)
            print(f"Resolved: {doc.name} ({n} event(s) on divergent branches; "
                  f"{'the main one stays' if args.keep == 'main' else f'branch {args.keep} stays'})")
            return 0
        doc = docs.find(store, args.doc)
        if not doc.divergent:
            print(f"{doc.name} has no divergent branches.")
            return 0
        branches = history.branches(store, doc)
        print(f"{doc.name}: the main one (head {doc.head[:10]}) and {len(branches)} divergent branch(es).\n")
        for b in branches:
            print(f"branch {b.tip.id[:10]}  {b.tip.ts[:19].replace('T', ' ')}  {b.tip.node}  "
                  f"({len(b.events)} event(s): {', '.join(e.op for e in b.events)})")
            sys.stdout.write(history.branch_diff(store, doc, b) or "  (same content as the main one)\n")
            print()
    print(f"Pick one: cairn resolve {doc.name} --keep main   |   --keep <branch hash>")
    return 0


# ──────────────────────────────────────────────────────────────── migrate wip-doc

def cmd_wip_doc_import(args) -> int:
    from cairn.migrate import wip_doc
    with _open() as store:
        rep = wip_doc.import_tree(store, Path(args.root), dry_run=args.dry_run)
    verb = "Would import" if args.dry_run else "Imported"
    print(f"{verb}: {len(rep.imported)} doc(s), {rep.links} link(s)")
    if args.verbose:
        for x in rep.imported:
            print(f"  {x}")
    for x in rep.skipped:
        warn(f"[skipped] {x}")
    for x in rep.unresolved:
        warn(f"[unresolved link, kept as text] {x}")
    _warn_all(f"[warning] {x}" for x in rep.warnings)
    if rep.ignored_files:
        warn(f"[note] {rep.ignored_files} file(s) that are not docs (scripts/, validations/, …) "
             "are not imported")
    return 0


def cmd_wip_doc_export(args) -> int:
    from cairn.migrate import wip_doc
    with _open() as store:
        rep = wip_doc.export_tree(store, Path(args.dest), _scopes(args.scope), force=args.force)
    print(f"Exported: {len(rep.written)} written, {rep.unchanged} unchanged, in {args.dest}")
    for x in rep.stale:
        warn(f"[warning] {x} matches no exported doc (did it change folder?): not deleted")
    return 0


def cmd_wip_doc_check(args) -> int:
    from cairn.migrate import wip_doc
    with _open() as store:
        problems = wip_doc.leftovers(store, Path(args.root) if args.root else None)
    for p in problems:
        print(f"  [warning] {p}")
    print(f"\n  {len(problems)} wip-doc leftover(s)" if problems else "  [OK] no wip-doc leftovers")
    return 1 if problems else 0


def cmd_wip_doc_unhook(args) -> int:
    from cairn.migrate import wip_doc
    rep = wip_doc.unhook(dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    for a in rep.actions:
        print(f"{prefix}{a}")
    if not rep.actions:
        print("wip-doc's hook is not there.")
    return 0


# ──────────────────────────────────────────────────────────────── mcp

def cmd_defaults(args) -> int:
    from cairn import mcp_server, templates
    dest = Path(args.folder).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    files = {**{f"templates/{k}": v.lstrip("\n") for k, v in templates.defaults().items()},
             "mcp_instructions.md": mcp_server.INSTRUCTIONS}
    for rel, text in files.items():
        p = dest / rel
        if p.exists() and not args.force:
            warn(f"already exists, not overwritten: {p} (--force)")
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(p)
    print(f"\nTo use them, in the config:\n  [docs]\n  templates = \"{dest / 'templates'}\"\n"
          f"  [mcp]\n  instructions = \"{dest / 'mcp_instructions.md'}\"")
    return 0


def cmd_serve(args) -> int:
    config.load()       # without `init`, say so here and not on the first request
    from cairn import web
    web.serve(args.port, args.open)
    return 0


def cmd_mcp(args) -> int:
    config.load()       # without `init`, say so here and not on the first tool call
    from cairn import mcp_server  # only this command loads `mcp`
    mcp_server.run()
    return 0


def cmd_integrate(args) -> int:
    command = shlex.split(args.command) if args.command else None
    if command == []:
        raise CairnError("--command is empty")
    if args.target == "systemd":
        rep = integrate.systemd(command, config.load().sync_interval, remove=args.remove,
                                dry_run=args.dry_run)
    else:
        rep = integrate.claude_code(command, remove=args.remove, dry_run=args.dry_run,
                                    mcp=not args.no_mcp, hooks=not args.no_hooks)
    prefix = "[dry-run] " if args.dry_run else ""
    for a in rep.actions:
        print(f"{prefix}{a}")
    for u in rep.unchanged:
        print(f"already there: {u}")
    if not rep.actions:
        print("Nothing to do." if args.remove else "Integration up to date.")
    return 0


# ──────────────────────────────────────────────────────────────── sync and access

FINGERPRINT_NOTE = ("write the fingerprint down outside this node (e.g. in the password manager): "
                    "if the node loses its data, `sync --restore --old-key <fingerprint>` uses it "
                    "so as not to blindly accept the old keys the peer sends")


def cmd_key(args) -> int:
    with _open() as store:
        if args.rotate:
            for line in access.rotate_key(store):
                print(line)
            print(f"new fingerprint: {keys.fingerprint(store.public_line())}")
            print(FINGERPRINT_NOTE)
            return 0
        line = store.public_line()
    if args.fingerprint:
        print(keys.fingerprint(line))
    else:
        print(line)                       # alone on stdout: `authorize <node> "$(cairn key)"`
        warn(f"fingerprint: {keys.fingerprint(line)} ({FINGERPRINT_NOTE})")
    return 0


def cmd_authorize(args) -> int:
    pub = args.key
    if Path(pub).expanduser().is_file():
        pub = Path(pub).expanduser().read_text(encoding="utf-8").strip()
    command = shlex.split(args.command) if args.command else None
    for line in access.authorize(args.node, pub, ssh=not args.no_ssh, command=command,
                                 replace=args.replace):
        print(line)
    return 0


def cmd_revoke(args) -> int:
    for line in access.revoke(args.node):
        print(line)
    return 0


def cmd_peers(args) -> int:
    cfg = config.load()
    with_ssh = set(access.ssh_authorized_nodes())
    if not cfg.peers:
        print("No authorized nodes.")
    for p in cfg.peers.values():
        access_ = f"revoked {p.revoked}" if p.revoked else ("ssh" if p.node in with_ssh else "events only")
        print(f"{p.node:<16} {access_:<18} {keys.fingerprint(p.key)}")
        for k in p.old:
            print(f"{'':<16} {'previous':<18} {keys.fingerprint(k.key)}  until {k.until[:19]}")
    return 0


def cmd_remote(args) -> int:
    if args.remote_cmd == "add":
        access.remote_add(args.name, args.url, key=args.key, command=args.command)
        print(f"Remote {args.name}: {args.url}")
    elif args.remote_cmd == "remove":
        access.remote_remove(args.name)
        print(f"Remote {args.name} removed")
    else:
        cfg = config.load()
        if not cfg.remotes:
            print("No remotes.")
        for r in cfg.remotes.values():
            print(f"{r.name:<12} {r.url}" + (f"  (key {r.key})" if r.key else ""))
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
            raise CairnError("No remotes: `cairn remote add <name> ssh://user@host`")
        timeout = args.timeout or store.config.sync_timeout
        if (args.old_key or args.trust_peer_keys) and not args.restore:
            raise CairnError("--old-key and --trust-peer-keys are for `sync --restore`")
        if args.restore:
            try:
                old_keys = [keys.as_fingerprint(k) for k in args.old_key]
            except keys.KeyFileError as e:
                raise CairnError(f"--old-key: {e}") from None
            if len(args.remotes) != 1:
                raise CairnError("--restore brings this node's log from a single remote: "
                                 "`cairn sync --restore <remote>`")
            if store.log.segments(store.config.node):
                raise CairnError(f"{store.config.node} already has events of its own: restore is "
                                 "for a node that lost its data and was reinitialized")
            remote = store.config.remotes.get(args.remotes[0])
            if remote is None:
                raise CairnError(f"No such remote: {args.remotes[0]}")
            results = [sync.sync_remote(store, remote, timeout=timeout, restore=True,
                                        old_keys=old_keys, trust_peer_keys=args.trust_peer_keys)]
        else:
            results = sync.sync_all(store, args.remotes, timeout=timeout)
    when = f"{clock.now().astimezone().isoformat(timespec='seconds')} " if args.quiet else ""
    for r in results:
        if not args.quiet or not r.ok:
            print(f"{when}{r.remote} ({r.peer or '?'}): {r.sent} sent, {r.received} received"
                  + ("" if r.ok else f", {len(r.errors)} error(s)"))
        sys.stdout.flush()
        for e in r.errors:
            warn(f"  [error] {e}")
    return 0 if all(r.ok for r in results) else 1


def cmd_sync_peer(args) -> int:
    if not args.stdio:
        raise CairnError("sync-peer is launched by `cairn sync` on the other side, over ssh (--stdio)")
    return sync.serve_stdio(args.as_node)


# ──────────────────────────────────────────────────────────────── parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn", description="A local-first store for working docs and handoffs.")
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("init", help="initializes the node: config, data folder and empty database")
    p.add_argument("--node", help="node name (default: the hostname)")
    p.add_argument("--data", help="data folder (default: ~/.local/share/cairn)")
    p.add_argument("--projects", help="projects root (default: ~/projects)")
    p.add_argument("--author", help="author of the docs (default: `git config user.name`, or the user)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("new", help="creates a doc: diagnosis (bug/infra), plan or reference")
    p.add_argument("area")
    p.add_argument("topic")
    p.add_argument("type", nargs="?", default="infra", help="bug | infra | plan | ref (default: infra)")
    p.add_argument("--repo", help="the repo it belongs to (default: the PWD's; `cross-repo` if it "
                   "spans several)")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("close", help="closes a doc (status resolved/completed/discarded + `closed:`)")
    p.add_argument("doc")
    p.add_argument("-f", "--force", action="store_true", help="close even with unchecked `- [ ]` items")
    p.add_argument("--discard", metavar="REASON", help="close it unfinished: status `discarded`, "
                   "the reason in `note:` (needs no --force)")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("reopen", help="reopens a closed doc (or makes a handoff pending again)")
    p.add_argument("doc")
    p.add_argument("reason", nargs="?", help="goes in `note:`")
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("promote", help="copies sections of a doc to a reference and links them")
    p.add_argument("doc")
    p.add_argument("area")
    p.add_argument("topic")
    p.add_argument("--section", action="append", help="start of the section's title (repeatable); "
                   "default: Context + Useful commands | Design + Decisions")
    p.add_argument("-f", "--force", action="store_true", help="promote even if already promoted")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("link", help="links two docs by hand: <doc> gets <other> under <kind>:")
    p.add_argument("doc")
    p.add_argument("kind", choices=list(docs.LINK_KINDS))
    p.add_argument("other")
    p.add_argument("--remove", action="store_true", help="removes the link instead")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("list", help="lists the docs (default: the open ones)")
    p.add_argument("scope", nargs="?", default="wip", choices=["wip", "done", "ref", "all"])
    p.add_argument("--by-repo", action="store_true",
                   help="grouped by repo, with how many there are and when each one changed")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("search", help="searches all docs (regex, case-insensitive)")
    p.add_argument("pattern")
    p.add_argument("scope", nargs="?", default="all", choices=["wip", "done", "ref", "all"])
    p.add_argument("--fts", action="store_true", help="full-text search (FTS5) by relevance")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("weekly", help="digest of what was closed in the last days")
    p.add_argument("days", nargs="?", type=int, default=7)
    p.add_argument("format", nargs="?", default="summary", choices=["summary", "full"])
    p.set_defaults(func=cmd_weekly)

    p = sub.add_parser("check", help="is it in order? log, projection and docs (exits 1 if not)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("edit", help="edits a doc in $EDITOR (or with --file/--stdin/--set/--unset)")
    p.add_argument("doc")
    p.add_argument("--file", help="the whole doc from a file (without front-matter: only the body)")
    p.add_argument("--stdin", action="store_true", help="the whole doc from stdin")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="a front-matter field")
    p.add_argument("--unset", action="append", metavar="KEY", help="removes a front-matter field")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("show", help="shows a doc as a file; `doc@rev` shows a revision")
    p.add_argument("doc", help="name or id, with @<hash> or @~N for a revision")
    p.add_argument("--section", help="only that section (the start of its title), numbered")
    p.add_argument("--lines", help="only those lines (`40-80`, `40-`, `40`), numbered")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("log", help="event history of a doc (or of the whole database)")
    p.add_argument("doc", nargs="?")
    p.add_argument("-n", type=int, help="maximum number")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("diff", help="diff between revisions (default: the last change)")
    p.add_argument("doc")
    p.add_argument("rev_a", nargs="?", help="from (hash or ~N)")
    p.add_argument("rev_b", nargs="?", help="to (default: the head)")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("resolve", help="shows a doc's divergent branches and resolves which one stays")
    p.add_argument("doc")
    p.add_argument("--keep", metavar="main|HASH", help="`main` (the main one) or a branch's hash")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("rebuild", help="rebuilds the SQLite projection from the event log")
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("defaults", help="exports the built-in templates and MCP instructions to a "
                       "folder, to customize them")
    p.add_argument("folder")
    p.add_argument("-f", "--force", action="store_true", help="overwrite files that already exist")
    p.set_defaults(func=cmd_defaults)

    p = sub.add_parser("serve", help="local read-only web view of the docs (127.0.0.1)")
    p.add_argument("--port", type=int, default=8737, help="default: 8737")
    p.add_argument("--open", action="store_true", help="open it in the browser")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mcp", help="stdio MCP server (the agent launches it, it is not run by hand)")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("integrate", help="registers cairn's MCP and hooks in an agent "
                       "(claude-code) or the periodic sync timer (systemd)")
    p.add_argument("target", choices=["claude-code", "systemd"])
    p.add_argument("--remove", action="store_true", help="undoes the integration")
    p.add_argument("--dry-run", action="store_true", help="shows what it would do, touching nothing")
    p.add_argument("--command", help="how to invoke cairn (default: the command's path; "
                   "e.g. \"ssh vps cairn\" to use another node's database)")
    p.add_argument("--no-mcp", action="store_true", help="leave the MCP alone")
    p.add_argument("--no-hooks", action="store_true", help="leave the hooks alone")
    p.set_defaults(func=cmd_integrate)

    p = sub.add_parser("key", help="this node's public key (for `authorize` on the other one)")
    p.add_argument("--fingerprint", action="store_true",
                   help="only the fingerprint (`SHA256:…`), to write it down outside the node")
    p.add_argument("--rotate", action="store_true",
                   help="change it (the previous one keeps verifying what it already signed; peers "
                        "accept the new one with `authorize --replace`)")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("authorize", help="accepts a node's events and gives it ssh sync access")
    p.add_argument("node")
    p.add_argument("key", help="its public key (`cairn key` on that node), or a file with it")
    p.add_argument("--no-ssh", action="store_true",
                   help="only trust its events (it does not connect to this node)")
    p.add_argument("--command", help="how cairn is invoked here, for authorized_keys "
                   "(default: the command's path)")
    p.add_argument("--replace", action="store_true",
                   help="the node changed its key: the previous one stays to verify what it signed")
    p.set_defaults(func=cmd_authorize)

    p = sub.add_parser("revoke", help="removes a node's access and rejects its new events")
    p.add_argument("node")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("peers", help="the authorized nodes")
    p.set_defaults(func=cmd_peers)

    r = sub.add_parser("remote", help="the nodes this one syncs with")
    rs = r.add_subparsers(dest="remote_cmd", metavar="<add|remove|list>")
    p = rs.add_parser("add")
    p.add_argument("name")
    p.add_argument("url", help="ssh://user@host[:port]")
    p.add_argument("--key", help="ssh key (default: the node's)")
    p.add_argument("--command", help="how cairn is invoked on the other side (default: cairn)")
    p = rs.add_parser("remove")
    p.add_argument("name")
    rs.add_parser("list")
    r.set_defaults(func=cmd_remote, remote_cmd="list")

    p = sub.add_parser("sync", help="exchanges events with the remotes")
    p.add_argument("remotes", nargs="*", help="default: all of them")
    p.add_argument("--quiet", action="store_true", help="errors only")
    p.add_argument("--timeout", type=float, help="seconds per remote (default: `[sync] timeout`, 60)")
    p.add_argument("--background", action="store_true",
                   help="launch it in the background and return (output in <data>/sync.log)")
    p.add_argument("--after", type=float, metavar="SEC", help=argparse.SUPPRESS)  # the debounce
    p.add_argument("--restore", action="store_true",
                   help="also bring this node's own log (it lost its data and was reinitialized)")
    p.add_argument("--old-key", action="append", default=[], metavar="FINGERPRINT",
                   help="with --restore: fingerprint (or public key) of a previous key of this "
                        "node, written down outside it; one per key. Keys the peer sends that do "
                        "not match stop the restore")
    p.add_argument("--trust-peer-keys", action="store_true",
                   help="with --restore: accept the previous keys the peer sends without "
                        "checking them (if the fingerprints were not written down)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("sync-peer", help="the other side of `sync` (ssh launches it, it is not run by hand)")
    p.add_argument("--stdio", action="store_true")
    p.add_argument("--as", dest="as_node", help="identity fixed by authorized_keys")
    p.set_defaults(func=cmd_sync_peer)

    m = sub.add_parser("migrate", help="migrate from another tool")
    ms = m.add_subparsers(dest="tool", required=True)
    w = ms.add_parser("wip-doc", help="from wip-doc: the <root>/{_wip,_done,_ref} tree and its hook")
    ws = w.add_subparsers(dest="wip_doc_cmd", required=True)
    p = ws.add_parser("import", help="imports the docs in <root>/{_wip,_done,_ref} (once)")
    p.add_argument("root", help="the folder that has _wip/, _done/ and _ref/ (e.g. ~/projects)")
    p.add_argument("--dry-run", action="store_true", help="show what would be imported, writing nothing")
    p.add_argument("-v", "--verbose", action="store_true", help="list each doc")
    p.set_defaults(func=cmd_wip_doc_import)
    p = ws.add_parser("export", help="writes the docs as files in <dest>/{_wip,_done,_ref}")
    p.add_argument("dest")
    p.add_argument("scope", nargs="?", default="all", choices=["wip", "done", "ref", "all"])
    p.add_argument("-f", "--force", action="store_true", help="overwrite files with other content")
    p.set_defaults(func=cmd_wip_doc_export)
    p = ws.add_parser("check", help="what is left of wip-doc: docs not imported, files already "
                      "imported, _wip/ inside repos (exits 1 if any)")
    p.add_argument("root", nargs="?", help="default: the projects root")
    p.set_defaults(func=cmd_wip_doc_check)
    p = ws.add_parser("unhook", help="removes wip-doc's handoffs hook from Claude Code")
    p.add_argument("--dry-run", action="store_true", help="shows what it would do, touching nothing")
    p.set_defaults(func=cmd_wip_doc_unhook)

    h = sub.add_parser("handoff", help="notes for the next conversation")
    hs = h.add_subparsers(dest="handoff_cmd", metavar="<new|take|drop|list|hook>", required=True)
    p = hs.add_parser("new", help="leaves a pending note")
    p.add_argument("area")
    p.add_argument("topic")
    p.add_argument("--to", action="append", help="which repo it is for (repeatable; default: `repo:`)")
    p.add_argument("--plan", action="append", help="the plan it points to (repeatable)")
    p.add_argument("--repo", help="the repo it belongs to (default: the PWD's)")
    p.set_defaults(func=cmd_handoff_new)
    p = hs.add_parser("take", help="prints it and archives it (status taken)")
    p.add_argument("note", nargs="?", help="without a name: the only one pending for the PWD's repo")
    p.set_defaults(func=cmd_handoff_take)
    p = hs.add_parser("drop", help="archives it without using it (status dropped)")
    p.add_argument("note")
    p.add_argument("reason", nargs="?")
    p.set_defaults(func=cmd_handoff_drop)
    p = hs.add_parser("list", help="pending ones for the PWD's repo")
    p.add_argument("--all", action="store_true", help="all of them")
    p.set_defaults(func=cmd_handoff_list)
    p = hs.add_parser("hook", help="for Claude Code's SessionStart hook: announces pending ones")
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
