"""Leer un doc por partes, para no gastar el contexto de una conversación en docs enteros.

Todo trabaja sobre el doc como archivo (front-matter incluido), así los números de línea son los
mismos que da la búsqueda:

- `lineas="40-80"` (o `"40"`, `"40-"`): ese rango, numerado;
- `seccion="Decisiones"`: la sección cuyo título empieza con (o contiene) eso, hasta la próxima
  de su nivel o más alto, numerada;
- un doc más largo que `max_chars`, sin pedir nada de lo anterior ni `completo`: el índice
  (front-matter, título, TL;DR y las secciones con sus líneas y tamaño) en vez del doc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cairn.store import CairnError

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Seccion:
    nivel: int
    titulo: str
    desde: int          # líneas, desde 1 e inclusive
    hasta: int
    chars: int


def _inicio_cuerpo(lines: list[str]) -> int:
    """Índice de la primera línea después del front-matter (0 si no tiene)."""
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return i + 1
    return 0


def secciones(text: str) -> list[Seccion]:
    lines = text.splitlines()
    heads, fence = [], False
    for i in range(_inicio_cuerpo(lines), len(lines)):
        if FENCE.match(lines[i]):
            fence = not fence
        elif not fence and (m := HEADING.match(lines[i])):
            heads.append((i, len(m.group(1)), m.group(2)))
    out = []
    for k, (i, nivel, titulo) in enumerate(heads):
        fin = next((j for j, n, _ in heads[k + 1:] if n <= nivel), len(lines))
        out.append(Seccion(nivel, titulo, i + 1, fin, sum(len(x) + 1 for x in lines[i:fin])))
    return out


def _numeradas(lines: list[str], desde: int) -> str:
    return "\n".join(f"{n:>4}| {line}" for n, line in enumerate(lines, desde))


def rango(text: str, spec: str) -> str:
    lines = text.splitlines()
    m = re.fullmatch(r"\s*(\d+)\s*(?:(-)\s*(\d+)?)?\s*", spec)
    if not m:
        raise CairnError(f"lineas: se espera `A-B`, `A-` o `A` (p. ej. `40-80`), no {spec!r}")
    a = int(m.group(1))
    b = int(m.group(3)) if m.group(3) else (len(lines) if m.group(2) else a)
    if a < 1 or b < a or a > len(lines):
        raise CairnError(f"lineas {spec}: el doc tiene {len(lines)} línea(s)")
    b = min(b, len(lines))
    return f"(líneas {a}-{b} de {len(lines)})\n" + _numeradas(lines[a - 1:b], a)


def seccion(text: str, nombre: str) -> str:
    todas = secciones(text)
    buscado = nombre.strip().lstrip("#").strip().lower()
    elegida = (next((s for s in todas if s.titulo.lower().startswith(buscado)), None)
               or next((s for s in todas if buscado in s.titulo.lower()), None))
    if elegida is None:
        disponibles = ", ".join(f"«{s.titulo}»" for s in todas if s.nivel <= 3) or "(ninguna)"
        raise CairnError(f"No hay una sección «{nombre}». Secciones: {disponibles}")
    lines = text.splitlines()
    return (f"(sección «{elegida.titulo}», líneas {elegida.desde}-{elegida.hasta} de {len(lines)})\n"
            + _numeradas(lines[elegida.desde - 1:elegida.hasta], elegida.desde))


def indice(text: str, max_chars: int) -> str:
    lines = text.splitlines()
    todas = secciones(text)
    cabeza = lines[:_inicio_cuerpo(lines)]
    titulo = next((s for s in todas if s.nivel == 1), None)
    tldr = next((s for s in todas if s.titulo.lower().startswith("tl;dr")), None)
    out = [*cabeza]
    if titulo:
        out.append(lines[titulo.desde - 1])
    if tldr:
        out += ["", *lines[tldr.desde - 1:tldr.hasta]]
    out += ["", f"[doc largo: {len(text)} caracteres, {len(lines)} líneas (más de {max_chars}); "
                "esto es el índice, no el doc]", "Secciones:"]
    out += [f"{'  ' * (s.nivel - 1)}{'#' * s.nivel} {s.titulo}  (líneas {s.desde}-{s.hasta}, "
            f"{s.chars} caracteres)" for s in todas if s.nivel > 1]
    out += ["", "Pedir lo que haga falta: `seccion=\"<título>\"`, `lineas=\"A-B\"` (los números de "
                "cairn_search sirven), o `completo=true` para el doc entero."]
    return "\n".join(out)


def vista(text: str, seccion_: str | None = None, lineas: str | None = None,
          completo: bool = False, max_chars: int = 8000) -> str:
    if seccion_ and lineas:
        raise CairnError("seccion o lineas, no las dos")
    if lineas:
        return rango(text, lineas)
    if seccion_:
        return seccion(text, seccion_)
    if completo or len(text) <= max_chars:
        return text
    return indice(text, max_chars)
