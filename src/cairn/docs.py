"""Operations on docs: what wip-doc's commands did, now as events.

There are no folders: where a doc "is" (`wip`, `done`, `ref`) follows from its type and status.
Links between docs (`origin`, `references`, `plan`) are stored by id, so closing or reopening
does not break them: the relative paths (`../_ref/x.md`) are computed when showing or exporting.

Every operation returns data (and warnings) and prints nothing: output is the CLI's business,
and the MCP server uses these same functions.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from cairn import clock, templates, vocab
from cairn.markdown import (
    COMMENT,
    dump_front_matter,
    fmt_date,
    is_placeholder,
    pending_items,
    section_raw,
    section_text,
)
from cairn.projection import CLOSED_STATES, OPEN_STATES
from cairn.store import CairnError, Store, new_doc_id

TYPES = ("bug", "infra", "plan", "ref")          # the ones `new` creates
DOC_TYPES = TYPES + ("handoff",)                 # the ones that exist (handoff: `handoff new`)
SCOPES = ("wip", "done", "ref")
ICON = {"bug": "[bug]  ", "plan": "[plan] ", "ref": "[ref]  ", "infra": "[infra]", "handoff": "[handoff]"}
LINK_KINDS = ("plan", "origin", "references")   # the ones that go into the front-matter as paths
CROSS_REPO = "cross-repo"                       # `repo:` of what spans several (or none)
_NAME_RE = re.compile(r"^[\w.+-]+$")


# ──────────────────────────────────────────────────────────────── the doc

@dataclass
class Doc:
    id: str
    name: str
    meta: dict
    body: str
    scope: str
    head: str
    created: str
    last_event: str      # when it last changed (for an imported doc, its file's modification time)
    divergent: bool
    links: list[list[str]] = field(default_factory=list)

    @property
    def type(self) -> str:
        t = self.meta.get("type")
        return t if t in DOC_TYPES else "infra"

    @property
    def status(self) -> str:
        return str(self.meta.get("status", ""))

    @property
    def repo(self) -> str:
        return str(self.meta.get("repo") or "")

    @property
    def title(self) -> str:
        m = re.search(r"^# (.+)$", self.body, re.M)
        return m.group(1).strip() if m else self.name

    @property
    def tldr(self) -> str:
        return section_text(self.body, "TL;DR")

    @property
    def path(self) -> str:
        """The doc as a file, `_<scope>/<name>.md`: how it is shown, what links are rendered
        relative to, and where `migrate wip-doc export` writes it."""
        return f"_{self.scope}/{self.name}.md"

    def date_closed(self) -> str:
        for k in ("closed", "taken", "dropped"):
            if self.meta.get(k):
                return str(self.meta[k])
        return self.last_event[:10]


def _doc_from_row(store: Store, r) -> Doc:
    links = [[x["kind"], x["dst"]] for x in store.projection.db.execute(
        "SELECT kind, dst FROM links WHERE src = ? ORDER BY rowid", (r["id"],))]
    return Doc(id=r["id"], name=r["name"], meta=json.loads(r["meta"]), body=r["body"],
               scope=r["scope"], head=r["head"], created=r["created"], last_event=r["last_event"],
               divergent=bool(r["divergent"]), links=links)


def all_docs(store: Store, scopes: tuple[str, ...] = SCOPES) -> list[Doc]:
    """The docs in those scopes, most recently touched first."""
    marks = ",".join("?" * len(scopes))
    rows = store.projection.db.execute(
        f"SELECT * FROM docs WHERE scope IN ({marks}) ORDER BY last_event DESC, name", scopes).fetchall()
    return [_doc_from_row(store, r) for r in rows]


def get(store: Store, doc_id: str) -> Doc | None:
    r = store.projection.db.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
    return _doc_from_row(store, r) if r else None


def normalize_ref(ref: str) -> str:
    """`../_done/x.md`, `_wip/x.md`, `x.md` or `x` → `x`."""
    ref = ref.strip().rstrip("/").split("/")[-1]
    return ref[:-3] if ref.endswith(".md") else ref


def find(store: Store, ref: str) -> Doc:
    """A doc by name (with or without `.md` and a leading folder) or by id prefix (6+ hex). A
    name no doc has now is looked up among the ones they had (`rename`): what still says the
    old name (a commit, a script, another node's note) keeps finding it."""
    name = normalize_ref(ref)
    db = store.projection.db
    rows = db.execute("SELECT * FROM docs WHERE name = ?", (name,)).fetchall()
    if not rows and re.fullmatch(r"[0-9a-f]{6,32}", name):
        rows = db.execute("SELECT * FROM docs WHERE id LIKE ?", (name + "%",)).fetchall()
    if not rows:
        rows = db.execute("SELECT * FROM docs WHERE id IN (SELECT doc_id FROM revisions "
                          "WHERE name = ?)", (name,)).fetchall()
    if not rows:
        raise CairnError(f"Not found: {ref}")
    if len(rows) > 1:
        raise CairnError(f"{ref} is ambiguous: " + ", ".join(f"{r['name']} ({r['id'][:8]})" for r in rows))
    return _doc_from_row(store, rows[0])


def name_taken(store: Store, name: str) -> bool:
    return store.projection.db.execute("SELECT 1 FROM docs WHERE name = ?", (name,)).fetchone() is not None


def _check_name_parts(*parts: str) -> None:
    for p in parts:
        if not _NAME_RE.match(p):
            raise CairnError(f"Invalid name: {p!r} (letters, digits, `_`, `-`, `.`, `+`)")


# ──────────────────────────────────────────────────────────────── render

def link_paths(store: Store, doc: Doc) -> dict[str, list[str]]:
    """Links as relative paths in the tree (`../_ref/x.md`), by kind. Adds the ones left as text
    in the front-matter (imported links that did not point at any doc)."""
    out: dict[str, list[str]] = {}
    for kind in LINK_KINDS:
        raw = doc.meta.get(kind)
        out[kind] = [str(x) for x in raw] if isinstance(raw, list) else ([str(raw)] if raw else [])
    for kind, dst in doc.links:
        target = get(store, dst)
        if target is not None and kind in out:
            p = f"../{target.path}"
            if p not in out[kind]:
                out[kind].append(p)
    return out


def render(store: Store, doc: Doc) -> str:
    """The doc as a `.md` file: front-matter (with links as paths) + body."""
    meta = {k: v for k, v in doc.meta.items() if k not in LINK_KINDS}
    meta.update({k: v for k, v in link_paths(store, doc).items() if v})
    return dump_front_matter(meta) + doc.body


def repo_label(doc: Doc) -> str:
    """Older docs created outside any repo have no `repo:`: they are cross-repo too."""
    return doc.repo or CROSS_REPO


def status_label(doc: Doc) -> str:
    if doc.scope == "wip":
        return "pending" if doc.type == "handoff" else "open"
    if doc.scope == "ref":
        return "reference"
    if doc.type == "handoff":
        return f"{doc.meta.get('status', 'taken')} {fmt_date(doc.date_closed())}"
    return f"closed {fmt_date(doc.date_closed())}"


# ──────────────────────────────────────────────────────────────── new / close / reopen

@dataclass
class Result:
    doc: Doc
    warnings: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def _meta_base(store: Store, type_: str, status: str | None, repo: str) -> dict:
    meta = {"type": type_, "repo": repo or CROSS_REPO}
    if status:
        meta["status"] = status
    meta["date"] = clock.today()
    if type_ == "ref":
        meta["updated"] = clock.today()
    meta["author"] = store.config.author
    return meta


def new(store: Store, area: str, topic: str, type_: str = "infra", repo: str = "") -> Result:
    if type_ == "handoff":
        raise CairnError("A handoff is created with: handoff new <area> <topic> [--to R]... "
                         "[--plan <doc>]...")
    if type_ not in TYPES:
        raise CairnError(f"Invalid type: {type_}. Options: {', '.join(TYPES)}")
    _check_name_parts(area, topic)
    title = f"{area} — {topic.replace('_', ' ')}"
    if type_ == "ref":
        # a reference is neither opened nor closed: no date in the name, it is looked up by topic
        name = f"{area}_{topic}"
        body = templates.body(store.config.templates_dir, "ref", title)
        meta = _meta_base(store, "ref", None, repo)
    else:
        infix = "_BUG_" if type_ == "bug" else "_"
        name = f"{clock.today()}_{area}{infix}{topic}"
        body = templates.body(store.config.templates_dir, type_, title)
        meta = _meta_base(store, type_, "in_progress", repo)
    doc_id = new_doc_id()
    with store.writer() as w:
        if name_taken(store, name):
            raise CairnError(f"Already exists: {name}")
        w.emit("create", doc_id, {"name": name, "meta": meta, "body": body})
    return Result(get(store, doc_id))


def close(store: Store, ref: str, force: bool = False, discard: str | None = None) -> Result:
    """Closes a finished doc (resolved/completed) or, with `discard` (the reason), one that is
    left unfinished: it becomes `discarded` with the reason in `note:`, and needs no --force."""
    if discard is not None and not discard.strip():
        raise CairnError("Discarding needs a reason: it is the only thing that will explain why it "
                         "was left that way.")
    warnings: list[str] = []
    with store.writer() as w:
        doc = find(store, ref)
        if doc.type == "handoff":
            raise CairnError(f"{doc.name} is a handoff: it is archived with `handoff take` (to use "
                             "it) or `handoff drop` (to discard it).")
        if doc.scope == "ref":
            raise CairnError(f"{doc.name} is a reference: it is not closed, it is updated.")
        if doc.scope == "done":
            raise CairnError(f"{doc.name} is already closed ({doc.date_closed()}).")
        # a doc with an open checklist is not done: closing it removes it from `list` and reports
        # it in `weekly` as finished work, which is exactly what it is not
        pend = pending_items(doc.body)
        if discard:
            if doc.meta.get("note"):
                warnings.append(f"[note] Note replaced by the reason: \"{doc.meta['note']}\"")
            w.emit("close", doc.id, {"set": {"status": "discarded", "closed": clock.today(),
                                             "note": discard.strip()},
                                     "unset": ["reopened"]})
        elif pend and not force:
            lines = "\n".join(f"    {n}: {t}" for n, t in pend[:10])
            raise CairnError(f"[warning] {doc.name} has {len(pend)} unchecked pending item(s):\n"
                             f"{lines}\n\nCheck them, delete them, close it anyway with: close "
                             f"{doc.name} --force, or, if it is left unfinished: close {doc.name} "
                             "--discard \"reason\"")
        else:
            if doc.meta.get("note"):
                # the note described the open work ("low priority", "reopened because…"); once closed, it lies
                warnings.append(f"[note] Note dropped on closing: \"{doc.meta['note']}\" — if it had "
                                "information about the closing, move it to the doc's body.")
            if pend:
                warnings.append(f"[warning] Closed with {len(pend)} unchecked pending item(s) (--force)")
            status = "completed" if doc.type == "plan" else "resolved"
            w.emit("close", doc.id, {"set": {"status": status, "closed": clock.today()},
                                     "unset": ["note", "reopened"]})
    return Result(get(store, doc.id), warnings)


def reopen(store: Store, ref: str, reason: str | None = None) -> Result:
    warnings: list[str] = []
    with store.writer() as w:
        doc = find(store, ref)
        if doc.scope != "done":
            raise CairnError(f"{doc.name} is not closed (it is in {doc.scope}).")
        if doc.type == "handoff":
            # taken by mistake, or the conversation was cut short: it waits for the next one again
            change = {"set": {"status": "pending"}, "unset": ["taken", "dropped", "closed"]}
        else:
            change = {"set": {"status": "in_progress"}, "unset": ["closed"]}
        change["set"]["reopened"] = clock.today()
        if reason:
            change["set"]["note"] = reason
        else:
            change["unset"].append("note")
            warnings.append("[note] No reason: pass it as the 2nd argument so it stays in the note")
        w.emit("reopen", doc.id, change)
    return Result(get(store, doc.id), warnings)


# ──────────────────────────────────────────────────────────────── promote

# the sections `promote` carries over by default (plan / diagnosis), each one in English or as
# the Spanish templates named it
PROMOTE_DEFAULTS = {
    True: [("Design", "Diseño"), ("Decisions", "Decisiones")],
    False: [("Context", "Contexto"), ("Useful commands", "Comandos útiles")],
}

def promote(store: Store, ref: str, area: str, topic: str, sections: list[str] | None = None,
            force: bool = False) -> Result:
    """A diagnosis tells WHAT HAPPENED; the topology it uncovered or the commands to diagnose
    again say HOW SOMETHING IS. Copies (does not move) those sections to a reference and links
    the two docs (`origin` / `references`)."""
    _check_name_parts(area, topic)
    warnings: list[str] = []
    with store.writer() as w:
        src = find(store, ref)
        if src.scope == "ref":
            raise CairnError(f"{src.name} is already a reference: promote takes open or closed docs.")
        ref_name = f"{area}_{topic}"
        sections = sections or [next((x for x in alts if section_raw(src.body, x)), alts[0])
                                for alts in PROMOTE_DEFAULTS[src.type == "plan"]]
        target = None
        if name_taken(store, ref_name):
            target = find(store, ref_name)
            if target.scope != "ref":
                raise CairnError(f"{ref_name} exists and is not a reference.")
            if not force and ["origin", src.id] in target.links:
                raise CairnError(f"{ref_name} already has sections promoted from {src.name}. "
                                 "--force to add them anyway.")

        # gather first: if there is nothing to carry over, nothing is created or touched
        block, carried, skipped = "", [], []
        for s in sections:
            raw = section_raw(src.body, s)
            if not raw:
                skipped.append(f"{s} (does not exist)")
                continue
            heading, content = raw
            content = COMMENT.sub("", content).strip("\n")
            if is_placeholder(content):
                skipped.append(f"{heading} (empty)")
                continue
            carried.append(heading)
            block += f"\n---\n\n## {heading}\n\n{content.strip()}\n"
        if not carried:
            raise CairnError(f"Nothing to promote from {src.name}: {', '.join(skipped)}\n"
                             "Pick sections with --section \"<start of the title>\".")

        created = target is None
        if created:
            ref_id = new_doc_id()
            body = templates.body(store.config.templates_dir, "ref_header",
                                  f"{area} — {topic.replace('_', ' ')}")
            w.emit("create", ref_id, {"name": ref_name, "body": body,
                                      "meta": _meta_base(store, "ref", None, src.repo)})
        else:
            ref_id, body = target.id, target.body
        w.emit("promote", ref_id, {"body": body.rstrip("\n") + "\n" + block,
                                   "set": {"updated": clock.today()},
                                   "link_add": [["origin", src.id]]})
        if ["references", ref_id] not in src.links:
            w.emit("link", src.id, {"link_add": [["references", ref_id]]})
    if skipped:
        warnings.append(f"  skipped: {', '.join(skipped)}")
    if created:
        warnings.append("[note] New reference: fill in the TL;DR and rewrite what was copied as a "
                        "reference\n    (what IS, not what happened); remove the incident's context "
                        "that does not apply.")
    else:
        warnings.append("[note] Check that what was added does not contradict what the reference "
                        "already said.")
    return Result(get(store, ref_id), warnings, {"carried": carried})


def link(store: Store, ref: str, kind: str, other: str, remove: bool = False) -> Result:
    """Links two docs by hand, for what `promote` and `handoff new` do not cover (pending items
    carried to a new doc, a plan that came out of another one). `ref` gets `other` under
    `<kind>:`; it is one way, the other doc shows it as an incoming link. `remove` takes it
    out, and also accepts the id prefix of a doc that no longer exists (what `check` reports)."""
    kind = vocab.LINK_ALIASES.get(kind, kind)
    if kind not in LINK_KINDS:
        raise CairnError(f"Unknown link kind: {kind} (one of: {', '.join(LINK_KINDS)})")
    with store.writer() as w:
        doc = find(store, ref)
        mine = [dst for k, dst in doc.links if k == kind]
        try:
            dst = find(store, other).id
        except CairnError:
            dangling = [x for x in mine if get(store, x) is None and x.startswith(normalize_ref(other))]
            if not remove or len(dangling) != 1:
                raise
            dst = dangling[0]
        if dst == doc.id:
            raise CairnError("A doc is not linked to itself.")
        target = get(store, dst)
        label = target.name if target else dst[:8]
        if remove:
            if dst not in mine:
                raise CairnError(f"{doc.name} has no `{kind}` link to {label}.")
            w.emit("unlink", doc.id, {"link_remove": [[kind, dst]]})
        elif dst not in mine:
            w.emit("link", doc.id, {"link_add": [[kind, dst]]})
        else:
            return Result(doc, [f"[note] {doc.name} already had `{kind}` → {label}."],
                          {"changed": False, "other": label})
    return Result(get(store, doc.id), [], {"changed": True, "other": label})


# ──────────────────────────────────────────────────────────────── rename / delete / restore

def _mention_re(name: str) -> re.Pattern:
    """The name as a whole token: not part of a longer name (`x_v2`, `x.v2`), but it may end in
    `.md` or come after a folder (`../_done/x.md`)."""
    return re.compile(rf"(?<![\w.-]){re.escape(name)}(?![\w-])(?!\.(?!md\b)\w)")


def _replace_mentions(rx: re.Pattern, new: str, value):
    """The value (a front-matter one or the body) with the mentions replaced, or None if it has
    none."""
    if isinstance(value, str):
        out = rx.sub(new, value)
        return out if out != value else None
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        out = [rx.sub(new, x) for x in value]
        return out if out != value else None
    return None


def _mention_changes(doc: Doc, rx: re.Pattern, new: str) -> dict:
    payload: dict = {}
    body = _replace_mentions(rx, new, doc.body)
    if body is not None:
        payload["body"] = body
    meta = {k: v for k, v in ((k, _replace_mentions(rx, new, v)) for k, v in doc.meta.items())
            if v is not None}
    if meta:
        payload["set"] = meta
    return payload


def rename(store: Store, ref: str, new_name: str) -> Result:
    """Changes a doc's name and, in every doc (this one included), the mentions of the old one:
    links go by id and do not need it, but the text does (a `note:`, a path in the body). The old
    name keeps finding the doc (`find`)."""
    new_name = normalize_ref(new_name)
    _check_name_parts(new_name)
    with store.writer() as w:
        doc = find(store, ref)
        old = doc.name
        if new_name == old:
            raise CairnError(f"{old} is already called that.")
        if name_taken(store, new_name):
            raise CairnError(f"Already exists: {new_name}")
        if doc.scope != "ref" and not re.match(r"^\d{4}-\d{2}-\d{2}_", new_name):
            raise CairnError(f"{new_name} does not start with a YYYY-MM-DD_ date: only "
                             "references go without one.")
        rx = _mention_re(old)
        w.emit("rename", doc.id, {"name": new_name, **_mention_changes(doc, rx, new_name)})
        touched = []
        for other in all_docs(store):
            if other.id == doc.id:
                continue
            change = _mention_changes(other, rx, new_name)
            if change:
                w.emit("edit", other.id, change)
                touched.append(other.name)
    return Result(get(store, doc.id), [], {"old": old, "mentions": sorted(touched)})


def delete(store: Store, ref: str, reason: str | None = None) -> Result:
    """Deletes a doc: it leaves `list`, `search`, `show` and the web view, and its name is free
    again. The log is append-only and synced, so its events stay (they cannot be purged: another
    node would bring them back): `restore` undoes it. Returns the doc as it was."""
    reason = (reason or "").strip()
    warnings: list[str] = []
    with store.writer() as w:
        doc = find(store, ref)
        incoming = sorted(r["name"] for r in store.projection.db.execute(
            "SELECT DISTINCT d.name FROM links l JOIN docs d ON d.id = l.src WHERE l.dst = ?",
            (doc.id,)))
        change = {"deleted": clock.today()}
        if reason:
            change["deleted_reason"] = reason
        w.emit("delete", doc.id, {"set": change})
    if incoming:
        warnings.append(f"[note] {len(incoming)} doc(s) still link to it: {', '.join(incoming)}. "
                        "`check` reports them; `restore` or `link ... --remove` settles it.")
    return Result(doc, warnings)


def _deleted_doc(store: Store, doc_id: str, state, r) -> Doc:
    return Doc(id=doc_id, name=state.name, meta=state.meta, body=state.body, scope="deleted",
               head=r["event_id"], created="", last_event=r["ts"], divergent=False,
               links=state.links)


def deleted_docs(store: Store) -> list[Doc]:
    """The deleted docs, the most recently deleted first."""
    return [_deleted_doc(store, i, s, r) for i, s, r in store.projection.deleted()]


def restore(store: Store, ref: str) -> Result:
    """Brings a deleted doc back as it was. Its name has to be free: if another doc took it,
    rename that one first."""
    with store.writer() as w:
        name = normalize_ref(ref)
        found = [d for d in deleted_docs(store)
                 if d.name == name or (re.fullmatch(r"[0-9a-f]{6,32}", name) and d.id.startswith(name))]
        if not found:
            raise CairnError(f"No deleted doc is called {ref} (`restore` with no name lists them).")
        if len(found) > 1:
            raise CairnError(f"{ref} is ambiguous: " + ", ".join(f"{d.name} ({d.id[:8]})" for d in found))
        doc = found[0]
        if name_taken(store, doc.name):
            raise CairnError(f"{doc.name} is taken by another doc: rename that one first.")
        w.emit("restore", doc.id, {"unset": ["deleted", "deleted_reason"]})
    return Result(get(store, doc.id))


def deleted_line(doc: Doc) -> str:
    reason = doc.meta.get("deleted_reason")
    return (f"{doc.name}  [{doc.type} · deleted {fmt_date(str(doc.meta.get('deleted', '')))} · "
            f"{repo_label(doc)}]" + (f"  {reason}" if reason else ""))


# ──────────────────────────────────────────────────────────────── search / weekly

@dataclass
class Hit:
    doc: Doc
    lines: list[tuple[int, str]]


def search(store: Store, pattern: str, scopes: tuple[str, ...] = SCOPES) -> tuple[list[Hit], int]:
    """A Python regex, case-insensitive, over the doc as a file (front-matter included, so the
    line numbers match `show`). Returns (hits, docs searched)."""
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        raise CairnError(f"Invalid regex: {e}") from None
    docs = all_docs(store, scopes)
    hits = []
    for doc in docs:
        text = render(store, doc)
        lines = [(i + 1, line) for i, line in enumerate(text.splitlines()) if rx.search(line)]
        if lines:
            hits.append(Hit(doc, lines))
    hits.sort(key=lambda h: h.doc.date_closed(), reverse=True)
    hits.sort(key=lambda h: repo_label(h.doc))
    return hits, len(docs)


@dataclass
class FtsHit:
    doc: Doc
    snippet: str        # the part of the doc where it matches, with the match between «»


def search_fts(store: Store, query: str, scopes: tuple[str, ...] = SCOPES,
               limit: int = 20) -> tuple[list[FtsHit], int]:
    """Full-text search (FTS5 syntax, ignoring accents and case), by relevance. Returns up to
    `limit` results, each with the snippet where it matches, and how many docs match in total
    (to say so if the list was cut)."""
    marks = ",".join("?" * len(scopes))
    where = f"FROM docs_fts f JOIN docs d ON d.id = f.doc_id WHERE docs_fts MATCH ? AND d.scope IN ({marks})"
    try:
        rows = store.projection.db.execute(
            f"SELECT d.*, snippet(docs_fts, -1, '«', '»', ' … ', 20) AS snippet {where} "
            "ORDER BY bm25(docs_fts) LIMIT ?", (query, *scopes, limit)).fetchall()
        total = store.projection.db.execute(f"SELECT count(*) {where}", (query, *scopes)).fetchone()[0]
    except sqlite3.OperationalError as e:      # invalid FTS5 syntax
        raise CairnError(f"Invalid FTS query: {e}") from None
    return [FtsHit(_doc_from_row(store, r), " ".join(r["snippet"].split())) for r in rows], total


def fts_header(query: str, hits: list[FtsHit], total: int) -> str:
    cut = f"; showing the {len(hits)} most relevant" if total > len(hits) else ""
    return f'FTS search "{query}": {total} doc(s), by relevance{cut}'


def weekly(store: Store, days: int = 7) -> tuple[str, list[Doc]]:
    """What was closed in the last `days` days (no handoffs: they are not closed work). (since, docs)."""
    since = (date.fromisoformat(clock.today()) - timedelta(days=days)).isoformat()
    docs = [d for d in all_docs(store, ("done",)) if d.type != "handoff" and d.date_closed() >= since]
    docs.sort(key=lambda d: d.date_closed(), reverse=True)
    return since, docs


# ──────────────────────────────────────────────────────────────── check

def check(store: Store) -> list[str]:
    """What is out of order: the log (corrupt lines, invalid signatures, unprojected events,
    divergent branches) and the docs (inconsistent status, names, broken links, stale handoffs)."""
    problems: list[str] = []
    in_log = 0
    problems += store.verifier.bad_peers
    for entry in store.log.read_all():
        why = entry.error if entry.event is None else store.verifier.problem(entry.event)
        if why:
            problems.append(f"logs/{entry.node}/{entry.segment}.jsonl byte {entry.offset}: {why}")
        else:
            in_log += 1
    projected = store.projection.count_events()
    if projected != in_log:
        problems.append(f"the projection has {projected} event(s) and the log {in_log}: "
                        "run `cairn rebuild`")
    db = store.projection.db
    for r in db.execute("SELECT status, count(*) n FROM events WHERE status NOT IN ('applied', 'resolved') "
                      "GROUP BY status"):
        what = {"divergent": "on divergent branches (two nodes wrote on top of the same head)",
                "orphan": "orphaned (the previous event is missing: sync)"}.get(r["status"], r["status"])
        problems.append(f"{r['n']} event(s) {what}")
    for r in db.execute("SELECT name, count(*) n FROM docs GROUP BY name HAVING n > 1"):
        problems.append(f"{r['name']}: {r['n']} docs with the same name")
    ids = {r["id"] for r in db.execute("SELECT id FROM docs")}
    deleted = {d.id: d.name for d in deleted_docs(store)}
    for doc in all_docs(store):
        n = doc.name
        if doc.divergent:
            problems.append(f"{n} has divergent branches: `cairn resolve {n}` to see them and pick one")
        if doc.meta.get("type") not in DOC_TYPES:
            problems.append(f"{n} has type '{doc.meta.get('type', '')}' (valid: {', '.join(DOC_TYPES)})")
        if doc.scope != "ref":
            if not re.match(r"^\d{4}-\d{2}-\d{2}_", n):
                problems.append(f"{n} does not start with a YYYY-MM-DD_ date")
            if doc.status not in OPEN_STATES | CLOSED_STATES:
                problems.append(f"{n} has status '{doc.status}' (unknown)")
        if doc.type == "handoff":
            if doc.scope == "done" and not (doc.meta.get("taken") or doc.meta.get("dropped")):
                problems.append(f"{n} is an archived handoff without `taken:` or `dropped:`")
            age = handoff_age(doc)
            if doc.scope == "wip" and age is not None and age > store.config.handoff_stale_days:
                problems.append(f"{n} is a handoff not taken for {age} days: "
                                "`handoff take` or `handoff drop`")
        elif doc.scope == "done" and not doc.meta.get("closed"):
            problems.append(f"{n} is closed without `closed:`: weekly falls back to the last change")
        for kind, dst in doc.links:
            if dst in deleted:
                problems.append(f"{n}: the `{kind}` link points to {deleted[dst]}, which was deleted: "
                                f"`cairn restore {deleted[dst]}` or `cairn link {n} {kind} "
                                f"{dst[:8]} --remove`")
            elif dst not in ids:
                problems.append(f"{n}: the `{kind}` link points to a doc that does not exist ({dst[:8]})")
    return problems


# ──────────────────────────────────────────────────────────────── handoffs

def _as_list(v) -> list[str]:
    if not v:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def handoff_to(doc: Doc) -> list[str]:
    """Which conversations it is meant for: `to:`, or if missing, the doc's `repo:`."""
    return _as_list(doc.meta.get("to")) or _as_list(doc.meta.get("repo"))


def handoff_age(doc: Doc) -> int | None:
    try:
        return (date.fromisoformat(clock.today()) - date.fromisoformat(str(doc.meta["date"]))).days
    except (KeyError, TypeError, ValueError):
        return None


def pending_handoffs(store: Store, repo: str) -> list[Doc]:
    """The pending handoffs for a conversation in `repo`. From the projects root (repo "") all
    of them are shown; from a repo, those that name it in `to:`."""
    out = [d for d in all_docs(store, ("wip",))
           if d.type == "handoff" and d.status == "pending" and (not repo or repo in handoff_to(d))]
    return sorted(out, key=lambda d: d.name)


def modified_date(doc: Doc) -> date:
    """The (local) date of the doc's last change."""
    ts = datetime.fromisoformat(doc.last_event)
    tz = clock.now().tzinfo if os.environ.get("CAIRN_NOW") else None
    return ts.astimezone(tz).date()


def modified_label(doc: Doc) -> str:
    d = modified_date(doc)
    days = (date.fromisoformat(clock.today()) - d).days
    return f"modified {d.isoformat()}, " + ("today" if days <= 0 else f"{days} day(s) ago")


def grouped_by_repo(docs_: list[Doc]) -> list[str]:
    """The lines of a listing grouped by repo, with how many docs each group has and when each
    doc was last touched. `docs_` goes from newest to oldest (`all_docs`): the groups keep that
    order (that of each group's newest doc) and so do the docs inside them."""
    groups: dict[str, list[Doc]] = {}
    for d in docs_:
        groups.setdefault(repo_label(d), []).append(d)
    out = [f"{len(docs_)} doc(s) in {len(groups)} repo(s); groups and docs from the most recently "
           "modified to the oldest"]
    for repo, ds in groups.items():
        out += ["", f"{repo} ({len(ds)})"]
        out += [f"  {d.name}  [{d.type} · {status_label(d)} · {modified_label(d)}]" for d in ds]
    return out


def handoff_line(doc: Doc, stale: int = 14) -> str:
    """`stale`: after how many days it is flagged as old (`[docs] handoff_stale_days`)."""
    age = handoff_age(doc)
    parts = [f"to: {', '.join(handoff_to(doc)) or CROSS_REPO}"]
    if age is not None:
        parts.append("from today" if age == 0 else f"{age} day(s) old")
        if age > stale:
            parts.append("OLD: check whether it still holds")
    return f"{doc.name}  ({'; '.join(parts)})"


def handoff_new(store: Store, area: str, topic: str, to: list[str] | None = None,
                plans: list[str] | None = None, repo: str = "") -> Result:
    _check_name_parts(area, topic)
    to, plans = to or [], plans or []
    name = f"{clock.today()}_{area}_HANDOFF_{topic}"
    meta = _meta_base(store, "handoff", "pending", repo)
    if to:
        meta["to"] = to if len(to) > 1 else to[0]
    doc_id = new_doc_id()
    with store.writer() as w:
        if name_taken(store, name):
            raise CairnError(f"Already exists: {name}")
        links = [["plan", find(store, p).id] for p in plans]
        body = templates.body(store.config.templates_dir, "handoff",
                              f"{area} — handoff: {topic.replace('_', ' ')}")
        w.emit("handoff_new", doc_id, {"name": name, "meta": meta, "body": body, "links": links})
    warnings = []
    if not (to or repo):
        warnings.append("[note] No `repo:` and no `--to`: it is only announced in conversations "
                        "opened in the projects root.")
    return Result(get(store, doc_id), warnings)


def _pick_handoff(store: Store, ref: str | None, repo: str) -> Doc:
    if ref:
        doc = find(store, ref)
        if doc.type != "handoff":
            raise CairnError(f"{doc.name} is not a handoff (type: {doc.meta.get('type', '?')}).")
        if doc.status != "pending":
            raise CairnError(f"{doc.name} is no longer pending: {doc.status} on {doc.date_closed()} "
                             "(another conversation took it, or it was discarded). `reopen` makes "
                             "it pending again.")
        return doc
    pend = pending_handoffs(store, repo)
    where = repo or "the projects root"
    if not pend:
        raise CairnError(f"No pending handoffs for {where}.")
    if len(pend) > 1:
        raise CairnError(f"There are {len(pend)} pending handoffs for {where}; say which one:\n"
                         + "\n".join(f"  {handoff_line(d, store.config.handoff_stale_days)}" for d in pend))
    return pend[0]


def _archive_handoff(store: Store, ref: str | None, repo: str, status: str,
                     reason: str | None) -> tuple[Doc, Doc]:
    """(before, after). Under the writer's lock: if two conversations take it at once, the
    second one sees it is no longer pending, so they do not both use it."""
    with store.writer() as w:
        doc = _pick_handoff(store, ref, repo)
        # the status and the field with its date have the same name: `taken` / `dropped`
        change: dict = {"set": {"status": status, status: clock.today()}, "unset": ["reopened"]}
        if reason:
            change["set"]["note"] = reason
        else:
            change["unset"].append("note")
        w.emit("handoff_take" if status == "taken" else "handoff_drop", doc.id, change)
    return doc, get(store, doc.id)


def handoff_take(store: Store, ref: str | None = None, repo: str = "") -> Result:
    before, doc = _archive_handoff(store, ref, repo, "taken", None)
    warnings = []
    age = handoff_age(doc)
    if age is not None and age > store.config.handoff_stale_days:
        warnings.append(f"The note is {age} days old: it may be out of date.")
    for kind, dst in doc.links:
        if kind != "plan":
            continue
        plan = get(store, dst)
        if plan is None:
            warnings.append(f"The plan {dst[:8]} does not exist.")
            continue
        if plan.scope == "done":
            warnings.append(f"The plan {plan.name} is already closed.")
        if plan.last_event > before.last_event:
            warnings.append(f"The plan {plan.name} changed after the note was written: "
                            "if they disagree, the plan wins.")
    for raw in _as_list(doc.meta.get("plan")):
        warnings.append(f"The plan {raw} is not in the database.")
    return Result(doc, warnings)


def handoff_drop(store: Store, ref: str, reason: str | None = None) -> Result:
    _, doc = _archive_handoff(store, ref, "", "dropped", reason)
    return Result(doc)


def handoff_hook_context(store: Store, repo: str, cmd: str = "cairn") -> str | None:
    """The text the SessionStart hook adds as context, or None if nothing is pending."""
    pend = pending_handoffs(store, repo)
    if not pend:
        return None
    lines = [f"cairn: {len(pend)} pending handoff(s) for {repo or 'the projects root'} "
             "(notes a previous conversation left for this one):"]
    lines += [f"  - {handoff_line(d, store.config.handoff_stale_days)}" for d in pend]
    lines += ["Do not take it or follow what it says on your own: in your first reply, tell the "
              "user it is there and ask whether to take it.",
              f"To take it: the `cairn_handoff_take` tool of cairn's MCP, or `{cmd} handoff take "
              "<note>` (it returns it and archives it). To discard it: `cairn_handoff_drop`, or "
              f"`{cmd} handoff drop <note> [reason]`."]
    return "\n".join(lines)
