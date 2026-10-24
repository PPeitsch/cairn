"""Editing a doc: its body and front-matter, as an `edit` event (or `set_state` if only
`status`/`note` change).

The doc is edited as a file (`show` → editor → back), so the front-matter can be changed by hand
just like in wip-doc, with two limits:

- links (`origin`, `references`, `plan`) are not edited: `promote`, `handoff new` and `link`
  manage them, and in the file they are computed paths;
- `status` cannot go from open to closed or back: that is `close` / `reopen` /
  `handoff take|drop`, which change other fields at the same time (and `deleted` is `delete` /
  `restore`).

A reference whose body changes gets `updated: <today>` on its own.

Field names and statuses written by hand with the old (Spanish) vocabulary are accepted and
saved in the canonical one (`vocab`).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from cairn import clock, docs, vocab
from cairn.docs import DOC_TYPES, LINK_KINDS, Doc, Result
from cairn.markdown import parse_front_matter
from cairn.projection import CLOSED_STATES, OPEN_STATES
from cairn.store import CairnError, Store

STATE_KEYS = {"status", "note"}
DELETE_KEYS = {"deleted", "deleted_reason"}


def _group(status: str) -> str:
    return "closed" if status in CLOSED_STATES else "open" if status in OPEN_STATES else "?"


def changes(doc: Doc, new_meta: dict | None, new_body: str | None) -> dict:
    """The event payload (empty if nothing changes). Links are left out: see `apply`."""
    payload: dict = {}
    if new_body is not None and new_body != doc.body:
        payload["body"] = new_body
    if new_meta is not None:
        cur = {k: v for k, v in doc.meta.items() if k not in LINK_KINDS}
        new = {k: v for k, v in new_meta.items() if k not in LINK_KINDS and v not in ("", [], None)}
        set_ = {k: v for k, v in new.items() if cur.get(k) != v}
        unset = sorted(k for k in cur if k not in new)
        if set_:
            payload["set"] = set_
        if unset:
            payload["unset"] = unset
    set_, unset = payload.get("set", {}), payload.get("unset", [])
    if "type" in unset or ("type" in set_ and set_["type"] not in DOC_TYPES):
        raise CairnError(f"`type` has to be one of: {', '.join(DOC_TYPES)}")
    if "type" in set_ and (set_["type"] == "ref") != (doc.type == "ref"):
        raise CairnError("A doc does not become a reference (or stop being one) by editing "
                         "its type: that is what `promote` is for.")
    if DELETE_KEYS & (set(set_) | set(unset)):
        raise CairnError("A doc is not deleted (or restored) by editing: that is what `delete` "
                         "and `restore` are for.")
    if "status" in unset and doc.type != "ref":
        raise CairnError("`status` cannot be removed")
    if "status" in set_:
        if _group(set_["status"]) == "?":
            raise CairnError(f"Unknown status: {set_['status']} (open: "
                             f"{', '.join(sorted(OPEN_STATES))}; "
                             f"closed: {', '.join(sorted(CLOSED_STATES))})")
        if _group(set_["status"]) != _group(doc.status):
            raise CairnError(f"`status` does not go from {_group(doc.status)} to "
                             f"{_group(set_['status'])} by editing: use close / reopen / "
                             "handoff take|drop.")
    if "body" in payload and doc.type == "ref" and "updated" not in set_:
        payload.setdefault("set", {})["updated"] = clock.today()
    return payload


def _op(payload: dict) -> str:
    keys = set(payload.get("set", {})) | set(payload.get("unset", []))
    return "set_state" if "body" not in payload and keys and keys <= STATE_KEYS else "edit"


def apply(store: Store, ref: str, text: str | None = None, set_: dict | None = None,
          unset: list[str] | None = None, expected_head: str | None = None,
          body: str | None = None) -> Result:
    """Applies an edit. `text`: the whole doc (with or without front-matter; without it only the
    body changes). `body`: just the body, as is (even if it starts with `---`).
    `set_`/`unset`: individual fields. `expected_head`: if the doc changed since then (another
    edit in between), stop instead of overwriting it."""
    if text is not None and body is not None:
        raise CairnError("apply: `text` or `body`, not both")
    with store.writer() as w:
        doc = docs.find(store, ref)
        if expected_head and doc.head != expected_head:
            raise CairnError(f"{doc.name} changed while you were editing it: nothing was saved.")
        meta = None
        warnings: list[str] = []
        if text is not None:
            meta, body = parse_front_matter(text)
            if meta is None:
                body = text
            else:
                meta = vocab.meta(meta)
                if docs.link_paths(store, doc) != {k: _as_list(meta.get(k)) for k in LINK_KINDS}:
                    warnings.append("[note] Links (origin, references, plan) are not edited by "
                                    "hand: the change was ignored (`cairn link`).")
        if set_ or unset:
            meta = dict(meta if meta is not None else doc.meta)
            for k in vocab.fields(unset or []):
                meta.pop(k, None)
            meta.update(vocab.meta(set_ or {}))
        payload = changes(doc, meta, body)
        if payload:
            w.emit(_op(payload), doc.id, payload)
    return Result(docs.get(store, doc.id), warnings, {"changed": bool(payload)})


def _as_list(v) -> list[str]:
    if not v:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def parse_assignments(items: list[str]) -> dict:
    out = {}
    for it in items:
        if "=" not in it:
            raise CairnError(f"--set expects key=value: {it!r}")
        k, v = it.split("=", 1)
        if not k.strip():
            raise CairnError(f"--set without a key: {it!r}")
        out[k.strip()] = v
    return out


def editor_command() -> list[str]:
    return shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")


def interactive(store: Store, ref: str) -> Result:
    """Opens the doc in the editor and saves what changed. The editor runs without holding the
    lock: if another process touched the doc in the meantime, it is not overwritten (and the
    text stays in the temporary file)."""
    doc = docs.find(store, ref)
    original = docs.render(store, doc)
    tmpdir = Path(tempfile.mkdtemp(prefix="cairn-edit-"))
    path = tmpdir / f"{doc.name}.md"
    path.write_text(original, encoding="utf-8")
    try:
        r = subprocess.run([*editor_command(), str(path)])
    except OSError as e:
        raise CairnError(f"Could not open the editor: {e}") from None
    if r.returncode != 0:
        raise CairnError(f"The editor exited with {r.returncode}: nothing was saved (text in {path})")
    text = path.read_text(encoding="utf-8")
    if text == original:
        path.unlink()
        tmpdir.rmdir()
        return Result(doc, [], {"changed": False})
    try:
        result = apply(store, doc.id, text=text, expected_head=doc.head)
    except CairnError as e:
        raise CairnError(f"{e}\n  Your version is in {path}") from None
    path.unlink()
    tmpdir.rmdir()
    return result
