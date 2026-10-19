"""`cairn serve`: the views as HTML (without a server) and the server itself on a free port."""

import json
import threading
import urllib.error
import urllib.request

from cairn import config, docs, edit, web
from cairn.store import Store

HOY = "2026-03-10"


def _plan(store, topic="cache", repo="api"):
    return docs.new(store, "api", topic, "plan", repo).doc


def _main(body):
    return body.split("<main>")[1].split("</main>")[0]


def test_home_counts_open_work_by_repo_handoffs_and_activity(store):
    a = _plan(store, "cache", "api")
    b = _plan(store, "colas", "web")
    h = docs.handoff_new(store, "api", "seguir", to=["api"], plans=[a.name], repo="api").doc
    status, body = web.route(store, "/")
    assert status == 200
    main = _main(body)
    assert '<div class="n">2</div><div class="l">open work · 2 plan</div>' in main
    assert '<div class="n">1</div><div class="l">pending handoffs</div>' in main
    assert 'href="/docs?repo=api">all</a>' in main and 'href="/docs?repo=web">all</a>' in main
    assert all(f'href="/doc/{d.name}"' in main for d in (a, b, h))
    assert "to: api" in main and "handoff created" in main
    assert 'data-state="3"' in body                                  # events, for the reload offer


def test_list_filters_with_counts(store):
    a = _plan(store, "cache", "api")
    b = _plan(store, "colas", "web")
    _, body = web.route(store, "/docs")
    assert '<option value="web">web (1)</option>' in body and '<a href="/docs" class="on">Open</a>' in body
    assert 'class="pill"' not in body and "2 doc(s)" in body
    _, only_web = web.route(store, "/docs?repo=web")
    assert b.name in _main(only_web) and f'href="/doc/{a.name}"' not in only_web
    assert '<option value="web" selected>web (1)</option>' in only_web
    assert '<a class="pill" href="/docs" title="remove this filter">repo: web' in only_web
    _, closed_web = web.route(store, "/docs?scope=done&repo=web")
    assert '<input type="hidden" name="scope" value="done">' in closed_web      # the menus keep it
    _, refs = web.route(store, "/docs?scope=ref")
    assert "Nothing here." in refs
    assert web.route(store, "/docs?scope=nope")[0] == 404


def test_a_doc_as_text_with_outline_links_and_checklist(store):
    a = _plan(store)
    b = _plan(store, "colas")
    text = docs.render(store, a).replace("- [ ]", f"- [ ] seguir con `{b.name}`\n- [x] hecho")
    edit.apply(store, a.id, text=text)
    status, body = web.route(store, f"/doc/{a.name}?view=text")
    assert status == 200
    pre = body.split('<pre class="text">')[1].split("</pre>")[0]
    assert f'<a href="/doc/{b.name}">{b.name}</a>' in pre                  # another doc, linked
    assert f'href="/doc/{a.name}"' not in pre                              # not itself
    assert 'class="todo"' in pre and 'class="done"' in pre
    assert '<span class="h">## TL;DR</span>' in pre
    assert '<a class="l2" href="#L11">TL;DR</a>' in body                   # the outline
    assert f'href="/doc/{a.name}/log"' in body
    assert 'title="1 of 2 checklist items done"' in body
    assert f'data-copy="cairn edit {a.name}"' in body


def test_a_doc_rendered_as_markdown(store):
    a = _plan(store)
    b = _plan(store, "colas")
    text = docs.render(store, a).replace("- [ ]", f"- [ ] seguir con `{b.name}`\n- [x] hecho") + (
        f"\n| a | b |\n|---|---|\n| [ver](../_wip/{b.name}.md) | x |\n\n<b>crudo</b>\n"
        "\n```python\ndef f():\n    return 1\n```\n\n```mermaid\nflowchart LR\n  A --> B\n```\n")
    edit.apply(store, a.id, text=text)
    _, body = web.route(store, f"/doc/{a.name}")
    assert '<details class="fm">' in body and '<div class="table-wrap"><table>' in body
    assert f'<code><a href="/doc/{b.name}">{b.name}</a></code>' in body     # a name in `code`
    assert f'<a href="/doc/{b.name}">ver</a>' in body                       # a tree path
    assert '<li class="task"><span class="box"></span>seguir' in body
    assert '<li class="task done"><span class="box on"></span>hecho' in body
    assert '<h2 id="L11">TL;DR<a class="anchor" href="#L11">#</a></h2>' in body
    assert "<b>crudo</b>" not in body and "&lt;b&gt;crudo&lt;/b&gt;" in body
    assert "<!--" not in _main(body)                                       # template comments
    assert '<span class="lang">python</span>' in body and '<span class="k">def</span>' in body
    assert '<pre class="mermaid">flowchart LR\n  A --&gt; B\n</pre>' in body
    assert f'href="/doc/{a.name}?view=text">text</a>' in body


def test_without_markdown_it_it_stays_as_text(store, monkeypatch):
    a = _plan(store)
    monkeypatch.setattr(web, "MarkdownIt", None)
    _, body = web.route(store, f"/doc/{a.name}")
    assert '<pre class="text">' in body and "?view=text" not in body


def test_links_both_ways(store):
    plan = _plan(store)
    h = docs.handoff_new(store, "api", "seguir", to=["api"], plans=[plan.name], repo="api").doc
    _, body = web.route(store, f"/doc/{plan.name}")
    assert f'<span class="k">plan (from)</span><a href="/doc/{h.name}">' in body
    _, body = web.route(store, f"/doc/{h.name}")
    assert f'<span class="k">plan</span><a href="/doc/{plan.name}">' in body


def test_history_and_a_revision(store):
    a = _plan(store)
    edit.apply(store, a.id, set_={"note": "baja prioridad"})
    status, body = web.route(store, f"/doc/{a.name}/log")
    assert status == 200 and "2 revisions" in body
    assert '<span class="add">+note: baja prioridad</span>' in body
    assert body.count("<details") == 2 and body.count('<details class="card rev op-set_state" open>') == 1
    _, old = web.route(store, f"/doc/{a.name}@~1")
    assert "Revision <code>" in old and "baja prioridad" not in old


def test_activity_by_day(store):
    a = _plan(store)
    edit.apply(store, a.id, set_={"note": "x"})
    status, body = web.route(store, "/activity")
    assert status == 200 and '<li class="day">' in body
    assert body.index(">state changed<") < body.index(">created<")                # newest first
    assert web.route(store, "/activity?n=0")[0] == 404


def test_everything_from_the_docs_is_escaped(store):
    a = _plan(store)
    edit.apply(store, a.id, text=docs.render(store, a) + "\n<script>alert(1)</script> & co\n")
    _, body = web.route(store, f"/doc/{a.name}")
    assert "<script>alert" not in body and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in body
    _, found = web.route(store, "/search?q=alert")
    assert "<script>alert" not in found and "<mark>alert</mark>" in found


def test_weekly_search_and_not_found(store):
    a = _plan(store)
    docs.close(store, a.name, force=True)
    status, body = web.route(store, "/weekly?days=30")
    assert status == 200 and a.name in body and "since 2026-02-08" in body
    assert web.route(store, "/weekly?days=x")[0] == 404
    _, found = web.route(store, "/search?q=cache")
    assert a.name in found
    assert web.route(store, "/doc/no_existe")[0] == 404
    assert web.route(store, "/nada")[0] == 404


def test_the_server_answers_on_localhost(cfg, store):
    a = _plan(store)
    server = web.make_server(0, lambda: Store(config.load()))
    assert server.server_address[0] == "127.0.0.1"
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"{base}/doc/{a.name}") as r:
            assert r.status == 200 and "text/html" in r.headers["Content-Type"]
            assert a.name in r.read().decode()
        for path, ctype in (("/static/style.css", "text/css"), ("/static/app.js", "text/javascript")):
            with urllib.request.urlopen(base + path) as r:
                assert ctype in r.headers["Content-Type"] and len(r.read()) > 1000
        for path in ("/doc/no_existe", "/static/..%2Fweb.py", "/static/nada.js"):
            try:
                urllib.request.urlopen(base + path)
                raise AssertionError(f"expected a 404 for {path}")
            except urllib.error.HTTPError as e:
                assert e.code == 404
        with urllib.request.urlopen(f"{base}/api/state") as r:
            before = json.loads(r.read())["events"]
        # a doc written after the server started shows up without restarting it
        b = _plan(store, "nuevo")
        with urllib.request.urlopen(f"{base}/api/state") as r:
            assert json.loads(r.read())["events"] == before + 1
        with urllib.request.urlopen(f"{base}/docs") as r:
            assert b.name in r.read().decode()
    finally:
        server.shutdown()
        server.server_close()
