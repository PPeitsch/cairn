"""`cairn serve`: the views as HTML (without a server) and the server itself on a free port."""

import threading
import urllib.error
import urllib.request

from cairn import config, docs, edit, web
from cairn.store import Store

HOY = "2026-03-10"


def _plan(store, topic="cache", repo="api"):
    return docs.new(store, "api", topic, "plan", repo).doc


def test_index_groups_by_repo_and_filters(store):
    a = _plan(store, "cache", "api")
    b = _plan(store, "colas", "web")
    docs.handoff_new(store, "api", "seguir", to=["api"], plans=[a.name], repo="api")
    status, body = web.route(store, "/")
    assert status == 200
    assert body.index("pending handoffs") < body.index("<h2>api (1)</h2>")
    assert a.name in body and b.name in body and "to: api" in body
    _, only_web = web.route(store, "/?repo=web")
    assert b.name in only_web and a.name not in only_web
    _, refs = web.route(store, "/?scope=ref")
    assert "Nothing here." in refs
    assert web.route(store, "/?scope=nope")[0] == 404


def test_a_doc_as_text_with_outline_links_and_checklist(store):
    a = _plan(store)
    b = _plan(store, "colas")
    text = docs.render(store, a).replace("- [ ]", f"- [ ] seguir con `{b.name}`\n- [x] hecho")
    edit.apply(store, a.id, text=text)
    status, body = web.route(store, f"/doc/{a.name}")
    assert status == 200
    assert f'<a href="/doc/{b.name}">{b.name}</a>' in body        # another doc, linked
    assert f'href="/doc/{a.name}"' not in body.split("<main>")[1].split("history")[1]  # not itself
    assert 'class="todo"' in body and 'class="done"' in body
    assert '<span class="h">## TL;DR</span>' in body
    assert '>TL;DR</a>' in body                                    # in the outline
    assert f'href="/doc/{a.name}/log"' in body


def test_links_both_ways(store):
    plan = _plan(store)
    h = docs.handoff_new(store, "api", "seguir", to=["api"], plans=[plan.name], repo="api").doc
    _, body = web.route(store, f"/doc/{plan.name}")
    assert f'<a href="/doc/{h.name}">{h.name}</a> → plan' in body
    _, body = web.route(store, f"/doc/{h.name}")
    assert f'plan → <a href="/doc/{plan.name}">{plan.name}</a>' in body


def test_history_and_a_revision(store):
    a = _plan(store)
    edit.apply(store, a.id, set_={"note": "baja prioridad"})
    status, body = web.route(store, f"/doc/{a.name}/log")
    assert status == 200 and "(2 revisions)" in body
    assert '<span class="add">+note: baja prioridad</span>' in body
    _, old = web.route(store, f"/doc/{a.name}@~1")
    assert "Revision <code>" in old and "baja prioridad" not in old


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
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/doc/{a.name}") as r:
            assert r.status == 200 and "text/html" in r.headers["Content-Type"]
            assert a.name in r.read().decode()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/doc/no_existe")
            raise AssertionError("expected a 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        # a doc written after the server started shows up without restarting it
        b = _plan(store, "nuevo")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            assert b.name in r.read().decode()
    finally:
        server.shutdown()
        server.server_close()
