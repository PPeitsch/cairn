"""Markdown de los docs: front-matter, secciones y los recortes que usan `search` y `weekly`.

El front-matter es un subconjunto de YAML (escalares `clave: valor` y listas en bloque), el mismo
que escribe wip-doc: alcanza para la metadata y no agrega una dependencia.
"""

from __future__ import annotations

import json
import re

# orden de los campos al escribir el front-matter; los que no están acá van al final
FM_ORDEN = ["tipo", "repo", "para", "estado", "nota", "fecha", "cerrado", "usada", "descartada",
            "reabierto", "actualizado", "autor", "plan", "origen", "referencias"]
# sin `#` (abre comentario en YAML) ni `:`; lo que no matchea se escribe entre comillas
_PLAIN = re.compile(r"^[\w./()+áéíóúñÁÉÍÓÚÑ -]*$")
MESES = ["x", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _yaml_scalar(v) -> str:
    v = str(v)
    if v and _PLAIN.match(v) and not v.startswith(("-", " ")) and not v.endswith(" ") \
            and v.lower() not in ("true", "false", "null", "yes", "no", "~"):
        return v
    return json.dumps(v, ensure_ascii=False)  # JSON string == YAML double-quoted string


def _yaml_unscalar(v: str) -> str:
    v = v.strip()
    if v.startswith('"') and v.endswith('"') and len(v) >= 2:
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if v.startswith("'") and v.endswith("'") and len(v) >= 2:
        return v[1:-1].replace("''", "'")
    return v


def parse_front_matter(text: str) -> tuple[dict | None, str]:
    """(meta, cuerpo). `meta` es None si el texto no arranca con un front-matter."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 3)
    if end == -1:
        return None, text
    raw, body = text[4:end], text[end + 5:]
    meta: dict = {}
    key = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s+-\s+(.*)$", line)
        if m and key is not None:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(_yaml_unscalar(m.group(1)))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2)
            if val.strip() in ("", "[]"):
                meta[key] = [] if val.strip() == "[]" else ""
            else:
                meta[key] = _yaml_unscalar(val)
    return meta, body


def dump_front_matter(meta: dict) -> str:
    keys = [k for k in FM_ORDEN if k in meta] + [k for k in meta if k not in FM_ORDEN]
    out = ["---"]
    for k in keys:
        v = meta[k]
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, list):
            out.append(f"{k}:")
            out.extend(f"  - {_yaml_scalar(i)}" for i in v)
        else:
            out.append(f"{k}: {_yaml_scalar(v)}")
    out.append("---")
    return "\n".join(out) + "\n"


# ──────────────────────────────────────────────────────────────── secciones

COMMENT = re.compile(r"<!--.*?-->", re.S)


def section_raw(body: str, prefix: str) -> tuple[str, str] | None:
    """(heading, contenido) de la primera sección `## <prefix…>` (hasta el próximo `## ` o `---`)."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## ") and line[3:].strip().lower().startswith(prefix.lower()):
            out = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("## ") or re.match(r"^---\s*$", nxt):
                    break
                out.append(nxt)
            return line[3:].strip(), "\n".join(out)
    return None


def section_text(body: str, prefix: str) -> str:
    """Contenido de la sección sin comentarios HTML y sin líneas en blanco en los bordes."""
    s = section_raw(body, prefix)
    if not s:
        return ""
    return COMMENT.sub("", s[1]).strip("\n").strip()


# líneas que deja la plantilla y que no cuentan como contenido
_PLACEHOLDER = re.compile(
    r"^\s*(```\w*|1\.|-|- \[ \]|\*\*Causa raíz:\*\*|# pegar salida relevante acá|"
    r"# para retomar o re-diagnosticar)?\s*$")


def is_placeholder(text: str) -> bool:
    return all(_PLACEHOLDER.match(line) for line in text.splitlines())


def first_sentences(text: str, n: int) -> str:
    """Primeras n oraciones. Un [.!?] cierra oración sólo si lo sigue espacio+mayúscula (o fin) y
    no viene tras un dígito: no corta en AGENTS.md, dominios, decimales ni listas `1.`."""
    line = re.sub(r"\s+", " ", text).strip()
    if not line:
        return ""
    ends = [m.end() for m in re.finditer(r"(?<![0-9])[.!?](?=\s+[A-ZÁÉÍÓÚÑ¿¡`*\[]|\s*$)", line)]
    return line[:ends[n - 1]] if len(ends) >= n else line


def fmt_date(d: str) -> str:
    try:
        return f"{d[8:10]} {MESES[int(d[5:7])]}"
    except (ValueError, IndexError):
        return d


PEND_RE = re.compile(r"^\s*[-*]\s*\[\s\]\s*\S", re.M)   # `- [ ] algo` (no el `- [ ]` vacío)


def pending_items(body: str) -> list[tuple[int, str]]:
    """Los `- [ ]` sin tildar, con su número de línea en el cuerpo."""
    return [(i + 1, line.strip()) for i, line in enumerate(body.splitlines()) if PEND_RE.match(line)]
