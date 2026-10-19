"""Reading a doc in parts: a line range, a section, and the outline of a long doc."""

import pytest

from cairn import reading
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


def test_sections():
    s = {x.title: x for x in reading.sections(DOC)}
    assert list(s) == ["demo — un plan", "TL;DR", "Contexto", "Trampas", "Decisiones tomadas"]
    assert (s["Contexto"].start, s["Contexto"].end) == (12, 21)       # includes Trampas
    assert (s["Trampas"].start, s["Trampas"].end) == (16, 21)


def test_line_range_and_section():
    assert reading.line_range(DOC, "12-13") == "(líneas 12-13 de 24)\n  12| ## Contexto\n  13| "
    assert reading.line_range(DOC, "23-").endswith("  24| - una")
    for bad in ("x", "5-3", "0", "99"):
        with pytest.raises(CairnError):
            reading.line_range(DOC, bad)
    out = reading.find_section(DOC, "decisiones")
    assert out.startswith("(sección «Decisiones tomadas», líneas 22-24 de 24)") and "- una" in out
    assert "«Trampas»" in reading.find_section(DOC, "## tramp")
    assert "«Trampas»" in reading.find_section(DOC, "ampas")   # contains, if not a prefix
    with pytest.raises(CairnError, match="Secciones: .*«Decisiones tomadas»"):
        reading.find_section(DOC, "no existe")


def test_view_short_long_and_full():
    assert reading.view(DOC) == DOC
    idx = reading.view(DOC, max_chars=50)
    assert idx.startswith("---\ntipo: plan") and "# demo — un plan" in idx and "Se hizo algo." in idx
    assert "esto es el índice, no el doc" in idx
    assert "  ## Contexto  (líneas 12-21," in idx and "    ### Trampas  (líneas 16-21," in idx
    assert "texto de contexto" not in idx
    assert reading.view(DOC, full=True, max_chars=50) == DOC
    assert "- una" in reading.view(DOC, section="Decis", max_chars=50)
    with pytest.raises(CairnError, match="no las dos"):
        reading.view(DOC, section="TL", lines="1-2")
