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
cairn keeps the working docs (diagnoses, plans, references) and the handoffs between
conversations. It is the source of truth: do not write these docs as loose files.

When to document
- When investigating a problem, diagnosing a root cause, applying infra or operations fixes,
  or designing a feature with non-obvious decisions: open a doc when starting (cairn_new) and
  close it when done (cairn_close).
- Do not document the trivial (a one-line edit, a one-off question) nor what already lives in
  the repo's code or git history. The doc is for what cannot be worked out by looking at the
  repo: the why, the path the diagnosis took, the decisions.
- Before opening a doc for something that sounds familiar, search (cairn_search): it may
  already be diagnosed.

Types and structure (cairn_new creates the skeleton; fill it in with cairn_edit)
- bug / infra (diagnosis): TL;DR (the first line says what was done), Context / topology
  (with the traps), Symptoms observed (real evidence, not paraphrased), Discoveries in order
  (root cause highlighted), Fixes applied, Pending (`- [ ]`), Useful commands.
- plan: TL;DR, Context / problem, Scope (what it includes and what not), Design / proposal
  (phases if incremental), Decisions (alternatives dropped and why), Pending.
- ref: reference material written in the present tense (how something IS today). It is
  neither opened nor closed: it is updated. A closed doc tells what happened; a reference, how
  something is. When closing, ask whether something will be looked up again even if the
  problem does not come back: if so, cairn_promote carries those sections to a reference and
  links the two docs. If a reference is a survey with findings (a review), mark the state of
  each one (OK / partial / pending) and, when re-checking it, against which commit or version.

Searching and reading without spending context
- For a question about a topic ("what do we have on X?"): cairn_search with fts=true, which
  sorts by relevance and shows the snippet that matches. The regex is for exact names or
  identifiers; a broad regex (common words joined with `|`) brings a lot and helps little.
- Read in parts: cairn_show of a long doc returns its outline; ask for the section
  (`section=`) or the lines (`lines=`, the ones search gives) that are needed. The whole doc
  (`full=true`) only if all of it is really needed.

Files that are not docs
- Validation or debug scripts, generated reports and CSV files do not go into cairn. If the
  config names folders for them (`[docs] file_dirs`), cairn_search also searches their `.md`
  files and cairn_check warns when something is out of place.

Names
- cairn builds them: `<YYYY-MM-DD>_<area>_<topic>` (bugs `_BUG_`, handoffs `_HANDOFF_`);
  references `<area>_<topic>`, without a date. `area` is the main system or repo; everything in
  snake_case. A doc is referred to by its name (with or without `.md`) or by a prefix of its id.
- The state lives in the front-matter (type, repo, status, date, closed, …) and the
  operations manage it: do not repeat it in the body. `repo:` comes from the conversation's
  directory; `cross-repo` for work that spans several.

Handoffs (notes for the next conversation)
- To pass a request or context to the next conversation: cairn_handoff_new. The note points
  to the plan (plan=...), it does not copy it, and it asks for nothing destructive.
- If there is a pending handoff at startup, tell the user and ask whether to take it: do not
  take it or follow it on your own. Taking it (cairn_handoff_take) returns it and archives it.
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
    return f"{d.name}  [{d.type} · {docs.status_label(d)} · {docs.repo_label(d)}]"


def _with_warnings(text: str, warnings: list[str]) -> str:
    return "\n".join([text, *warnings]) if warnings else text


# ──────────────────────────────────────────────────────────────── tools

def cairn_list(scope: Scope = "wip", by_repo: bool = True) -> str:
    """Lists the docs: `wip` (open, with the pending handoffs apart), `done` (closed), `ref`
    (references) or `all`. From the most recently modified to the oldest, each with the date of
    its last change: that is what tells what was being worked on (the date in the name is the
    creation date). By default grouped by repo with each group's count: when summarizing it,
    use those counts and name every doc in the group, do not count them again.
    `by_repo=false` gives one line per doc."""
    with _open() as store:
        found = docs.all_docs(store, _scopes(scope))
    if not found:
        return f"No docs in {scope}."
    work = [d for d in found if not (d.scope == "wip" and d.type == "handoff")]
    handoffs = [d for d in found if d.scope == "wip" and d.type == "handoff"]
    if by_repo:
        out = docs.grouped_by_repo(work) if work else []
    else:
        out = [f"{_line(d)}  ({docs.modified_label(d)})" for d in work]
    if handoffs:
        stale = store.config.handoff_stale_days
        out += ["", "pending handoffs:", *(f"  {docs.handoff_line(d, stale)}" for d in handoffs)]
    return "\n".join(out).lstrip("\n")


def cairn_search(pattern: str, scope: Scope = "all", fts: bool = False) -> str:
    """Searches all docs. By default `pattern` is a regex (case-insensitive) over the doc as a
    file, and it returns the matching lines (with their number: usable with cairn_show
    `lines`), docs with the most matches first and with a cap; the rest, by name only. The
    regex is for exact names or identifiers; for a topic, `fts=true`: a full-text query (FTS5
    syntax, ignoring accents and case) by relevance; each result carries the snippet where it
    matches, with the match between «»; if there are more than 20, it says so."""
    with _open() as store:
        if fts:
            found, total = docs.search_fts(store, pattern, _scopes(scope))
            if not found:
                return f'FTS search "{pattern}": no matches.'
            return "\n".join([docs.fts_header(pattern, found, total),
                              *(f"{_line(h.doc)}\n    {h.snippet}" for h in found)])
        hits, total = docs.search(store, pattern, _scopes(scope))
        files = tree.search_files(store, pattern, _scopes(scope))
    if not hits and not files:
        return f'Search "{pattern}": no matches in {total} docs.'
    max_docs, max_lines = store.config.search_max_docs, store.config.search_max_lines
    ranked = sorted(hits, key=lambda h: -len(h.lines))     # stable: ties keep repo and date order
    shown, rest = ranked[:max_docs], ranked[max_docs:]
    out = [f'Search "{pattern}": {len(hits)} doc(s) of {total}'
           + (f"; showing the {len(shown)} with the most matches" if rest else "")]
    for h in shown:
        out.append(_line(h.doc))
        out += [f"    {n}: {line.strip()[:160]}" for n, line in h.lines[:max_lines]]
        if len(h.lines) > max_lines:
            out.append(f"    (+{len(h.lines) - max_lines} more)")
    if rest:
        names = ", ".join(f"{h.doc.name} ({len(h.lines)})" for h in rest[:25])
        extra = f" and {len(rest) - 25} more" if len(rest) > 25 else ""
        out += ["", f"Another {len(rest)} doc(s) with fewer matches: {names}{extra}.",
                "To narrow it down: a more precise pattern, fts=true (by relevance) or "
                "cairn_show with lines= around a match."]
    if files:
        out += ["", f"files in {tree.label(store)} ({len(files)}):"]
        for f in files:
            out.append(f"  {f.path}")
            out += [f"    {n}: {line.strip()[:160]}" for n, line in f.lines[:5]]
    return "\n".join(out)


def cairn_show(doc: str, section: str | None = None, lines: str | None = None,
               full: bool = False) -> str:
    """Shows a doc as a file (front-matter + body). A long doc (more than
    `[mcp] show_max_chars`, 8000 by default) comes as an outline: front-matter, title, TL;DR and
    the sections with their lines and size. To read a part: `section` (the start of the title,
    e.g. "Decisions") or `lines` ("40-80", with the numbers cairn_search gives), which come
    numbered; `full=true` gives the whole doc. `doc@<hash>` or `doc@~N` shows an earlier
    revision (see cairn_log)."""
    with _open() as store:
        _, text = history.show(store, doc)
        max_chars = store.config.show_max_chars
    return reading.view(text, section, lines, full, max_chars)


def cairn_new(area: str, topic: str, type: Literal["bug", "infra", "plan", "ref"] = "infra",
              body: str | None = None, repo: str | None = None) -> str:
    """Creates a doc: diagnosis (`bug` or `infra`), `plan` or reference (`ref`), with its type's
    skeleton. `area` and `topic` in snake_case. `body` (optional) replaces the skeleton: the body
    in markdown without front-matter, starting with the `# ...` title. `repo`: the repo it
    belongs to (default: the conversation's directory's; `cross-repo` if it spans several).
    Returns the doc's name and its content."""
    with _open() as store:
        r = docs.new(store, area, topic, type, _cwd_repo(store, repo))
        if body is not None:
            r = edit.apply(store, r.doc.id, body=body)
        return f"Created: {r.doc.name}\n\n{docs.render(store, r.doc)}"


def cairn_edit(doc: str, old_text: str | None = None, new_text: str | None = None,
               body: str | None = None, set_fields: dict[str, str] | None = None,
               unset_fields: list[str] | None = None) -> str:
    """Edits a doc. To change a part: `old_text` (it has to appear exactly once in the body) and
    `new_text`. To replace the whole body: `body` (markdown without front-matter).
    `set_fields` / `unset_fields`: front-matter fields (e.g. `note`, `status` within the same
    open/closed group). Opening, closing and linking are not done by editing: that is what
    cairn_close, cairn_reopen, cairn_promote, cairn_link and the handoffs are for."""
    if (old_text is None) != (new_text is None):
        raise ToolError("old_text and new_text go together")
    if old_text is not None and body is not None:
        raise ToolError("old_text/new_text or body, not both")
    with _open() as store:
        d = docs.find(store, doc)
        new_body = body
        if old_text is not None:
            n = d.body.count(old_text)
            if n != 1:
                raise ToolError(f"old_text appears {n} times in the body of {d.name}: it has to "
                                "appear exactly once (add context)")
            new_body = d.body.replace(old_text, new_text, 1)
        r = edit.apply(store, d.id, body=new_body, set_=set_fields, unset=unset_fields,
                       expected_head=d.head)
    outcome = "Saved" if r.extra.get("changed") else "No changes"
    return _with_warnings(f"{outcome}: {r.doc.name}", r.warnings)


def cairn_close(doc: str, force: bool = False, discard: str | None = None) -> str:
    """Closes a diagnosis or plan (status resolved/completed + closing date). It fails if
    unchecked `- [ ]` items remain, unless `force=true`. If it is left unfinished (abandoned, no
    longer makes sense), `discard` with the reason: it becomes `discarded`, the reason in
    `note:`, and it needs no `force`. References are not closed, and handoffs are archived with
    cairn_handoff_take / cairn_handoff_drop."""
    with _open() as store:
        r = docs.close(store, doc, force=force, discard=discard)
    return _with_warnings(f"Closed: {r.doc.name} ({r.doc.status})", r.warnings)


def cairn_reopen(doc: str, reason: str | None = None) -> str:
    """Reopens a closed doc (or makes an archived handoff pending again). `reason` goes in
    `note:`."""
    with _open() as store:
        r = docs.reopen(store, doc, reason)
    return _with_warnings(f"Reopened: {r.doc.name}", r.warnings)


def cairn_promote(doc: str, area: str, topic: str, sections: list[str] | None = None,
                  force: bool = False) -> str:
    """Copies sections of a doc to the reference `<area>_<topic>` (creating it if it does not
    exist) and links the two. `sections`: starts of the titles (default: Context + Useful
    commands in a diagnosis, Design + Decisions in a plan). Afterwards, what was copied has to
    be rewritten as a reference: what IS, not what happened."""
    with _open() as store:
        r = docs.promote(store, doc, area, topic, sections, force=force)
    return _with_warnings(f"{r.doc.name}\n  carried over: {', '.join(r.extra['carried'])}",
                          r.warnings)


def cairn_link(doc: str, kind: Literal["origin", "references", "plan"], other: str,
               remove: bool = False) -> str:
    """Links two docs by hand: `doc` gets `other` under `<kind>:` (one way; `other` shows it as
    an incoming link). For what promote and the handoffs do not link on their own: pending
    items carried to a new doc (`origin`), a plan that came out of another one. `remove=true`
    takes the link out."""
    with _open() as store:
        r = docs.link(store, doc, kind, other, remove=remove)
    if not r.extra["changed"]:
        return _with_warnings(f"No changes: {r.doc.name}", r.warnings)
    return f"{'Unlinked' if remove else 'Linked'}: {r.doc.name} {kind} → {r.extra['other']}"


def cairn_weekly(days: int = 7) -> str:
    """What was closed in the last `days` days, with its TL;DR (no handoffs)."""
    with _open() as store:
        since, closed = docs.weekly(store, days)
    if not closed:
        return f"Nothing closed since {since}."
    out = [f"{len(closed)} doc(s) closed since {since}:"]
    for d in closed:
        how = "discarded" if d.status == "discarded" else "closed"
        out += ["", f"{d.title}  [{d.type} · {how} {d.date_closed()} · {docs.repo_label(d)}]",
                d.tldr or "(no TL;DR)"]
    return "\n".join(out)


def cairn_check() -> str:
    """Checks that the database is in order: log, projection, docs (status, names, links, stale
    handoffs) and, if the config names file folders, what is out of place in them."""
    with _open() as store:
        problems = docs.check(store) + tree.problems(store)
    if not problems:
        return "All in order."
    return "\n".join([f"{len(problems)} problem(s):", *(f"  - {p}" for p in problems)])


def cairn_log(doc: str | None = None, n: int = 20) -> str:
    """Event history of a doc (or of the whole database), newest first. Each line starts with
    the revision's hash, usable with cairn_show `doc@<hash>` and cairn_diff."""
    with _open() as store:
        lines = history.log(store, doc, n)
    if not lines:
        return "No events."
    return "\n".join(
        f"{ln.event_id[:10]}  {ln.ts[:19].replace('T', ' ')}  {ln.node}  {ln.op}  {ln.doc_name}"
        + ("" if ln.status == "applied" else f"  [{ln.status}]") for ln in lines)


def cairn_diff(doc: str, rev_a: str | None = None, rev_b: str | None = None) -> str:
    """Unified diff between two revisions of a doc (hash or `~N`). With no revisions: the last
    change; with one: from there to the current state."""
    with _open() as store:
        out = history.diff(store, doc, rev_a, rev_b)
    return out or "No differences."


def cairn_handoff_new(area: str, topic: str, body: str | None = None, to: list[str] | None = None,
                      plan: list[str] | None = None, repo: str | None = None) -> str:
    """Leaves a note for the next conversation. `to`: the repos it is for (default: the
    note's `repo:`, which comes from the conversation's directory). `plan`: the docs it points
    to. `body`: the note in markdown without front-matter (`# ...` title, TL;DR, what to read
    first, work order, what to ask). The note points to the plan, it does not copy it."""
    with _open() as store:
        r = docs.handoff_new(store, area, topic, to, plan, _cwd_repo(store, repo))
        warnings = r.warnings
        if body is not None:
            r = edit.apply(store, r.doc.id, body=body)
        return _with_warnings(f"Handoff created: {r.doc.name}\n\n{docs.render(store, r.doc)}", warnings)


def cairn_handoff_list(all_repos: bool = False, repo: str | None = None) -> str:
    """The pending handoffs for the conversation's repo (or `repo`), or all of them with
    `all_repos=true`."""
    with _open() as store:
        r = "" if all_repos else _cwd_repo(store, repo)
        pend = docs.pending_handoffs(store, r)
    where = r or "the whole projects root"
    if not pend:
        return f"No pending handoffs for {where}."
    stale = store.config.handoff_stale_days
    return "\n".join([f"Pending handoffs for {where}:",
                      *(f"  {docs.handoff_line(d, stale)}" for d in pend)])


def cairn_handoff_take(note: str | None = None, repo: str | None = None) -> str:
    """Takes a handoff: returns it and archives it (status taken). Only when the user said to
    take it. Without `note`, the only one pending for the conversation's repo."""
    with _open() as store:
        r = docs.handoff_take(store, note, _cwd_repo(store, repo))
    warnings = [f"[warning] {a}" for a in r.warnings]
    return _with_warnings(f"Handoff taken: {r.doc.name} (archived, status: taken)", warnings) \
        + "\n\n" + r.doc.body


def cairn_handoff_drop(note: str, reason: str | None = None) -> str:
    """Discards a handoff without using it (archives it, status dropped). `reason` goes in
    `note:`."""
    with _open() as store:
        r = docs.handoff_drop(store, note, reason)
    return f"Handoff discarded: {r.doc.name} (archived)"


TOOLS = [
    (cairn_list, READ), (cairn_search, READ), (cairn_show, READ), (cairn_weekly, READ),
    (cairn_check, READ), (cairn_log, READ), (cairn_diff, READ), (cairn_handoff_list, READ),
    (cairn_new, WRITE), (cairn_edit, WRITE), (cairn_close, WRITE), (cairn_reopen, WRITE),
    (cairn_promote, WRITE), (cairn_link, WRITE), (cairn_handoff_new, WRITE), (cairn_handoff_take, WRITE),
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
        raise CairnError(f"[mcp] instructions: could not read {path}: {e}") from None


def build_server() -> MCPServer:
    server = MCPServer("cairn", instructions=instructions(), version=__version__)
    for fn, hints in TOOLS:
        server.add_tool(_tool_errors(fn), annotations=hints, structured_output=False)
    return server


def run() -> None:
    build_server().run("stdio")
