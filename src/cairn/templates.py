"""Doc templates: the structure of each type.

The HTML comments are the guidance for whoever fills in the doc; `search`, `weekly` and `promote`
ignore them. These are the built-in ones: `[docs] templates = <folder>` in the config replaces
them with that folder's (`bug.md`, `infra.md`, `plan.md`, `ref.md`, `ref_header.md`,
`handoff.md`; any that is missing comes from here). `{title}` is replaced by the doc's title
(`{titulo}` too, for templates exported before it was renamed). `cairn defaults <folder>`
exports them as a starting point.
"""

from __future__ import annotations

from pathlib import Path

DIAG = """
# {title}

## TL;DR

<!-- 2-4 lines: problem, root cause, fix. The first one says WHAT was done (it goes in weekly). -->

---

## Context / topology

<!-- Machines, paths, repos, services involved. Include the traps (confusing things). -->

---

## Symptoms observed

<!-- What was seen, with evidence (command output, logs). -->

```
# paste relevant output here
```

---

## Discoveries (in order)

<!-- The diagnosis step by step. Highlight the root cause. -->

1.

**Root cause:**

---

## Fixes applied

<!-- What was changed and why. -->

---

## Pending / to monitor

- [ ]

---

## Useful commands

```bash
# to pick it up again or diagnose it again
```
"""

PLAN = """
# {title}

## TL;DR

<!-- The goal in 2-4 lines: what is being built and why. -->

---

## Context / problem

<!-- What triggered the need, what exists today that falls short. -->

---

## Scope

<!-- What it includes and what it explicitly does not (to keep the scope from creeping). -->

---

## Design / proposal

<!-- The solution, in phases if the work is incremental. -->

---

## Decisions

<!-- Alternatives considered and why they were dropped, if any. -->

---

## Pending / checklist

- [ ]
"""

REF = """
# {title}

## TL;DR

<!-- What this reference is and when to look it up, in 1-2 lines. -->
"""

REF_CONTENT = """
---

## Content

<!-- Notes, mappings, tables, diagrams, reviews: what gets looked up, not what gets closed.
     Editing it sets `actualizado:` on its own: an old reference lies without warning. -->
"""

HANDOFF = """
# {title}

## TL;DR

<!-- 1-2 lines: what the next conversation has to do. -->

---

## Read first

<!-- Which plan to read and which section. Point to it, do not copy it: if the plan is corrected
     later, the note does not go stale. With `--plan <doc>`, `take` warns if the plan changed
     after the note was written. -->

---

## Work order

1.

---

## Ask before starting

-

---

## How to work

<!-- Optional: what is neither in the plan nor in CLAUDE.md. Nothing destructive: the note is
     archived on its own when taken, it does not ask to be deleted. -->
"""


def defaults() -> dict[str, str]:
    """The built-in templates, by file name."""
    return {"bug.md": DIAG, "infra.md": DIAG, "plan.md": PLAN, "ref.md": REF + REF_CONTENT,
            "ref_header.md": REF, "handoff.md": HANDOFF}


def body(templates_dir: Path | None, kind: str, title: str) -> str:
    """A doc's initial body: `kind` is bug, infra, plan, ref, ref_header (the header of a
    reference created by `promote`) or handoff."""
    name = f"{kind}.md"
    text = None
    if templates_dir is not None:
        p = templates_dir / name
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            if not text.startswith("\n"):
                text = "\n" + text          # like the built-in ones: a blank line after the front-matter
    if text is None:
        text = defaults()[name]
    return text.replace("{title}", title).replace("{titulo}", title)
