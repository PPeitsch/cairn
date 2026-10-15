"""El servidor MCP contra un nodo aislado, con el cliente en proceso del SDK."""

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


def test_tools_e_instrucciones(node):
    async def go():
        async with Client(mcp_server.build_server()) as c:
            return c.instructions, {t.name: t for t in (await c.list_tools()).tools}
    instr, tools = asyncio.run(go())
    assert "Cuándo documentar" in instr and "handoff" in instr
    assert {"cairn_new", "cairn_edit", "cairn_close", "cairn_handoff_take", "cairn_search"} <= set(tools)
    assert tools["cairn_list"].annotations.read_only_hint is True
    assert tools["cairn_close"].annotations.read_only_hint is False
    assert "pattern" in tools["cairn_search"].input_schema["required"]


def test_ciclo_de_vida(node):
    text = ok("cairn_new", {"area": "demo", "tema": "lento", "tipo": "bug"})
    assert "Creado: 2026-03-10_demo_BUG_lento" in text
    assert "repo: demo" in text                       # sale del directorio de la conversación

    assert "Guardado" in ok("cairn_edit", {"doc": "2026-03-10_demo_BUG_lento", "old_text": "## TL;DR",
                                           "new_text": "## TL;DR\n\nSe arregló.\n\n- [ ] confirmar"})
    assert "Se arregló." in ok("cairn_show", {"doc": "2026-03-10_demo_BUG_lento"})

    err, text = call("cairn_close", {"doc": "2026-03-10_demo_BUG_lento"})
    assert err and "pendiente(s) sin tildar" in text
    assert "Cerrado" in ok("cairn_close", {"doc": "2026-03-10_demo_BUG_lento", "force": True})
    assert "BUG_lento" in ok("cairn_list", {"scope": "done"})
    listado = ok("cairn_list", {"scope": "done"})
    assert listado.startswith("1 doc(s) en 1 repo(s)") and "demo (1)" in listado
    assert "modificado 2026-03-10, hoy" in listado
    plano = ok("cairn_list", {"scope": "done", "por_repo": False})
    assert plano.startswith("2026-03-10_demo_BUG_lento  [bug · cerrado") and "modificado" in plano
    assert "Sin docs en wip" in ok("cairn_list")
    assert "Reabierto" in ok("cairn_reopen", {"doc": "2026-03-10_demo_BUG_lento", "motivo": "volvió"})
    r = ok("cairn_close", {"doc": "2026-03-10_demo_BUG_lento", "descartar": "no se reproduce"})
    assert "(descartado)" in r
    assert "[bug · descartado 2026-03-10" in ok("cairn_weekly")


def test_new_con_cuerpo_y_busqueda(node):
    body = "---\n\n# Raro\n\n## TL;DR\n\nUn cuerpo que empieza con una regla horizontal.\n"
    ok("cairn_new", {"area": "demo", "tema": "raro", "body": body})
    shown = ok("cairn_show", {"doc": "2026-03-10_demo_raro"})
    assert shown.endswith(body)
    assert "regla horizontal" in ok("cairn_search", {"pattern": "regla"})
    fts = ok("cairn_search", {"pattern": "horizontal", "fts": True})
    assert "2026-03-10_demo_raro" in fts and "«horizontal»" in fts and fts.startswith("Búsqueda FTS")
    assert "sin coincidencias" in ok("cairn_search", {"pattern": "zzz"})


def test_edit_old_text_ambiguo_o_ausente(node):
    ok("cairn_new", {"area": "demo", "tema": "x", "body": "# X\n\nuno uno\n"})
    err, text = call("cairn_edit", {"doc": "2026-03-10_demo_x", "old_text": "uno", "new_text": "dos"})
    assert err and "2 veces" in text
    err, text = call("cairn_edit", {"doc": "2026-03-10_demo_x", "old_text": "tres", "new_text": "dos"})
    assert err and "0 veces" in text
    err, text = call("cairn_edit", {"doc": "2026-03-10_demo_x", "old_text": "uno"})
    assert err and "van juntos" in text


def test_edit_no_cruza_estados(node):
    ok("cairn_new", {"area": "demo", "tema": "x"})
    err, text = call("cairn_edit", {"doc": "2026-03-10_demo_x", "set_fields": {"estado": "resuelto"}})
    assert err and "close / reopen" in text
    assert "Guardado" in ok("cairn_edit", {"doc": "2026-03-10_demo_x", "set_fields": {"nota": "ojo"}})


def test_errores_son_de_la_tool(node):
    err, text = call("cairn_show", {"doc": "no_existe"})
    assert err and "No encontrado: no_existe" in text


def test_sin_init(env, monkeypatch):
    err, text = call("cairn_list")
    assert err and "cairn init" in text


def test_handoffs(node, cfg):
    ok("cairn_new", {"area": "demo", "tema": "plan_x", "tipo": "plan"})
    text = ok("cairn_handoff_new", {"area": "demo", "tema": "seguir", "plan": ["2026-03-10_demo_plan_x"],
                                    "body": "# Seguir\n\n## TL;DR\n\nSeguir con el plan.\n"})
    assert "Handoff creado: 2026-03-10_demo_HANDOFF_seguir" in text
    assert "plan:\n  - ../_wip/2026-03-10_demo_plan_x.md" in text
    assert "HANDOFF_seguir" in ok("cairn_handoff_list")
    assert "Sin handoffs pendientes para otro" in ok("cairn_handoff_list", {"repo": "otro"})
    taken = ok("cairn_handoff_take")
    assert "Handoff tomado" in taken and "Seguir con el plan." in taken
    err, text = call("cairn_handoff_take", {"nota": "2026-03-10_demo_HANDOFF_seguir"})
    assert err and "ya no está pendiente" in text
    with Store(cfg) as store:
        assert docs.find(store, "2026-03-10_demo_HANDOFF_seguir").estado == "usada"


def test_promote_log_diff_weekly_check(node):
    body = ("# Bug\n\n## TL;DR\n\nSe arregló el bug.\n\n## Contexto / topología\n\nUn server.\n\n"
            "## Comandos útiles de referencia\n\n```\nls\n```\n")
    ok("cairn_new", {"area": "demo", "tema": "b", "tipo": "bug", "body": body})
    ok("cairn_close", {"doc": "2026-03-10_demo_BUG_b"})
    text = ok("cairn_promote", {"doc": "2026-03-10_demo_BUG_b", "area": "demo", "tema": "server"})
    assert text.startswith("demo_server") and "Contexto / topología" in text
    log = ok("cairn_log", {"doc": "2026-03-10_demo_BUG_b"})
    assert "close" in log and "create" in log
    assert "+## TL;DR" not in ok("cairn_diff", {"doc": "2026-03-10_demo_BUG_b"})
    assert "Se arregló el bug." in ok("cairn_weekly")
    assert "Todo en orden" in ok("cairn_check")


def test_lectura_acotada(node, monkeypatch):
    largo = "## TL;DR\n\nResumen.\n\n## Detalle\n\n" + "linea de relleno con costo\n" * 400
    ok("cairn_new", {"area": "demo", "tema": "largo", "tipo": "plan", "body": "# demo largo\n\n" + largo})
    idx = ok("cairn_show", {"doc": "2026-03-10_demo_largo"})
    assert "esto es el índice, no el doc" in idx and "Resumen." in idx and "## Detalle  (líneas" in idx
    assert idx.count("relleno") == 0
    assert ok("cairn_show", {"doc": "2026-03-10_demo_largo", "completo": True}).count("relleno") == 400
    sec = ok("cairn_show", {"doc": "2026-03-10_demo_largo", "seccion": "Detalle"})
    assert sec.startswith("(sección «Detalle»") and sec.count("relleno") == 400
    assert ok("cairn_show", {"doc": "2026-03-10_demo_largo", "lineas": "1-3"}).startswith("(líneas 1-3")
    err, text = call("cairn_show", {"doc": "2026-03-10_demo_largo", "seccion": "nada"})
    assert err and "Secciones:" in text

    # la regex con tope: docs con más coincidencias primero, el resto por nombre
    for i in range(3):
        ok("cairn_new", {"area": "demo", "tema": f"chico{i}", "body": f"# chico {i}\n\ncosto\n"})
    from cairn import config
    config.update(lambda raw: raw.setdefault("mcp", {}).update(
        {"search_max_docs": "2", "search_max_lines": "2"}))
    out = ok("cairn_search", {"pattern": "costo"})
    assert out.startswith('Búsqueda "costo": 4 doc(s)') and "se muestran los 2 con más coincidencias" in out
    assert out.splitlines()[1].startswith("2026-03-10_demo_largo") and "(+398 más)" in out
    assert "Otros 2 doc(s) con menos coincidencias:" in out and "fts=true" in out
