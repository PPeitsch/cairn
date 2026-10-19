"""`cairn serve`: a local, read-only web view of the docs.

The views are functions that take a store and return HTML, so they are tested without a server;
`serve` puts them behind the standard library's HTTP server, listening only on 127.0.0.1. Each
request opens the store, reads and closes it, like any CLI command: what the last sync brought
shows up on reload (and the page offers the reload when it notices). Nothing here writes events.

A doc is shown rendered as markdown when `markdown-it-py` is installed (`cairn[serve]`), and
otherwise, or with `?view=text`, as it is stored and edited: as text, front-matter included, so
line numbers match `show` and `search`. In both, the names of other docs become links, and
headings get the same anchor (`L<line>`), so the outline works for either. With the extra, code
blocks are highlighted (Pygments) and `mermaid` blocks are drawn in the browser; mermaid is
loaded from a CDN only on pages that have a diagram, and without network its source stays.

The style and the script are in `web_assets/` and are served from `/static/`; the pages work
without the script.
"""

from __future__ import annotations

import difflib
import html
import json
import os
import re
import sys
import webbrowser
from collections.abc import Callable
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, quote, unquote, urlsplit

from cairn import clock, config, docs, history, reading
from cairn.docs import Doc
from cairn.markdown import COMMENT, first_sentences
from cairn.store import CairnError, Store

try:
    from markdown_it import MarkdownIt
except ImportError:         # without `cairn[serve]`, docs are shown only as text
    MarkdownIt = None
try:
    from pygments import highlight as _pygments
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name
    from pygments.util import ClassNotFound
except ImportError:         # without it, code blocks are plain
    _pygments = None

DEFAULT_PORT = 8737
MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs"
STALE_DAYS = 21             # an open doc untouched this long is flagged as quiet
ASSETS = ("style.css", "app.js")
_ON = ' class="on"'

OP_LABEL = {
    "create": "created", "import": "imported", "handoff_new": "handoff created", "edit": "edited",
    "set_state": "state changed", "close": "closed", "reopen": "reopened", "promote": "promoted",
    "link": "linked", "unlink": "unlinked", "handoff_take": "handoff taken",
    "handoff_drop": "handoff dropped", "resolve": "branches resolved",
}

# ──────────────────────────────────────────────────────────────── icons (own SVGs, theme colored)

_SVG = '<svg class="i{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="{w}" ' \
       'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{d}</svg>'
_PATHS = {
    "logo": ('<ellipse cx="12" cy="19" rx="8" ry="2.6" fill="currentColor" stroke="none"/>'
             '<ellipse cx="12" cy="13.6" rx="5.8" ry="2.3" fill="currentColor" stroke="none" opacity=".75"/>'
             '<ellipse cx="12" cy="8.9" rx="3.9" ry="2" fill="currentColor" stroke="none" opacity=".55"/>'
             '<ellipse cx="12" cy="5" rx="2.2" ry="1.5" fill="currentColor" stroke="none" opacity=".4"/>', 2),
    "search": ('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>', 2),
    "copy": ('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>', 2),
    "sun": ('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
            'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>', 2),
    "moon": ('<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z"/>', 2),
    "auto": ('<circle cx="12" cy="12" r="8"/><path d="M12 4v16a8 8 0 0 0 0-16Z" fill="currentColor"/>', 2),
    "history": ('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>', 2),
    "alert": ('<path d="M12 3 2 20h20L12 3Z"/><path d="M12 10v4M12 17h.01"/>', 2),
}


def icon(name: str, cls: str = "") -> str:
    d, w = _PATHS[name]
    return _SVG.format(cls=f" {cls}" if cls else "", w=w, d=d)


# ──────────────────────────────────────────────────────────────── small pieces

def esc(s) -> str:
    return html.escape(str(s), quote=True)


def doc_url(name: str, suffix: str = "") -> str:
    return f"/doc/{quote(name)}{suffix}"


def _qs(params: dict[str, str]) -> str:
    q = "&".join(f"{k}={quote(v)}" for k, v in params.items() if v)
    return f"?{q}" if q else ""


def title_html(title: str) -> str:
    """A doc's title (or a line of its summary), with its `code` and **bold** spans."""
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(title))
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)


def _local(ts: str) -> datetime:
    tz = clock.now().tzinfo if os.environ.get("CAIRN_NOW") else None
    return datetime.fromisoformat(ts).astimezone(tz)


def _days(ts: str) -> int:
    return (date.fromisoformat(clock.today()) - _local(ts).date()).days


def ago(ts: str) -> str:
    d = _days(ts)
    if d <= 0:
        return "today"
    if d == 1:
        return "yesterday"
    return f"{d} days ago" if d < 45 else _local(ts).date().isoformat()


def copyable(text: str, shown: str | None = None) -> str:
    return (f'<button type="button" class="copyable" data-copy="{esc(text)}" title="copy">'
            f"<span>{esc(shown or text)}</span>{icon('copy')}</button>")


def type_badge(t: str) -> str:
    return f'<span class="badge type t-{esc(t)}">{esc(t)}</span>'


def status_badge(doc: Doc) -> str:
    if doc.scope == "ref":
        key, label = "reference", "reference"
    else:
        key = doc.status or ("open" if doc.scope == "wip" else "completed")
        label = key.replace("_", " ")
    return f'<span class="badge dot s-{esc(key)}">{esc(label)}</span>'


_TODO = re.compile(r"^\s*[-*] \[ \]")
_DONE = re.compile(r"^\s*[-*] \[[xX]\]")


def checklist(body: str) -> tuple[int, int]:
    """(done, total) of the `- [ ]` / `- [x]` items outside code fences."""
    done = total = 0
    fence = False
    for line in body.splitlines():
        if reading.FENCE.match(line):
            fence = not fence
        elif not fence and _TODO.match(line):
            total += 1
        elif not fence and _DONE.match(line):
            done += 1
            total += 1
    return done, total


def progress(done: int, total: int, big: bool = False) -> str:
    if not total:
        return ""
    pct = round(100 * done / total)
    return (f'<span class="prog{" big" if big else ""}" title="{done} of {total} checklist items done">'
            f'<span class="track"><span class="fill" style="width:{pct}%"></span></span>'
            f"{done}/{total}</span>")


def _summary(doc: Doc) -> str:
    s = first_sentences(doc.tldr, 1)
    return s[:237] + "..." if len(s) > 240 else s


def item(doc: Doc, show_repo: bool = True, extra: str = "", sum_html: str | None = None) -> str:
    """A doc in a list: type, title, status, and a line of facts (and its summary)."""
    facts = [f'<span class="mono">{esc(doc.name)}</span>']
    if show_repo:
        facts.append(f'<a href="/docs{_qs({"scope": "all", "repo": docs.repo_label(doc)})}">'
                     f"{esc(docs.repo_label(doc))}</a>")
    if doc.type == "handoff" and doc.scope == "wip":
        facts.append(f"to: {esc(', '.join(docs.handoff_to(doc)) or docs.CROSS_REPO)}")
    if doc.scope == "done":
        facts.append(f"closed {esc(doc.date_closed())}")
    quiet = doc.scope == "wip" and _days(doc.last_event) >= STALE_DAYS
    facts.append(f'<span class="{"stale" if quiet else ""}">'
                 f'{"quiet, " if quiet else ""}touched {esc(ago(doc.last_event))}</span>')
    if doc.scope != "ref":
        facts.append(progress(*checklist(doc.body)))
    if extra:
        facts.append(extra)
    s = sum_html if sum_html is not None else title_html(_summary(doc))
    return (f'<li class="item">{type_badge(doc.type)}'
            f'<a class="t" href="{doc_url(doc.name)}">{title_html(doc.title)}</a>'
            f'<span class="right">{status_badge(doc)}</span>'
            f'<div class="meta">{"".join(f"<span>{f}</span>" for f in facts if f)}</div>'
            + (f'<div class="sum">{s}</div>' if s else "") + "</li>")


def page(title: str, body: str, store: Store | None = None, active: str = "", q: str = "") -> str:
    state = f' data-state="{store.projection.count_events()}"' if store is not None else ""
    nav = "".join(f'<a href="{href}"{_ON if key == active else ""}>{label}</a>' for key, href, label in (
                      ("home", "/", "Home"), ("open", "/docs", "Open"),
                      ("ref", "/docs?scope=ref", "References"), ("done", "/docs?scope=done", "Closed"),
                      ("activity", "/activity", "Activity"), ("weekly", "/weekly", "Weekly")))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)} · cairn</title>"
        "<script>try{var t=localStorage.getItem('cairn-theme');"
        "if(t)document.documentElement.dataset.theme=t}catch(e){}</script>"
        '<link rel="stylesheet" href="/static/style.css">'
        '<link rel="icon" href="data:image/svg+xml,'
        + quote('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#c15f3c">'
                '<ellipse cx="12" cy="19" rx="8" ry="2.6"/><ellipse cx="12" cy="13.6" rx="5.8" ry="2.3"/>'
                '<ellipse cx="12" cy="8.9" rx="3.9" ry="2"/>'
                '<ellipse cx="12" cy="5" rx="2.2" ry="1.5"/></svg>')
        + '"><script src="/static/app.js" defer></script></head>'
        f'<body{state} data-mermaid="{esc(MERMAID_URL)}">'
        f'<header class="top"><div class="top-in"><a class="brand" href="/">{icon("logo")}cairn</a>'
        f'<nav class="nav">{nav}</nav>'
        f'<form class="search" action="/search">{icon("search")}'
        f'<input name="q" value="{esc(q)}" placeholder="Search docs" aria-label="search" autocomplete="off">'
        '<kbd>/</kbd></form>'
        '<button type="button" class="icon-btn theme" title="theme" aria-label="theme">'
        f'{icon("auto", "i-auto")}{icon("sun", "i-sun")}{icon("moon", "i-moon")}</button>'
        f"</div></header><main>{body}</main>"
        '<div class="toast" role="status">There are new changes.<a href="">Reload</a></div>'
        "</body></html>")


def _sec(title: str, count: int | None = None) -> str:
    c = f'<span class="count">{count}</span>' if count is not None else ""
    return f'<h2 class="sec-title">{title}{c}</h2>'


# ──────────────────────────────────────────────────────────────── home

def _feed(store: Store, lines: list[history.LogLine], by_day: bool = False) -> str:
    titles = {r[0]: r[1] for r in store.projection.db.execute("SELECT name, body FROM docs")}
    out, day = [], None
    for ln in lines:
        local = _local(ln.ts)
        if by_day and local.date() != day:
            day = local.date()
            out.append(f'<li class="day">{esc(day.strftime("%A %d %b %Y"))}</li>')
        body = titles.get(ln.doc_name)
        m = re.search(r"^# (.+)$", body or "", re.M)
        title = title_html(m.group(1).strip()) if m else esc(ln.doc_name)
        link = f'<a href="{doc_url(ln.doc_name)}">{title}</a>' if body is not None else title
        when = local.strftime("%H:%M") if by_day else ago(ln.ts)
        mark = "" if ln.status == "applied" else f' <span class="stale">[{esc(ln.status)}]</span>'
        out.append(f'<li><span class="pip op-{esc(ln.op)}"></span><div class="what">{link} '
                   f'<span class="muted">{esc(OP_LABEL.get(ln.op, ln.op))}</span>{mark}'
                   f'<div class="when">{esc(when)} · {esc(ln.node)}</div></div></li>')
    return f'<ul class="feed">{"".join(out)}</ul>' if out else '<div class="empty">No activity.</div>'


def view_home(store: Store) -> str:
    """What is going on: counts, the open work by repo, pending handoffs, recent activity."""
    wip = docs.all_docs(store, ("wip",))
    handoffs = [d for d in wip if d.type == "handoff"]
    work = [d for d in wip if d.type != "handoff"]
    refs = docs.all_docs(store, ("ref",))
    _, closed = docs.weekly(store, 7)
    by_type = {t: sum(1 for d in work if d.type == t) for t in ("bug", "infra", "plan")}
    bar = "".join(f'<span class="t-{t}" style="flex:{n};background:currentColor"></span>'
                  for t, n in by_type.items() if n)
    split = " · ".join(f"{n} {t}" for t, n in by_type.items() if n)
    stats = (
        f'<a class="card stat" href="/docs"><div class="n">{len(work)}</div>'
        f'<div class="l">open work{" · " + split if split else ""}</div><div class="bar">{bar}</div></a>'
        f'<a class="card stat" href="/docs?type=handoff"><div class="n">{len(handoffs)}</div>'
        '<div class="l">pending handoffs</div></a>'
        f'<a class="card stat" href="/weekly"><div class="n">{len(closed)}</div>'
        '<div class="l">closed in the last 7 days</div></a>'
        f'<a class="card stat" href="/docs?scope=ref"><div class="n">{len(refs)}</div>'
        '<div class="l">references</div></a>')
    groups: dict[str, list[Doc]] = {}
    for d in work:
        groups.setdefault(docs.repo_label(d), []).append(d)
    repos = "".join(
        f'<section class="card"><div class="card-h">{esc(repo)}<span class="count">{len(ds)}</span>'
        f'<a class="more" href="/docs{_qs({"repo": repo})}">all</a></div>'
        f'<ul class="items">{"".join(item(d, show_repo=False) for d in ds)}</ul></section>'
        for repo, ds in groups.items())
    left = _sec("Open work", len(work)) + (f'<div class="repos">{repos}</div>' if repos
                                            else '<div class="empty">Nothing open.</div>')
    if closed:
        left += (_sec("Closed this week", len(closed))
                 + f'<div class="card"><ul class="items">{"".join(item(d) for d in closed[:8])}</ul></div>')
    right = ""
    if handoffs:
        right += (_sec("Pending handoffs", len(handoffs))
                  + f'<div class="card"><ul class="items">{"".join(item(d, extra="") for d in handoffs)}'
                  "</ul></div>")
    right += (_sec("Recent activity") + '<div class="card">'
              + _feed(store, history.log(store, None, 14))
              + '<div class="card-h" style="border:0;border-top:1px solid var(--line)">'
              '<a class="more" href="/activity">all activity</a></div></div>')
    body = (f'<h1 class="page-title">Working docs</h1><p class="page-sub">{esc(store.config.node)} · '
            f"{store.projection.count_events()} events · {len(docs.all_docs(store))} docs</p>"
            f'<div class="stats">{stats}</div>'
            f'<div class="grid-2"><div>{left}</div><aside>{right}</aside></div>')
    return page("Home", body, store, "home")


# ──────────────────────────────────────────────────────────────── lists

_SCOPES = (("", "Open"), ("done", "Closed"), ("ref", "References"), ("all", "All"))


def _select(key: str, blank: str, values: list[tuple[str, int]], current: str) -> str:
    opts = [f'<option value="">{esc(blank)}</option>']
    opts += [f'<option value="{esc(v)}"{" selected" if v == current else ""}>{esc(v)} ({n})</option>'
             for v, n in values]
    on = " on" if current else ""
    return (f'<label class="select{on}"><span class="sr">{esc(blank)}</span>'
            f'<select name="{key}">{"".join(opts)}</select></label>')


def filter_bar(current: dict[str, str], types: list[tuple[str, int]], repos: list[tuple[str, int]],
               shown: int) -> str:
    """One line: the scope as a segmented control, type and repo as menus (with how many docs
    each has), the active ones as removable pills, and how many docs are shown. The menus submit
    on change; without the script, with the button."""
    seg = "".join(f'<a href="/docs{_qs({**current, "scope": v})}"{_ON if current["scope"] == v else ""}>'
                  f"{label}</a>" for v, label in _SCOPES)
    hidden = f'<input type="hidden" name="scope" value="{esc(current["scope"])}">' if current["scope"] else ""
    pills = "".join(
        f'<a class="pill" href="/docs{_qs({**current, key: ""})}" title="remove this filter">'
        f'{esc(key)}: {esc(current[key])}<span aria-hidden="true">×</span></a>'
        for key in ("type", "repo") if current[key])
    return (f'<div class="toolbar"><nav class="seg seg-lg" aria-label="scope">{seg}</nav>'
            f'<form class="filter-form" action="/docs">{hidden}'
            f'{_select("type", "All types", types, current["type"])}'
            f'{_select("repo", "All repos", repos, current["repo"])}'
            '<noscript><button type="submit" class="btn">Apply</button></noscript></form>'
            f'{pills}<span class="toolbar-count">{shown} doc(s)</span></div>')


def view_list(store: Store, scope: str = "wip", repo: str = "", type_: str = "") -> str:
    """The docs of a scope, most recently touched first, filtered by repo and type."""
    scopes = docs.SCOPES if scope == "all" else (scope,)
    if not set(scopes) <= set(docs.SCOPES):
        raise CairnError(f"Unknown scope: {scope}")
    every = docs.all_docs(store, scopes)
    current = {"scope": scope if scope != "wip" else "", "repo": repo, "type": type_}
    by_repo = [d for d in every if not type_ or d.type == type_]
    by_type = [d for d in every if not repo or docs.repo_label(d) == repo]
    count = lambda ds, f: sorted({(f(d), sum(1 for x in ds if f(x) == f(d))) for d in ds})  # noqa: E731
    shown = [d for d in by_repo if not repo or docs.repo_label(d) == repo]
    bar = filter_bar(current, count(by_type, lambda d: d.type), count(by_repo, docs.repo_label), len(shown))
    title = {"wip": "Open", "done": "Closed", "ref": "References", "all": "All docs"}[scope]
    items = "".join(item(d) for d in shown)
    body = (f'<h1 class="page-title">{title}</h1>{bar}'
            + (f'<div class="card"><ul class="items">{items}</ul></div>' if shown
               else '<div class="empty">Nothing here.</div>'))
    return page(title, body, store, {"wip": "open"}.get(scope, scope))


# ──────────────────────────────────────────────────────────────── a doc

_TOKEN = re.compile(r"[\w./+-]+")


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


def _highlighted(code: str, lang: str) -> str | None:
    if _pygments is None or not lang:
        return None
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return None
    return _pygments(code, lexer, HtmlFormatter(nowrap=True)).rstrip("\n")


def _fence(self, tokens, idx, options, env) -> str:
    """Code blocks: `mermaid` as a diagram (drawn by the page's script), the rest highlighted,
    with their language and a copy button."""
    tok = tokens[idx]
    lang = (tok.info.split() or [""])[0].lower()
    if lang == "mermaid":
        return ('<div class="diagram"><pre class="mermaid">' + esc(tok.content) + "</pre>"
                '<div class="note" hidden>The diagram could not be drawn (mermaid did not load: no '
                "network?). This is its source.</div></div>")
    code = _highlighted(tok.content, lang) or esc(tok.content.rstrip("\n"))
    label = f'<span class="lang">{esc(lang)}</span>' if lang else ""
    return (f'<div class="codeblock">{label}<button type="button" class="icon-btn copy" title="copy" '
            f'aria-label="copy">{icon("copy")}</button><pre class="hl"><code>{code}</code></pre></div>')


def markdown_html(text: str, names: set[str], self_name: str = "") -> str | None:
    """The doc rendered as markdown (None without `markdown-it-py`). Raw HTML in a doc is shown
    as text, not rendered; template comments (`<!-- -->`) are left out."""
    if MarkdownIt is None:
        return None
    lines = text.splitlines()
    start = reading._body_start(lines)
    body = COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), "\n".join(lines[start:]))
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    md.add_render_rule("fence", _fence)
    md.add_render_rule("table_open", lambda *a: '<div class="table-wrap"><table>\n')
    md.add_render_rule("table_close", lambda *a: "</table></div>\n")
    tokens = md.parse(body)
    item_tok = anchor = None
    for tok in tokens:
        if tok.type == "heading_open" and tok.map:
            anchor = f"L{start + tok.map[0] + 1}"
            tok.attrSet("id", anchor)
        elif tok.type == "list_item_open":
            item_tok = tok
        elif tok.type == "inline" and tok.children:
            Token = type(tok.children[0])
            first = tok.children[0]
            task = first.type == "text" and first.content[:4] in ("[ ] ", "[x] ", "[X] ")
            if item_tok is not None and task:
                on = first.content[1] != " "
                item_tok.attrSet("class", "task done" if on else "task")
                first.content = first.content[4:]
                _md_inline(tok.children, names, self_name)
                tok.children.insert(0, Token("html_inline", "", 0,
                                             content=f'<span class="box{" on" if on else ""}"></span>'))
            else:
                _md_inline(tok.children, names, self_name)
            if anchor:
                tok.children.append(Token("html_inline", "", 0,
                                          content=f'<a class="anchor" href="#{anchor}">#</a>'))
            item_tok = anchor = None
    fm = ""
    if start:
        fm = ('<details class="fm"><summary>front-matter</summary><pre>'
              + esc("\n".join(lines[:start])) + "</pre></details>")
    return '<div class="md">' + md.renderer.render(tokens, md.options, {}) + "</div>" + fm


def _names(store: Store) -> set[str]:
    return {r[0] for r in store.projection.db.execute("SELECT name FROM docs")}


def _incoming(store: Store, doc: Doc) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in store.projection.db.execute(
        "SELECT d.name, l.kind FROM links l JOIN docs d ON d.id = l.src WHERE l.dst = ? "
        "ORDER BY d.name", (doc.id,))]


def _link_li(kind: str, target: Doc | None, name: str) -> str:
    title = title_html(target.title) if target else esc(name)
    return (f'<li><span class="k">{esc(kind)}</span><a href="{doc_url(name)}">{title}</a></li>')


def view_doc(store: Store, ref: str, as_text: bool = False) -> str:
    """A doc (or a revision, `name@rev`): its head (type, status, facts, checklist), the doc
    rendered or as text (`as_text`, or without `markdown-it-py`), and at the side its outline,
    its links both ways, its history and the commands to work on it."""
    name, rev = history.split_ref(ref)
    current = docs.find(store, name)
    doc, text = history.show(store, ref)
    names = _names(store)
    revs = history.revisions(store, current)
    shown = None if as_text else markdown_html(text, names, doc.name)
    notice = ""
    if rev is not None:
        notice = (f'<div class="notice">{icon("alert")}<div>Revision <code>{esc(doc.head[:10])}</code> '
                  f'of this doc, not the current one. <a href="{doc_url(current.name)}">See the current '
                  "one</a>.</div></div>")
    elif current.divergent:
        notice = (f'<div class="notice">{icon("alert")}<div>This doc has divergent branches (edited on two '
                  f"nodes at once): <code>cairn resolve {esc(current.name)}</code>.</div></div>")
    created = str(doc.meta.get("date") or current.created[:10])
    facts = [copyable(doc.name), f"created {esc(created)}", f"touched {esc(ago(current.last_event))}"]
    if doc.scope == "done":
        facts.append(f"closed {esc(doc.date_closed())}")
    if doc.meta.get("updated"):
        facts.append(f"updated {esc(doc.meta['updated'])}")
    if doc.meta.get("author"):
        facts.append(esc(doc.meta["author"]))
    if doc.meta.get("note"):
        facts.append(f'<span class="stale">note: {esc(doc.meta["note"])}</span>')
    repo = docs.repo_label(doc)
    head = (f'<div class="crumbs"><a href="/">Home</a>/<a href="/docs{_qs({"scope": "all", "repo": repo})}">'
            f"{esc(repo)}</a>/<span>{esc(doc.name)}</span></div>"
            f'<div class="doc-head"><div class="badges">{type_badge(doc.type)}{status_badge(doc)}</div>'
            f'<h1 class="page-title">{title_html(doc.title)}</h1>'
            f'<div class="facts">{"".join(f"<span>{f}</span>" for f in facts)}</div></div>')
    # the side
    toc = "".join(f'<a class="l{s.level}" href="#L{s.start}">{esc(s.title)}</a>'
                  for s in reading.sections(text) if s.level > 1)
    view = ""
    if MarkdownIt is not None:
        here = doc_url(ref)
        view = (f'<div class="row"><span class="muted">view</span><span class="seg">'
                f'<a href="{here}"{_ON if shown else ""}>rendered</a>'
                f'<a href="{here}?view=text"{"" if shown else _ON}>text</a></span></div>')
    done, total = checklist(doc.body)
    status_card = (
        f'<div class="card">{view}'
        + (f'<div class="row"><span class="muted">checklist</span>{progress(done, total, True)}</div>'
           if total else "")
        + f'<div class="row"><span class="muted">history</span><a href="{doc_url(current.name, "/log")}">'
        f'{len(revs)} revision(s)</a></div></div>')
    links = [_link_li(k, docs.get(store, dst), docs.get(store, dst).name)
             for k, dst in doc.links if docs.get(store, dst) is not None]
    links += [_link_li(f"{k} (from)", None, n) for n, k in _incoming(store, current)]
    side = status_card
    if toc:
        side += f'<div class="card toc-card"><h3>On this page</h3><nav class="toc toc-wrap">{toc}</nav></div>'
    if links:
        side += f'<div class="card"><h3>Links</h3><ul class="links-list">{"".join(links)}</ul></div>'
    side += (f'<div class="card"><h3>Commands</h3><div class="cmds">{copyable(f"cairn show {doc.name}")}'
             f'{copyable(f"cairn edit {current.name}")}</div></div>')
    content = (f'<article class="card doc-body">{shown}</article>' if shown else
               f'<article class="card doc-body text">{text_html(text, names, doc.name)}</article>')
    body = (f'{head}{notice}<div class="doc-grid"><div>{content}</div>'
            f'<aside class="side">{side}</aside></div>')
    return page(doc.title, body, store)


# ──────────────────────────────────────────────────────────────── history

def _diff_lines(a: str, b: str) -> list[str]:
    return list(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=2))[2:]


def diff_html(a: str, b: str) -> str:
    lines = _diff_lines(a, b)
    if not lines:
        return '<div class="nochange">No changes to the text.</div>'
    out = []
    for line in lines:
        cls = {"+": "add", "-": "del", "@": "hunk"}.get(line[:1], "")
        attr = f' class="{cls}"' if cls else ""
        out.append(f"<span{attr}>{esc(line) or ' '}</span>")
    return '<pre class="diff-text">' + "".join(out) + "</pre>"


def view_log(store: Store, ref: str) -> str:
    """The revisions of a doc as a timeline, newest first, each with its diff against the
    previous one (the newest open, the rest folded)."""
    doc = docs.find(store, ref)
    revs = history.revisions(store, doc)
    texts = [docs.render(store, r.doc) for r in revs]
    out = []
    for i in range(len(revs) - 1, -1, -1):
        r = revs[i]
        before = texts[i - 1] if i else ""
        lines = _diff_lines(before, texts[i])
        add = sum(1 for x in lines if x.startswith("+"))
        rm = sum(1 for x in lines if x.startswith("-"))
        when = _local(r.ts).strftime("%Y-%m-%d %H:%M")
        out.append(
            f'<details class="card rev op-{esc(r.op)}"{" open" if i == len(revs) - 1 else ""}><summary>'
            f'<span class="badge">{esc(OP_LABEL.get(r.op, r.op))}</span>'
            f'<span class="when">{esc(when)} · {esc(r.node)}</span>'
            f'<span class="stat-add">+{add}</span><span class="stat-del">-{rm}</span>'
            f'<a class="open-rev mono" href="{doc_url(f"{doc.name}@{r.event_id[:10]}")}">'
            f"{esc(r.event_id[:10])}</a></summary>"
            f'<div class="diff">{diff_html(before, texts[i])}</div></details>')
    body = (f'<div class="crumbs"><a href="/">Home</a>/<a href="{doc_url(doc.name)}">{esc(doc.name)}</a>'
            "/<span>history</span></div>"
            f'<h1 class="page-title">History</h1><p class="page-sub">{title_html(doc.title)} · '
            f"{len(revs)} revisions</p>"
            f'<div class="timeline">{"".join(out)}</div>')
    return page(f"history of {doc.name}", body, store)


def view_activity(store: Store, n: int = 150) -> str:
    """Every change on this node's database, newest first, by day."""
    lines = history.log(store, None, n)
    body = (f'<h1 class="page-title">Activity</h1><p class="page-sub">The last {len(lines)} changes, from '
            f'every node</p><div class="card">{_feed(store, lines, by_day=True)}</div>'
            + (f'<p class="muted"><a href="/activity?n={n * 2}">Show more</a></p>'
               if len(lines) == n else ""))
    return page("Activity", body, store, "activity")


# ──────────────────────────────────────────────────────────────── weekly / search

def view_weekly(store: Store, days: int = 7) -> str:
    since, closed = docs.weekly(store, days)
    seg = "".join(f'<a href="/weekly?days={d}"{_ON if d == days else ""}>{d} days</a>'
                  for d in (7, 14, 30, 90))
    groups: dict[str, list[Doc]] = {}
    for d in sorted(closed, key=docs.repo_label):
        groups.setdefault(docs.repo_label(d), []).append(d)
    cards = "".join(
        f'<section class="card" style="margin-bottom:14px"><div class="card-h">{esc(repo)}'
        f'<span class="count">{len(ds)}</span></div>'
        f'<ul class="items">{"".join(item(d, show_repo=False) for d in ds)}</ul></section>'
        for repo, ds in groups.items())
    body = (f'<h1 class="page-title">Closed since {esc(since)}</h1>'
            f'<p class="page-sub">{len(closed)} piece(s) of work in the last {days} days</p>'
            f'<div class="toolbar"><nav class="seg seg-lg" aria-label="window">{seg}</nav></div>'
            + (cards or '<div class="empty">Nothing closed in this window.</div>'))
    return page("Weekly", body, store, "weekly")


def view_search(store: Store, q: str) -> str:
    form = (f'<form class="search-big" action="/search"><input name="q" value="{esc(q)}" '
            'placeholder="Words, a name, a prefix*: full-text, by relevance" autofocus></form>')
    if not q.strip():
        return page("Search", f'<h1 class="page-title">Search</h1>{form}', store, q=q)
    hits, total = docs.search_fts(store, q, limit=50)
    items = "".join(item(h.doc, sum_html=esc(h.snippet).replace("«", "<mark>").replace("»", "</mark>"))
                    for h in hits)
    body = (f'<h1 class="page-title">Search</h1>{form}'
            f'<p class="page-sub">{esc(docs.fts_header(q, hits, total))}</p>'
            + (f'<div class="card"><ul class="items">{items}</ul></div>' if hits
               else '<div class="empty">No doc matches.</div>'))
    return page(f"search: {q}", body, store, q=q)


def not_found(message: str) -> str:
    return page("not found", f'<h1 class="page-title">Not found</h1><p class="page-sub">{esc(message)}</p>'
                '<p><a href="/">Back home</a></p>')


# ──────────────────────────────────────────────────────────────── routing and server

def route(store: Store, target: str) -> tuple[int, str]:
    """(status, HTML) for a request path with its query string."""
    url = urlsplit(target)
    path = unquote(url.path)
    qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
    try:
        if path == "/":
            return 200, view_home(store)
        if path == "/docs":
            return 200, view_list(store, qs.get("scope", "wip"), qs.get("repo", ""), qs.get("type", ""))
        if path in ("/weekly", "/activity"):
            n = qs.get("days" if path == "/weekly" else "n", "7" if path == "/weekly" else "150")
            if not n.isdigit() or not 0 < int(n) <= 5000:
                raise CairnError(f"a number from 1 to 5000, not {n!r}")
            return 200, (view_weekly if path == "/weekly" else view_activity)(store, int(n))
        if path == "/search":
            return 200, view_search(store, qs.get("q", ""))
        if path.startswith("/doc/"):
            ref = path[len("/doc/"):]
            if ref.endswith("/log"):
                return 200, view_log(store, ref[:-len("/log")])
            return 200, view_doc(store, ref, qs.get("view") == "text")
    except CairnError as e:
        return 404, not_found(str(e))
    return 404, not_found(path)


def asset(name: str) -> bytes | None:
    if name not in ASSETS:
        return None
    return resources.files("cairn").joinpath("web_assets", name).read_bytes()


def make_handler(open_store: Callable[[], Store]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, data: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if path.startswith("/static/"):
                data = asset(path[len("/static/"):])
                ctype = "text/css" if path.endswith(".css") else "text/javascript"
                if data is None:
                    self._send(404, b"not found", "text/plain")
                else:
                    self._send(200, data, f"{ctype}; charset=utf-8")
                return
            try:
                with open_store() as store:
                    if path == "/api/state":
                        state = {"events": store.projection.count_events()}
                        self._send(200, json.dumps(state).encode(), "application/json")
                        return
                    status, body = route(store, self.path)
            except (CairnError, config.ConfigError) as e:
                status, body = 500, page("error", f'<h1 class="page-title">Error</h1><p>{esc(e)}</p>')
            self._send(status, body.encode("utf-8"), "text/html; charset=utf-8")

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
