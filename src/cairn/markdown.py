"""The docs' markdown: front-matter, sections, and the excerpts `search` and `weekly` use.

The front-matter is a subset of YAML (`key: value` scalars and block lists), the same one wip-doc
writes: it is enough for the metadata and adds no dependency.
"""

from __future__ import annotations

import json
import re

# field order when writing the front-matter; fields not listed here go last
FM_ORDER = ["type", "repo", "to", "status", "note", "date", "closed", "taken", "dropped",
            "reopened", "updated", "author", "plan", "origin", "references"]
# no `#` (starts a YAML comment) and no `:`; anything that does not match is written quoted
_PLAIN = re.compile(r"^[\w./()+áéíóúñÁÉÍÓÚÑ -]*$")
MONTHS = ["x", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


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
    """(meta, body). `meta` is None if the text does not start with a front-matter."""
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
    keys = [k for k in FM_ORDER if k in meta] + [k for k in meta if k not in FM_ORDER]
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


# ──────────────────────────────────────────────────────────────── sections

COMMENT = re.compile(r"<!--.*?-->", re.S)


def section_raw(body: str, prefix: str) -> tuple[str, str] | None:
    """(heading, content) of the first `## <prefix…>` section (up to the next `## ` or `---`)."""
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
    """The content of the section without HTML comments and without blank lines at either end."""
    s = section_raw(body, prefix)
    if not s:
        return ""
    return COMMENT.sub("", s[1]).strip("\n").strip()


# lines the template leaves behind that do not count as content (the built-in templates' and
# those of the Spanish ones they replaced, which older docs still carry)
_PLACEHOLDER = re.compile(
    r"^\s*(```\w*|1\.|-|- \[ \]|\*\*Root cause:\*\*|# paste relevant output here|"
    r"# to pick it up again or diagnose it again|"
    r"\*\*Causa raíz:\*\*|# pegar salida relevante acá|# para retomar o re-diagnosticar)?\s*$")


def is_placeholder(text: str) -> bool:
    return all(_PLACEHOLDER.match(line) for line in text.splitlines())


def first_sentences(text: str, n: int) -> str:
    """The first n sentences. A [.!?] ends a sentence only if it is followed by space + capital
    letter (or the end) and does not come after a digit: it does not split AGENTS.md, domains,
    decimals or `1.` lists."""
    line = re.sub(r"\s+", " ", text).strip()
    if not line:
        return ""
    ends = [m.end() for m in re.finditer(r"(?<![0-9])[.!?](?=\s+[A-ZÁÉÍÓÚÑ¿¡`*\[]|\s*$)", line)]
    return line[:ends[n - 1]] if len(ends) >= n else line


def fmt_date(d: str) -> str:
    try:
        return f"{d[8:10]} {MONTHS[int(d[5:7])]}"
    except (ValueError, IndexError):
        return d


PEND_RE = re.compile(r"^\s*[-*]\s*\[\s\]\s*\S", re.M)   # `- [ ] something` (not the empty `- [ ]`)


def pending_items(body: str) -> list[tuple[int, str]]:
    """The unchecked `- [ ]` items, with their line number in the body."""
    return [(i + 1, line.strip()) for i, line in enumerate(body.splitlines()) if PEND_RE.match(line)]
