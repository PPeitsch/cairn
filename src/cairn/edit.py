"""Editar un doc: el cuerpo y el front-matter, como un evento `edit` (o `set_state` si sólo
cambian `estado`/`nota`).

Se edita el doc como archivo (`show` → editor → de vuelta), así que el front-matter se puede
tocar a mano igual que en wip-doc, con dos límites:

- los vínculos (`origen`, `referencias`, `plan`) no se editan: los manejan `promote` y
  `handoff new`, y en el archivo son rutas calculadas;
- `estado` no puede pasar de abierto a cerrado ni al revés: eso es `close` / `reopen` /
  `handoff take|drop`, que tocan otros campos a la vez.

Una referencia cuyo cuerpo cambia se marca `actualizado: <hoy>` sola.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from cairn import clock, docs
from cairn.docs import LINK_KINDS, TIPOS_DOC, Doc, Result
from cairn.markdown import parse_front_matter
from cairn.projection import ABIERTOS, CERRADOS
from cairn.store import CairnError, Store

STATE_KEYS = {"estado", "nota"}


def _grupo(estado: str) -> str:
    return "cerrado" if estado in CERRADOS else "abierto" if estado in ABIERTOS else "?"


def changes(doc: Doc, new_meta: dict | None, new_body: str | None) -> dict:
    """El payload del evento (vacío si no cambia nada). Los vínculos no entran: ver `apply`."""
    payload: dict = {}
    if new_body is not None and new_body != doc.body:
        payload["body"] = new_body
    if new_meta is not None:
        cur = {k: v for k, v in doc.meta.items() if k not in LINK_KINDS}
        nueva = {k: v for k, v in new_meta.items() if k not in LINK_KINDS and v not in ("", [], None)}
        set_ = {k: v for k, v in nueva.items() if cur.get(k) != v}
        unset = sorted(k for k in cur if k not in nueva)
        if set_:
            payload["set"] = set_
        if unset:
            payload["unset"] = unset
    set_, unset = payload.get("set", {}), payload.get("unset", [])
    if "tipo" in unset or ("tipo" in set_ and set_["tipo"] not in TIPOS_DOC):
        raise CairnError(f"`tipo` tiene que ser uno de: {', '.join(TIPOS_DOC)}")
    if "tipo" in set_ and (set_["tipo"] == "ref") != (doc.tipo == "ref"):
        raise CairnError("Un doc no pasa a ser referencia (ni deja de serlo) editando el tipo: "
                         "para eso está `promote`.")
    if "estado" in unset and doc.tipo != "ref":
        raise CairnError("`estado` no se puede sacar")
    if "estado" in set_:
        if _grupo(set_["estado"]) == "?":
            raise CairnError(f"Estado desconocido: {set_['estado']} (abiertos: "
                             f"{', '.join(sorted(ABIERTOS))}; cerrados: {', '.join(sorted(CERRADOS))})")
        if _grupo(set_["estado"]) != _grupo(doc.estado):
            raise CairnError(f"`estado` no pasa de {_grupo(doc.estado)} a {_grupo(set_['estado'])} "
                             "editando: usar close / reopen / handoff take|drop.")
    if "body" in payload and doc.tipo == "ref" and "actualizado" not in set_:
        payload.setdefault("set", {})["actualizado"] = clock.today()
    return payload


def _op(payload: dict) -> str:
    keys = set(payload.get("set", {})) | set(payload.get("unset", []))
    return "set_state" if "body" not in payload and keys and keys <= STATE_KEYS else "edit"


def apply(store: Store, ref: str, text: str | None = None, set_: dict | None = None,
          unset: list[str] | None = None, expected_head: str | None = None) -> Result:
    """Aplica una edición. `text`: el doc entero (con o sin front-matter; sin front-matter sólo
    cambia el cuerpo). `set_`/`unset`: campos sueltos. `expected_head`: si el doc cambió desde
    ahí (otra edición en el medio), corta en vez de pisarla."""
    with store.writer() as w:
        doc = docs.find(store, ref)
        if expected_head and doc.head != expected_head:
            raise CairnError(f"{doc.name} cambió mientras lo editabas: no se guardó nada.")
        meta, body = (None, None)
        warnings: list[str] = []
        if text is not None:
            meta, body = parse_front_matter(text)
            if meta is None:
                body = text
            elif docs.link_paths(store, doc) != {k: _lista(meta.get(k)) for k in LINK_KINDS}:
                warnings.append("[nota] Los vínculos (origen, referencias, plan) no se editan a mano: "
                                "se ignoró el cambio.")
        if set_ or unset:
            meta = dict(meta if meta is not None else doc.meta)
            for k in unset or []:
                meta.pop(k, None)
            meta.update(set_ or {})
        payload = changes(doc, meta, body)
        if payload:
            w.emit(_op(payload), doc.id, payload)
    return Result(docs.get(store, doc.id), warnings, {"changed": bool(payload)})


def _lista(v) -> list[str]:
    if not v:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def parse_assignments(items: list[str]) -> dict:
    out = {}
    for it in items:
        if "=" not in it:
            raise CairnError(f"--set espera clave=valor: {it!r}")
        k, v = it.split("=", 1)
        if not k.strip():
            raise CairnError(f"--set sin clave: {it!r}")
        out[k.strip()] = v
    return out


def editor_command() -> list[str]:
    return shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")


def interactive(store: Store, ref: str) -> Result:
    """Abre el doc en el editor y guarda lo que cambió. El editor corre sin el lock tomado: si
    mientras tanto otro proceso tocó el doc, no se pisa (y el texto queda en el temporal)."""
    doc = docs.find(store, ref)
    original = docs.render(store, doc)
    tmpdir = Path(tempfile.mkdtemp(prefix="cairn-edit-"))
    path = tmpdir / f"{doc.name}.md"
    path.write_text(original, encoding="utf-8")
    try:
        r = subprocess.run([*editor_command(), str(path)])
    except OSError as e:
        raise CairnError(f"No se pudo abrir el editor: {e}") from None
    if r.returncode != 0:
        raise CairnError(f"El editor salió con {r.returncode}: no se guardó nada (texto en {path})")
    text = path.read_text(encoding="utf-8")
    if text == original:
        path.unlink()
        tmpdir.rmdir()
        return Result(doc, [], {"changed": False})
    try:
        result = apply(store, doc.id, text=text, expected_head=doc.head)
    except CairnError as e:
        raise CairnError(f"{e}\n  Tu versión quedó en {path}") from None
    path.unlink()
    tmpdir.rmdir()
    return result
