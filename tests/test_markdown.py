from cairn.markdown import (
    dump_front_matter,
    first_sentences,
    is_placeholder,
    parse_front_matter,
    pending_items,
    section_text,
)


def test_front_matter_roundtrip():
    meta = {"tipo": "ref", "nota": "falta: el GRANT # ojo", "origen": ["../_done/a.md", "../_wip/b.md"],
            "estado": "true", "extra": "x"}
    text = dump_front_matter(meta) + "# t\n"
    back, body = parse_front_matter(text)
    assert back == meta and body == "# t\n"
    assert text.index("tipo") < text.index("estado") < text.index("origen") < text.index("extra")


def test_without_front_matter():
    assert parse_front_matter("# hola\n") == (None, "# hola\n")
    assert parse_front_matter("---\ntipo: bug\n") == (None, "---\ntipo: bug\n")


def test_first_sentences():
    t = "Se leyó AGENTS.md y el dominio api.example.com. Versión 1.5 lista. Tercera."
    assert first_sentences(t, 1) == "Se leyó AGENTS.md y el dominio api.example.com."
    assert first_sentences(t, 2) == "Se leyó AGENTS.md y el dominio api.example.com. Versión 1.5 lista."
    assert first_sentences("", 2) == ""


def test_sections_and_placeholders():
    body = "## TL;DR\n\n<!-- x -->\nHecho.\n\n---\n\n## Pendientes\n\n- [ ]\n- [ ] algo\n- [x] listo\n"
    assert section_text(body, "tl;dr") == "Hecho."
    assert is_placeholder("```bash\n# para retomar o re-diagnosticar\n```\n")
    assert not is_placeholder("curl x")
    assert pending_items(body) == [(11, "- [ ] algo")]
