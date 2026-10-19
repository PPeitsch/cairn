// cairn serve: everything here is an enhancement; the pages work without it.
(() => {
  const root = document.documentElement;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch { /* private mode */ } },
  };

  // ── theme: auto (system) → light → dark
  const isDark = () => root.dataset.theme === "dark" ||
    (!root.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  document.querySelectorAll(".theme").forEach((b) => b.addEventListener("click", () => {
    const next = { "": "light", light: "dark", dark: "" }[root.dataset.theme || ""];
    if (next) root.dataset.theme = next; else delete root.dataset.theme;
    store.set("cairn-theme", next);
    b.title = `theme: ${next || "auto"}`;
    renderDiagrams();
  }));

  // ── keys: "/" searches, Esc leaves the field
  const q = document.querySelector(".search input");
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing && q) { e.preventDefault(); q.focus(); q.select(); }
    else if (e.key === "Escape" && typing) document.activeElement.blur();
  });

  // ── filter menus apply on change
  document.querySelectorAll(".filter-form select").forEach((s) =>
    s.addEventListener("change", () => s.form.submit()));

  // ── copy: code blocks and [data-copy]
  const copy = async (text, el) => {
    try { await navigator.clipboard.writeText(text); } catch { return; }
    el.classList.add("ok");
    setTimeout(() => el.classList.remove("ok"), 1200);
  };
  document.querySelectorAll("[data-copy]").forEach((el) =>
    el.addEventListener("click", () => copy(el.dataset.copy, el)));
  document.querySelectorAll(".codeblock .copy").forEach((b) =>
    b.addEventListener("click", () => copy(b.closest(".codeblock").querySelector("pre").innerText, b)));

  // ── the outline follows the scroll
  const toc = [...document.querySelectorAll(".toc a[href^='#']")];
  if (toc.length && "IntersectionObserver" in window) {
    const byId = new Map(toc.map((a) => [a.hash.slice(1), a]));
    const targets = [...byId.keys()].map((id) => document.getElementById(id)).filter(Boolean);
    const visible = new Set();
    const mark = () => {
      const first = targets.find((t) => visible.has(t)) ||
        [...targets].reverse().find((t) => t.getBoundingClientRect().top < 120);
      toc.forEach((a) => a.classList.toggle("on", first && a.hash.slice(1) === first.id));
    };
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => en.isIntersecting ? visible.add(en.target) : visible.delete(en.target));
      mark();
    }, { rootMargin: "-64px 0px -60% 0px" });
    targets.forEach((t) => io.observe(t));
  }

  // ── something new on this node (a sync, an edit): offer a reload
  const seen = document.body.dataset.state;
  const toast = document.querySelector(".toast");
  if (seen && toast) {
    const poll = async () => {
      if (document.hidden) return;
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        const s = await r.json();
        if (String(s.events) !== seen) { toast.classList.add("show"); clearInterval(timer); }
      } catch { /* the server stopped: nothing to say */ }
    };
    const timer = setInterval(poll, 15000);
  }

  // ── mermaid, loaded only where there is a diagram; without network the source stays
  const diagrams = [...document.querySelectorAll("pre.mermaid")];
  diagrams.forEach((d) => { d.dataset.src = d.textContent; });
  let mermaid = null;
  async function renderDiagrams() {
    if (!diagrams.length) return;
    try {
      mermaid = mermaid || (await import(document.body.dataset.mermaid)).default;
    } catch {
      diagrams.forEach((d) => d.closest(".diagram")?.querySelector(".note")?.removeAttribute("hidden"));
      return;
    }
    mermaid.initialize({ startOnLoad: false, theme: isDark() ? "dark" : "neutral", securityLevel: "strict",
      fontFamily: getComputedStyle(document.body).fontFamily });
    diagrams.forEach((d) => { d.removeAttribute("data-processed"); d.textContent = d.dataset.src; });
    try { await mermaid.run({ nodes: diagrams }); } catch { /* a broken diagram keeps its source */ }
  }
  renderDiagrams();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (!root.dataset.theme) renderDiagrams();
  });
})();
