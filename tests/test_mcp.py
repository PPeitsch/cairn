"""The MCP server against an isolated node, with the SDK's in-process client."""

import asyncio

import pytest
from mcp import Client

from cairn import docs, mcp_server
from cairn.store import Store


def call(tool: str, args: dict | None = None):
    async def go():
        async with Client(mcp_server.build_server()) as c:
            r = await c.call_tool(tool, args or {})
            return r.is_error, "\n".join(x.text for x in r.content)
    return asyncio.run(go())


def ok(tool: str, args: dict | None = None) -> str:
    err, text = call(tool, args)
    assert not err, text
    return text


@pytest.fixture
def node(cfg, monkeypatch, env):
    repo = env / "projects" / "demo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    return cfg


def test_tools_and_instructions(node):
    async def go():
        async with Client(mcp_server.build_server()) as c:
            return c.instructions, {t.name: t for t in (await c.list_tools()).tools}
    instr, tools = asyncio.run(go())
    assert "When to document" in instr and "handoff" in instr
    assert {"cairn_new", "cairn_edit", "cairn_close", "cairn_handoff_take", "cairn_search"} <= set(tools)
    assert tools["cairn_list"].annotations.read_only_hint is True
    assert tools["cairn_close"].annotations.read_only_hint is False
    assert "pattern" in tools["cairn_search"].input_schema["required"]


def test_lifecycle(node):
    text = ok("cairn_new", {"area": "demo", "topic": "lento", "type": "bug"})
    assert "Created: 2026-03-10_demo_BUG_lento" in text
    assert "repo: demo" in text                       # comes from the conversation's directory

    assert "Saved" in ok("cairn_edit", {"doc": "2026-03-10_demo_BUG_lento", "old_text": "## TL;DR",
                                           "new_text": "## TL;DR\n\nSe arregló.\n\n- [ ] confirmar"})
    assert "Se arregló." in ok("cairn_show", {"doc": "2026-03-10_demo_BUG_lento"})

    err, text = call("cairn_close", {"doc": "2026-03-10_demo_BUG_lento"})
    assert err and "unchecked pending item(s)" in text
    assert "Closed" in ok("cairn_close", {"doc": "2026-03-10_demo_BUG_lento", "force": True})
    assert "BUG_lento" in ok("cairn_list", {"scope": "done"})
    listado = ok("cairn_list", {"scope": "done"})
    assert listado.startswith("1 doc(s) in 1 repo(s)") and "demo (1)" in listado
    assert "modified 2026-03-10, today" in listado
    plano = ok("cairn_list", {"scope": "done", "by_repo": False})
    assert plano.startswith("2026-03-10_demo_BUG_lento  [bug · closed") and "modified" in plano
    assert "No docs in wip" in ok("cairn_list")
    assert "Reopened" in ok("cairn_reopen", {"doc": "2026-03-10_demo_BUG_lento", "reason": "volvió"})
    r = ok("cairn_close", {"doc": "2026-03-10_demo_BUG_lento", "discard": "no se reproduce"})
    assert "(discarded)" in r
    assert "[bug · discarded 2026-03-10" in ok("cairn_weekly")


def test_new_with_body_and_search(node):
    body = "---\n\n# Raro\n\n## TL;DR\n\nUn cuerpo que empieza con una regla horizontal.\n"
    ok("cairn_new", {"area": "demo", "topic": "raro", "body": body})
    shown = ok("cairn_show", {"doc": "2026-03-10_demo_raro"})
    assert shown.endswith(body)
    assert "regla horizontal" in ok("cairn_search", {"pattern": "regla"})
    fts = ok("cairn_search", {"pattern": "horizontal", "fts": True})
    assert "2026-03-10_demo_raro" in fts and "«horizontal»" in fts and fts.startswith("FTS search")
    assert "no matches" in ok("cairn_search", {"pattern": "zzz"})


def test_edit_old_text_ambiguous_or_missing(node):
    ok("cairn_new", {"area": "demo", "topic": "x", "body": "# X\n\nuno uno\n"})
    err, text = call("cairn_edit", {"doc": "2026-03-10_demo_x", "old_text": "uno", "new_text": "dos"})
    assert err and "2 times" in text
    err, text = call("cairn_edit", {"doc": "2026-03-10_demo_x", "old_text": "tres", "new_text": "dos"})
    assert err and "0 times" in text
    err, text = call("cairn_edit", {"doc": "2026-03-10_demo_x", "old_text": "uno"})
    assert err and "go together" in text


def test_edit_does_not_cross_status_groups(node):
    ok("cairn_new", {"area": "demo", "topic": "x"})
    err, text = call("cairn_edit", {"doc": "2026-03-10_demo_x", "set_fields": {"status": "resolved"}})
    assert err and "close / reopen" in text
    assert "Saved" in ok("cairn_edit", {"doc": "2026-03-10_demo_x", "set_fields": {"note": "ojo"}})


def test_errors_are_tool_errors(node):
    err, text = call("cairn_show", {"doc": "no_existe"})
    assert err and "Not found: no_existe" in text


def test_without_init(env, monkeypatch):
    err, text = call("cairn_list")
    assert err and "cairn init" in text


def test_handoffs(node, cfg):
    ok("cairn_new", {"area": "demo", "topic": "plan_x", "type": "plan"})
    text = ok("cairn_handoff_new", {"area": "demo", "topic": "seguir", "plan": ["2026-03-10_demo_plan_x"],
                                    "body": "# Seguir\n\n## TL;DR\n\nSeguir con el plan.\n"})
    assert "Handoff created: 2026-03-10_demo_HANDOFF_seguir" in text
    assert "plan:\n  - ../_wip/2026-03-10_demo_plan_x.md" in text
    assert "HANDOFF_seguir" in ok("cairn_handoff_list")
    assert "No pending handoffs for otro" in ok("cairn_handoff_list", {"repo": "otro"})
    taken = ok("cairn_handoff_take")
    assert "Handoff taken" in taken and "Seguir con el plan." in taken
    err, text = call("cairn_handoff_take", {"note": "2026-03-10_demo_HANDOFF_seguir"})
    assert err and "is no longer pending" in text
    with Store(cfg) as store:
        assert docs.find(store, "2026-03-10_demo_HANDOFF_seguir").status == "taken"


def test_promote_log_diff_weekly_check(node):
    body = ("# Bug\n\n## TL;DR\n\nSe arregló el bug.\n\n## Contexto / topología\n\nUn server.\n\n"
            "## Comandos útiles de referencia\n\n```\nls\n```\n")
    ok("cairn_new", {"area": "demo", "topic": "b", "type": "bug", "body": body})
    ok("cairn_close", {"doc": "2026-03-10_demo_BUG_b"})
    text = ok("cairn_promote", {"doc": "2026-03-10_demo_BUG_b", "area": "demo", "topic": "server"})
    assert text.startswith("demo_server") and "Contexto / topología" in text   # Spanish docs too
    log = ok("cairn_log", {"doc": "2026-03-10_demo_BUG_b"})
    assert "close" in log and "create" in log
    assert "+## TL;DR" not in ok("cairn_diff", {"doc": "2026-03-10_demo_BUG_b"})
    assert "Se arregló el bug." in ok("cairn_weekly")
    assert "All in order" in ok("cairn_check")


def test_bounded_reading(node, monkeypatch):
    largo = "## TL;DR\n\nResumen.\n\n## Detalle\n\n" + "linea de relleno con costo\n" * 400
    ok("cairn_new", {"area": "demo", "topic": "largo", "type": "plan", "body": "# demo largo\n\n" + largo})
    idx = ok("cairn_show", {"doc": "2026-03-10_demo_largo"})
    assert "this is the outline, not the doc" in idx and "Resumen." in idx and "## Detalle  (lines" in idx
    assert idx.count("relleno") == 0
    assert ok("cairn_show", {"doc": "2026-03-10_demo_largo", "full": True}).count("relleno") == 400
    sec = ok("cairn_show", {"doc": "2026-03-10_demo_largo", "section": "Detalle"})
    assert sec.startswith("(section «Detalle»") and sec.count("relleno") == 400
    assert ok("cairn_show", {"doc": "2026-03-10_demo_largo", "lines": "1-3"}).startswith("(lines 1-3")
    err, text = call("cairn_show", {"doc": "2026-03-10_demo_largo", "section": "nada"})
    assert err and "Sections:" in text

    # capped regex: docs with the most hits first, the rest by name
    for i in range(3):
        ok("cairn_new", {"area": "demo", "topic": f"chico{i}", "body": f"# chico {i}\n\ncosto\n"})
    from cairn import config
    config.update(lambda raw: raw.setdefault("mcp", {}).update(
        {"search_max_docs": "2", "search_max_lines": "2"}))
    out = ok("cairn_search", {"pattern": "costo"})
    assert out.startswith('Search "costo": 4 doc(s)') and "showing the 2 with the most matches" in out
    assert out.splitlines()[1].startswith("2026-03-10_demo_largo") and "(+398 more)" in out
    assert "Another 2 doc(s) with fewer matches:" in out and "fts=true" in out
