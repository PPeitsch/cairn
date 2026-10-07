"""Leer un doc por partes: rango de líneas, sección e índice de un doc largo."""

import pytest

from cairn import lectura
from cairn.store import CairnError

DOC = """---
tipo: plan
estado: abierto
---

# demo — un plan

## TL;DR

Se hizo algo.

## Contexto

texto de contexto

### Trampas

```
## esto no es un título
```

## Decisiones tomadas

- una
"""


def test_secciones():
    s = {x.titulo: x for x in lectura.secciones(DOC)}
    assert list(s) == ["demo — un plan", "TL;DR", "Contexto", "Trampas", "Decisiones tomadas"]
    assert (s["Contexto"].desde, s["Contexto"].hasta) == (12, 21)       # incluye Trampas
    assert (s["Trampas"].desde, s["Trampas"].hasta) == (16, 21)


def test_rango_y_seccion():
    assert lectura.rango(DOC, "12-13") == "(líneas 12-13 de 24)\n  12| ## Contexto\n  13| "
    assert lectura.rango(DOC, "23-").endswith("  24| - una")
    for bad in ("x", "5-3", "0", "99"):
        with pytest.raises(CairnError):
            lectura.rango(DOC, bad)
    out = lectura.seccion(DOC, "decisiones")
    assert out.startswith("(sección «Decisiones tomadas», líneas 22-24 de 24)") and "- una" in out
    assert "«Trampas»" in lectura.seccion(DOC, "## tramp")
    assert "«Trampas»" in lectura.seccion(DOC, "ampas")                 # contiene, si no empieza
    with pytest.raises(CairnError, match="Secciones: .*«Decisiones tomadas»"):
        lectura.seccion(DOC, "no existe")


def test_vista_corta_larga_y_completa():
    assert lectura.vista(DOC) == DOC
    idx = lectura.vista(DOC, max_chars=50)
    assert idx.startswith("---\ntipo: plan") and "# demo — un plan" in idx and "Se hizo algo." in idx
    assert "esto es el índice, no el doc" in idx
    assert "  ## Contexto  (líneas 12-21," in idx and "    ### Trampas  (líneas 16-21," in idx
    assert "texto de contexto" not in idx
    assert lectura.vista(DOC, completo=True, max_chars=50) == DOC
    assert "- una" in lectura.vista(DOC, seccion_="Decis", max_chars=50)
    with pytest.raises(CairnError, match="no las dos"):
        lectura.vista(DOC, seccion_="TL", lineas="1-2")
