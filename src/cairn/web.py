"""`cairn serve`: a local, read-only web view of the docs.

The views are functions that take a store and return HTML, so they are tested without a server;
`serve` puts them behind the standard library's HTTP server, listening only on 127.0.0.1. Each
request opens the store, reads and closes it, like any CLI command: what the last sync brought
shows up on reload. Nothing here writes events.

A doc is shown rendered as markdown when `markdown-it-py` is installed (`cairn[serve]`), and
otherwise, or with `?view=text`, as it is stored and edited: as text, front-matter included, so
line numbers match `show` and `search`, with an outline, headings with anchors and checklist
items marked. In both, the names of other docs become links, and headings get the same anchor
(`L<line>`), so the outline works for either.
"""

from __future__ import annotations

import difflib
import html
import re
import sys
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from cairn import config, docs, history, reading
from cairn.docs import Doc
from cairn.markdown import COMMENT, first_sentences
from cairn.store import CairnError, Store

try:
    from markdown_it import MarkdownIt
except ImportError:         # without `cairn[serve]`, docs are shown only as text
    MarkdownIt = None

DEFAULT_PORT = 8737

_CSS = """
:root { --bg: #fbfbfa; --fg: #1f2328; --muted: #6a737d; --line: #e4e4e1; --panel: #f2f2ef;
  --accent: #0b5cad; --todo: #9a5b00; --done: #6a737d; --add: #1a7f37; --del: #cf222e;
  --mark: #fff1a8; --head: #1f2328; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg: #16181c;
  --fg: #e3e3e0; --muted: #949ba4; --line: #2c3038; --panel: #1e2126; --accent: #6cb0ff;
  --todo: #e3a84a; --done: #7d848c; --add: #5ccf7a; --del: #ff7b72; --mark: #5a4b10;
  --head: #ffffff; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header { display: flex; gap: 20px; align-items: baseline; flex-wrap: wrap; padding: 12px 16px;
  border-bottom: 1px solid var(--line); }
header .brand { font-weight: 600; color: var(--fg); }
header form { margin-left: auto; }
input, select, button { font: inherit; color: var(--fg); background: var(--panel);
  border: 1px solid var(--line); border-radius: 6px; padding: 3px 8px; }
main { max-width: 1180px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.4em; margin: 4px 0 12px; }
h1 code { font: .85em ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
h2 { font-size: 1.05em; margin: 24px 0 6px; color: var(--muted); font-weight: 600; }
.muted { color: var(--muted); }
.tag { display: inline-block; padding: 0 7px; margin: 0 4px 4px 0; border: 1px solid var(--line);
  border-radius: 10px; font-size: .85em; background: var(--panel); }
.row { padding: 6px 0; border-bottom: 1px solid var(--line); }
.row .sum { color: var(--muted); font-size: .92em; }
.filters { margin-bottom: 8px; }
.filters a { margin-right: 10px; }
.filters a.on { font-weight: 600; color: var(--fg); }
.doc { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 24px; }
.doc nav { position: sticky; top: 12px; align-self: start; font-size: .9em; max-height: 92vh;
  overflow: auto; }
.doc nav a { display: block; padding: 1px 0; color: var(--muted); }
@media (max-width: 760px) { .doc { grid-template-columns: 1fr; } .doc nav { position: static; } }
pre { margin: 0; font: 13px/1.5 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  white-space: pre-wrap; overflow-wrap: anywhere; }
pre.text { padding: 12px 0; }
pre.text > span { display: block; position: relative; padding-left: 4.5em; min-height: 1.5em; }
pre .n { position: absolute; left: 0; width: 3.5em; color: var(--muted); user-select: none;
  text-align: right; opacity: .6; }
pre .h { color: var(--head); font-weight: 700; }
pre .fm { color: var(--muted); }
pre .todo { color: var(--todo); }
pre .done { color: var(--done); }
pre .add { color: var(--add); }
pre .del { color: var(--del); }
pre .hunk { color: var(--muted); }
mark { background: var(--mark); color: inherit; }
.notice { padding: 8px 12px; border: 1px solid var(--todo); border-radius: 6px; margin-bottom: 12px; }
.rev { border-top: 1px solid var(--line); padding: 10px 0; }
.md { max-width: 52em; overflow-wrap: anywhere; }
.md h1 { display: none; }
.md h2 { font-size: 1.25em; color: var(--fg); margin: 28px 0 8px; padding-bottom: 4px;
  border-bottom: 1px solid var(--line); }
.md h3 { font-size: 1.08em; margin: 20px 0 6px; }
.md h4, .md h5, .md h6 { font-size: 1em; margin: 16px 0 4px; }
.md hr { border: 0; border-top: 1px solid var(--line); margin: 20px 0; }
.md code { font: .9em ui-monospace, "SF Mono", Menlo, Consolas, monospace; background: var(--panel);
  padding: 1px 4px; border-radius: 4px; }
.md pre { background: var(--panel); padding: 10px 12px; border-radius: 6px; overflow-x: auto;
  white-space: pre; }
.md pre code { background: none; padding: 0; }
.md table { border-collapse: collapse; margin: 8px 0; display: block; overflow-x: auto; }
.md th, .md td { border: 1px solid var(--line); padding: 4px 8px; text-align: left;
  vertical-align: top; overflow-wrap: normal; min-width: 5em; }
.md th { background: var(--panel); }
.md blockquote { margin: 8px 0; padding: 0 12px; border-left: 3px solid var(--line);
  color: var(--muted); }
.md li.task { list-style: none; margin-left: -1.3em; }
.md li.task.done { color: var(--done); }
.box { display: inline-block; width: .8em; height: .8em; border: 1.5px solid var(--todo);
  border-radius: 2px; margin-right: .45em; vertical-align: -.05em; }
.box.on { border-color: var(--done); background: var(--done); }
details.fm { margin: 4px 0 8px; color: var(--muted); font-size: .9em; }
"""


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def doc_url(name: str, suffix: str = "") -> str:
    return f"/doc/{quote(name)}{suffix}"


def page(title: str, body: str, q: str = "") -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc(title)} · cairn</title><style>{_CSS}</style></head><body>"
            '<header><a class="brand" href="/">cairn</a><a href="/">open</a>'
            '<a href="/?scope=ref">references</a><a href="/?scope=done">closed</a>'
            '<a href="/weekly">weekly</a>'
            f'<form action="/search"><input name="q" value="{esc(q)}" placeholder="search (FTS)" '
            'aria-label="search"></form></header>'
            f"<main>{body}</main></body></html>")


# ──────────────────────────────────────────────────────────────── the queue

def _summary(doc: Doc) -> str:
    s = first_sentences(doc.tldr, 1)
    return s[:197] + "..." if len(s) > 200 else s


def _doc_row(doc: Doc, when: str) -> str:
    sum_ = _summary(doc)
    return (f'<div class="row"><a href="{doc_url(doc.name)}">{esc(doc.name)}</a> '
            f'<span class="tag">{esc(doc.type)}</span><span class="muted">{esc(when)}</span>'
            + (f'<div class="sum">{esc(sum_)}</div>' if sum_ else "") + "</div>")


def _filter_links(label: str, key: str, values: list[str], current: dict[str, str],
                  blank: str = "all") -> str:
    """One line of filters: `blank` is the one that leaves the key out of the query string."""
    out = []
    for v in ["", *values]:
        params = {**current, key: v}
        qs = "&".join(f"{k}={quote(x)}" for k, x in params.items() if x)
        cls = ' class="on"' if current.get(key, "") == v else ""
        out.append(f'<a{cls} href="/{"?" + qs if qs else ""}">{esc(v or blank)}</a>')
    return f'<div class="filters"><span class="muted">{label}:</span> {"".join(out)}</div>'


def view_index(store: Store, scope: str = "wip", repo: str = "", type_: str = "") -> str:
    """The docs of a scope grouped by repo (as `list --by-repo`), with the pending handoffs
    first. `repo` and `type_` filter."""
    scopes = docs.SCOPES if scope == "all" else (scope,)
    if not set(scopes) <= set(docs.SCOPES):
        raise CairnError(f"Unknown scope: {scope}")
    every = docs.all_docs(store, scopes)
    current = {"scope": scope if scope != "wip" else "", "repo": repo, "type": type_}
    filters = (_filter_links("scope", "scope", ["done", "ref", "all"], current, blank="open")
               + _filter_links("type", "type", sorted({d.type for d in every}), current)
               + _filter_links("repo", "repo", sorted({docs.repo_label(d) for d in every}), current))
    shown = [d for d in every if (not repo or docs.repo_label(d) == repo)
             and (not type_ or d.type == type_)]
    handoffs = [d for d in shown if d.type == "handoff" and d.scope == "wip"]
    work = [d for d in shown if d not in handoffs]
    title = {"wip": "Open", "done": "Closed", "ref": "References", "all": "All docs"}[scope]
    out = [f"<h1>{title} <span class=\"muted\">({len(shown)})</span></h1>", filters]
    if handoffs:
        out.append("<h2>pending handoffs</h2>")
        out += [_doc_row(d, docs.handoff_line(d, store.config.handoff_stale_days)
                         .removeprefix(d.name).strip(" ()")) for d in handoffs]
    groups: dict[str, list[Doc]] = {}
    for d in work:
        groups.setdefault(docs.repo_label(d), []).append(d)
    for name, ds in groups.items():
        out.append(f"<h2>{esc(name)} ({len(ds)})</h2>")
        out += [_doc_row(d, f"{docs.status_label(d)} · {docs.modified_label(d)}") for d in ds]
    if not shown:
        out.append('<p class="muted">Nothing here.</p>')
    return page(title, "".join(out))


# ──────────────────────────────────────────────────────────────── a doc

_TOKEN = re.compile(r"[\w./+-]+")
_TODO = re.compile(r"^\s*[-*] \[ \]")
_DONE = re.compile(r"^\s*[-*] \[[xX]\]")


def _linked(line: str, names: set[str], self_name: str) -> str:
    """The line escaped, with the names of other docs (bare, with `.md` or as a tree path)
    turned into links."""
    out, last = [], 0
    for m in _TOKEN.finditer(line):
        token = m.group(0).rstrip(".,;:")
        name = docs.normalize_ref(token)
        if name in names and name != self_name:
            out.append(esc(line[last:m.start()]))
            out.append(f'<a href="{doc_url(name)}">{esc(token)}</a>')
            last = m.start() + len(token)
    out.append(esc(line[last:]))
    return "".join(out)


def text_html(text: str, names: set[str], self_name: str = "") -> str:
    """A doc as numbered text: front-matter dimmed, headings bold with an anchor (`L<n>`),
    checklist items marked, other docs linked. Inside code fences only the links."""
    lines = text.splitlines()
    body_start = reading._body_start(lines)
    out, fence = [], False
    for n, line in enumerate(lines, 1):
        content = _linked(line, names, self_name)
        cls = ""
        if n <= body_start:
            cls = "fm"
        elif reading.FENCE.match(line):
            fence = not fence
        elif not fence and reading.HEADING.match(line):
            cls = "h"
        elif not fence and _TODO.match(line):
            cls = "todo"
        elif not fence and _DONE.match(line):
            cls = "done"
        content = f'<span class="{cls}">{content}</span>' if cls else content
        out.append(f'<span id="L{n}"><a class="n" href="#L{n}">{n}</a>{content}</span>')
    return '<pre class="text">' + "".join(out) + "</pre>"


def _md_inline(children, names: set[str], self_name: str) -> None:
    """Inside a paragraph, heading or cell: links to `../_ref/x.md` and bare doc names (in text
    or `code`) point at the doc's page. Never inside a link already."""
    in_link = 0
    for c in children:
        if c.type == "link_open":
            in_link += 1
            href = c.attrGet("href") or ""
            name = docs.normalize_ref(href) if "://" not in href and not href.startswith("#") else ""
            if name in names:
                c.attrSet("href", doc_url(name))
        elif c.type == "link_close":
            in_link -= 1
        elif in_link:
            continue
        elif c.type == "text":
            c.type, c.content = "html_inline", _linked(c.content, names, self_name)
        elif c.type == "code_inline":
            c.type, c.content = "html_inline", f"<code>{_linked(c.content, names, self_name)}</code>"


def markdown_html(text: str, names: set[str], self_name: str = "") -> str | None:
    """The doc rendered as markdown (None without `markdown-it-py`). Raw HTML in a doc is shown
    as text, not rendered; template comments (`<!-- -->`) are left out."""
    if MarkdownIt is None:
        return None
    lines = text.splitlines()
    start = reading._body_start(lines)
    body = COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), "\n".join(lines[start:]))
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    tokens = md.parse(body)
    item = None
    for tok in tokens:
        if tok.type == "heading_open" and tok.map:
            tok.attrSet("id", f"L{start + tok.map[0] + 1}")
        elif tok.type == "list_item_open":
            item = tok
        elif tok.type == "inline" and tok.children:
            first = tok.children[0]
            if item is not None and first.type == "text" and first.content[:4] in ("[ ] ", "[x] ", "[X] "):
                on = first.content[1] != " "
                item.attrSet("class", "task done" if on else "task")
                first.content = first.content[4:]
                _md_inline(tok.children, names, self_name)
                tok.children.insert(0, type(first)("html_inline", "", 0,
                                                   content=f'<span class="box{" on" if on else ""}"></span>'))
            else:
                _md_inline(tok.children, names, self_name)
            item = None
    fm = ""
    if start:
        fm = ('<details class="fm"><summary>front-matter</summary><pre>'
              + esc("\n".join(lines[:start])) + "</pre></details>")
    return fm + '<div class="md">' + md.renderer.render(tokens, md.options, {}) + "</div>"


def title_html(title: str) -> str:
    """A doc's title, with its `code` spans as code."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(title))


def _names(store: Store) -> set[str]:
    return {r[0] for r in store.projection.db.execute("SELECT name FROM docs")}


def _incoming(store: Store, doc: Doc) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in store.projection.db.execute(
        "SELECT d.name, l.kind FROM links l JOIN docs d ON d.id = l.src WHERE l.dst = ? "
        "ORDER BY d.name", (doc.id,))]


def _tags(doc: Doc) -> str:
    """What says where the doc stands; the rest of the front-matter is in the text."""
    return "".join(f'<span class="tag">{esc(v)}</span>' for v in
                   (doc.type, doc.status or docs.status_label(doc), docs.repo_label(doc)) if v)


def view_doc(store: Store, ref: str, as_text: bool = False) -> str:
    """A doc (or a revision, `name@rev`), with its outline and its links both ways: rendered,
    or as text (`as_text`, or without `markdown-it-py`)."""
    name, rev = history.split_ref(ref)
    current = docs.find(store, name)
    doc, text = history.show(store, ref)
    names = _names(store)
    outline = "".join(
        f'<a href="#L{s.start}" style="padding-left:{(s.level - 1) * 10}px">{esc(s.title)}</a>'
        for s in reading.sections(text) if s.level > 1)
    out_links = []
    for kind, dst in doc.links:
        target = docs.get(store, dst)
        if target is not None:
            out_links.append(f'{esc(kind)} → <a href="{doc_url(target.name)}">{esc(target.name)}</a>')
    in_links = [f'<a href="{doc_url(n)}">{esc(n)}</a> → {esc(k)}' for n, k in _incoming(store, current)]
    links = ""
    if out_links or in_links:
        links = "<h2>links</h2>" + "<br>".join(out_links + in_links)
    notice = ""
    if rev is not None:
        notice = (f'<div class="notice">Revision <code>{esc(doc.head[:10])}</code> of this doc, not '
                  f'the current one: <a href="{doc_url(current.name)}">see the current one</a>.</div>')
    elif current.divergent:
        notice = ('<div class="notice">This doc has divergent branches (edited on two nodes at once): '
                  f"<code>cairn resolve {esc(current.name)}</code>.</div>")
    shown = None if as_text else markdown_html(text, names, doc.name)
    toggle = ""
    if MarkdownIt is not None:
        here = doc_url(ref)
        toggle = (f'<a href="{here}">rendered</a>' if shown is None
                  else f'<a href="{here}?view=text">as text</a>')
    nav = (f'<nav><a href="{doc_url(current.name, "/log")}">history</a>{toggle}'
           f"<h2>outline</h2>{outline}{links}</nav>")
    body = (f"<h1>{title_html(doc.title)}</h1>{notice}<div>{_tags(doc)}</div>"
            f'<div class="doc">{nav}<div>{shown or text_html(text, names, doc.name)}</div></div>')
    return page(doc.name, body)


# ──────────────────────────────────────────────────────────────── history

def diff_html(a: str, b: str) -> str:
    lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=2))[2:]
    if not lines:
        return '<p class="muted">No changes to the text.</p>'
    out = []
    for line in lines:
        cls = {"+": "add", "-": "del", "@": "hunk"}.get(line[:1], "")
        out.append(f'<span class="{cls}">{esc(line)}</span>' if cls else esc(line))
    return "<pre>" + "\n".join(out) + "</pre>"


def view_log(store: Store, ref: str) -> str:
    """The revisions of a doc, newest first, each with its diff against the previous one."""
    doc = docs.find(store, ref)
    revs = history.revisions(store, doc)
    out = [f'<h1>History of <a href="{doc_url(doc.name)}">{esc(doc.name)}</a> '
           f'<span class="muted">({len(revs)} revisions)</span></h1>']
    texts = [docs.render(store, r.doc) for r in revs]
    for i in range(len(revs) - 1, -1, -1):
        r = revs[i]
        link = doc_url(f"{doc.name}@{r.event_id[:10]}")
        when = r.ts[:19].replace("T", " ")
        out.append(f'<div class="rev"><a href="{link}"><code>{esc(r.event_id[:10])}</code></a> '
                   f'<span class="muted">{esc(when)} · {esc(r.node)} · {esc(r.op)}</span>'
                   f"{diff_html(texts[i - 1] if i else '', texts[i])}</div>")
    return page(f"history of {doc.name}", "".join(out))


# ──────────────────────────────────────────────────────────────── weekly / search

def view_weekly(store: Store, days: int = 7) -> str:
    since, closed = docs.weekly(store, days)
    on = ' class="on"'
    out = [f'<h1>Closed since {esc(since)} <span class="muted">({len(closed)})</span></h1>',
           '<div class="filters">' + "".join(
               f'<a{on if d == days else ""} href="/weekly?days={d}">{d} days</a>'
               for d in (7, 14, 30, 90)) + "</div>"]
    groups: dict[str, list[Doc]] = {}
    for d in sorted(closed, key=docs.repo_label):
        groups.setdefault(docs.repo_label(d), []).append(d)
    for repo, ds in groups.items():
        out.append(f"<h2>{esc(repo)} ({len(ds)})</h2>")
        out += [_doc_row(d, f"{d.meta.get('status', '')} {d.date_closed()}") for d in ds]
    return page("weekly", "".join(out))


def view_search(store: Store, q: str) -> str:
    if not q.strip():
        return page("search", "<h1>Search</h1><p class=\"muted\">Type something above.</p>")
    hits, total = docs.search_fts(store, q, limit=50)
    out = [f"<h1>{esc(docs.fts_header(q, hits, total))}</h1>"]
    for h in hits:
        snippet = esc(h.snippet).replace("«", "<mark>").replace("»", "</mark>")
        out.append(f'<div class="row"><a href="{doc_url(h.doc.name)}">{esc(h.doc.name)}</a> '
                   f'<span class="tag">{esc(h.doc.type)}</span><span class="muted">'
                   f'{esc(docs.status_label(h.doc))}</span><div class="sum">{snippet}</div></div>')
    return page(f"search: {q}", "".join(out), q)


# ──────────────────────────────────────────────────────────────── routing and server

def route(store: Store, target: str) -> tuple[int, str]:
    """(status, HTML) for a request path with its query string."""
    url = urlsplit(target)
    path = unquote(url.path)
    qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
    try:
        if path == "/":
            return 200, view_index(store, qs.get("scope", "wip"), qs.get("repo", ""), qs.get("type", ""))
        if path == "/weekly":
            days = qs.get("days", "7")
            if not days.isdigit():
                raise CairnError(f"days: a number, not {days!r}")
            return 200, view_weekly(store, int(days))
        if path == "/search":
            return 200, view_search(store, qs.get("q", ""))
        if path.startswith("/doc/"):
            ref = path[len("/doc/"):]
            if ref.endswith("/log"):
                return 200, view_log(store, ref[:-len("/log")])
            return 200, view_doc(store, ref, qs.get("view") == "text")
    except CairnError as e:
        return 404, page("not found", f"<h1>Not found</h1><p>{esc(e)}</p>")
    return 404, page("not found", f"<h1>Not found</h1><p>{esc(path)}</p>")


def make_handler(open_store: Callable[[], Store]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            try:
                with open_store() as store:
                    status, body = route(store, self.path)
            except (CairnError, config.ConfigError) as e:
                status, body = 500, page("error", f"<h1>Error</h1><p>{esc(e)}</p>")
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args) -> None:      # quiet: errors only
            if args and str(args[1])[:1] in "45":
                sys.stderr.write(f"{self.address_string()} {format % args}\n")

    return Handler


def make_server(port: int, open_store: Callable[[], Store]) -> ThreadingHTTPServer:
    """Only on 127.0.0.1: to see another node, an ssh tunnel."""
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), make_handler(open_store))
    except OSError as e:
        raise CairnError(f"Could not listen on 127.0.0.1:{port}: {e.strerror or e}") from None


def serve(port: int = DEFAULT_PORT, open_browser: bool = False) -> None:
    server = make_server(port, lambda: Store(config.load()))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"cairn serve: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
