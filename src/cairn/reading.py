"""Reading a doc in parts, so a conversation's context is not spent on whole docs.

Everything works on the doc as a file (front-matter included), so the line numbers are the same
ones search returns:

- `lines="40-80"` (or `"40"`, `"40-"`): that range, numbered;
- `section="Decisiones"`: the section whose title starts with (or contains) that, up to the next
  one of its level or higher, numbered;
- a doc longer than `max_chars`, with none of the above and without `full`: its outline
  (front-matter, title, TL;DR and the sections with their lines and size) instead of the doc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cairn.store import CairnError

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Section:
    level: int
    title: str
    start: int          # lines, 1-based and inclusive
    end: int
    chars: int


def _body_start(lines: list[str]) -> int:
    """Index of the first line after the front-matter (0 if there is none)."""
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return i + 1
    return 0


def sections(text: str) -> list[Section]:
    lines = text.splitlines()
    heads, fence = [], False
    for i in range(_body_start(lines), len(lines)):
        if FENCE.match(lines[i]):
            fence = not fence
        elif not fence and (m := HEADING.match(lines[i])):
            heads.append((i, len(m.group(1)), m.group(2)))
    out = []
    for k, (i, level, title) in enumerate(heads):
        end = next((j for j, n, _ in heads[k + 1:] if n <= level), len(lines))
        out.append(Section(level, title, i + 1, end, sum(len(x) + 1 for x in lines[i:end])))
    return out


def _numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{n:>4}| {line}" for n, line in enumerate(lines, start))


def line_range(text: str, spec: str) -> str:
    lines = text.splitlines()
    m = re.fullmatch(r"\s*(\d+)\s*(?:(-)\s*(\d+)?)?\s*", spec)
    if not m:
        raise CairnError(f"lineas: se espera `A-B`, `A-` o `A` (p. ej. `40-80`), no {spec!r}")
    a = int(m.group(1))
    b = int(m.group(3)) if m.group(3) else (len(lines) if m.group(2) else a)
    if a < 1 or b < a or a > len(lines):
        raise CairnError(f"lineas {spec}: el doc tiene {len(lines)} línea(s)")
    b = min(b, len(lines))
    return f"(líneas {a}-{b} de {len(lines)})\n" + _numbered(lines[a - 1:b], a)


def find_section(text: str, name: str) -> str:
    every = sections(text)
    wanted = name.strip().lstrip("#").strip().lower()
    chosen = (next((s for s in every if s.title.lower().startswith(wanted)), None)
              or next((s for s in every if wanted in s.title.lower()), None))
    if chosen is None:
        available = ", ".join(f"«{s.title}»" for s in every if s.level <= 3) or "(ninguna)"
        raise CairnError(f"No hay una sección «{name}». Secciones: {available}")
    lines = text.splitlines()
    return (f"(sección «{chosen.title}», líneas {chosen.start}-{chosen.end} de {len(lines)})\n"
            + _numbered(lines[chosen.start - 1:chosen.end], chosen.start))


def outline(text: str, max_chars: int) -> str:
    lines = text.splitlines()
    every = sections(text)
    head = lines[:_body_start(lines)]
    title = next((s for s in every if s.level == 1), None)
    tldr = next((s for s in every if s.title.lower().startswith("tl;dr")), None)
    out = [*head]
    if title:
        out.append(lines[title.start - 1])
    if tldr:
        out += ["", *lines[tldr.start - 1:tldr.end]]
    out += ["", f"[doc largo: {len(text)} caracteres, {len(lines)} líneas (más de {max_chars}); "
                "esto es el índice, no el doc]", "Secciones:"]
    out += [f"{'  ' * (s.level - 1)}{'#' * s.level} {s.title}  (líneas {s.start}-{s.end}, "
            f"{s.chars} caracteres)" for s in every if s.level > 1]
    out += ["", "Pedir lo que haga falta: `seccion=\"<título>\"`, `lineas=\"A-B\"` (los números de "
                "cairn_search sirven), o `completo=true` para el doc entero."]
    return "\n".join(out)


def view(text: str, section: str | None = None, lines: str | None = None,
         full: bool = False, max_chars: int = 8000) -> str:
    if section and lines:
        raise CairnError("seccion o lineas, no las dos")
    if lines:
        return line_range(text, lines)
    if section:
        return find_section(text, section)
    if full or len(text) <= max_chars:
        return text
    return outline(text, max_chars)
