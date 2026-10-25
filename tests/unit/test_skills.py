from __future__ import annotations

import stat
from pathlib import Path

import pytest

import tua.skills as skills
from tua.errors import SkillError
from tua.paths import TuaPaths
from tua.skills import (
    SkillSource,
    builtin_source,
    compile_sources,
    parse_skill,
    resolve_rules,
    store_compiled,
    verify_compiled_file,
)

HEADER = "---\nschema_version: 1\n---\n\n"


def src(body: str, *, level: str = "project", label: str = "project:a.md") -> SkillSource:
    return SkillSource(level=level, label=label, data=(HEADER + body).encode())  # type: ignore[arg-type]


def test_parse_rules_and_classes() -> None:
    rules = parse_skill(src("## must: no-net\nNo network.\n\n## default: style\nBe concise.\n"))
    assert [(r.rule_class, r.rule_id, r.text) for r in rules] == [
        ("must", "no-net", "No network."),
        ("default", "style", "Be concise."),
    ]
    assert rules[0].line == 5


def test_code_fences_do_not_split_rules() -> None:
    body = "## default: docs\nExample:\n```md\n## not a heading\n```\nEnd.\n"
    (rule,) = parse_skill(src(body))
    assert "## not a heading" in rule.text
    assert rule.text.endswith("End.")


def test_subheadings_stay_inside_a_rule() -> None:
    (rule,) = parse_skill(src("## default: a\nText\n### Details\nMore\n"))
    assert "### Details" in rule.text


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("## must: x\ntext\n", "front matter"),
        ("---\nschema_version: 1\n## must: x\ntext\n", "not closed"),
        ("---\nschema_version: 2\n---\n## must: x\ntext\n", "schema_version"),
        ("---\nschema_version: 1\nowner: me\n---\n## must: x\ntext\n", "unknown front matter"),
        ("---\n[1, 2]\n---\n", "mapping"),
        (HEADER + "stray text\n## must: x\ntext\n", "outside a rule"),
        (HEADER + "## required: x\ntext\n", "malformed rule heading"),
        (HEADER + "## must: Bad_ID\ntext\n", "malformed rule heading"),
        (HEADER + "## must: x\n\n", "no text"),
        (HEADER + "## must: x\n```\nunclosed\n", "unclosed code fence"),
        (HEADER + "## must: tua.mine\ntext\n", "reserved"),
    ],
)
def test_malformed_skills_are_rejected(data: str, message: str) -> None:
    with pytest.raises(SkillError, match=message):
        parse_skill(SkillSource(level="project", label="project:a.md", data=data.encode()))


def test_non_utf8_is_rejected() -> None:
    with pytest.raises(SkillError, match="UTF-8"):
        parse_skill(SkillSource(level="project", label="p", data=b"\xff\xfe"))


def test_duplicate_ids_within_a_level() -> None:
    a = src("## default: x\none\n", label="project:a.md")
    b = src("## default: x\ntwo\n", label="project:b.md")
    with pytest.raises(SkillError, match="duplicate rule id 'x'"):
        resolve_rules([a, b])
    with pytest.raises(SkillError, match="duplicate"):
        resolve_rules([src("## default: x\none\n\n## must: x\ntwo\n")])


def test_project_default_overrides_global_default() -> None:
    g = src(
        "## default: lang\nEnglish.\n\n## default: other\nKeep.\n",
        level="global",
        label="global:g.md",
    )
    p = src("## default: lang\nVietnamese.\n", label="project:p.md")
    must, defaults, overrides = resolve_rules([builtin_source(), g, p])
    assert [r.rule_id for r in defaults] == ["other", "lang"]
    assert defaults[1].text == "Vietnamese."
    assert [(o.rule_id, o.replaced_source, o.winning_source) for o in overrides] == [
        ("lang", "global:g.md", "project:p.md")
    ]
    compiled = compile_sources([builtin_source(), g, p])
    assert "Vietnamese." in compiled.payload
    assert "English." not in compiled.payload
    assert all(r.rule_id.startswith("tua.") for r in must)


def test_must_rules_cannot_be_redefined() -> None:
    g = src("## must: secrets\nNever.\n", level="global", label="global:g.md")
    for body in ("## default: secrets\nMaybe.\n", "## must: secrets\nOther.\n"):
        with pytest.raises(SkillError, match="cannot be redefined"):
            resolve_rules([g, src(body)])


def test_class_change_is_a_conflict() -> None:
    g = src("## default: tests\nAdd tests.\n", level="global", label="global:g.md")
    with pytest.raises(SkillError, match="conflicting rule 'tests'"):
        resolve_rules([g, src("## must: tests\nAlways add tests.\n")])


def test_payload_orders_must_before_default_and_labels_precedence() -> None:
    compiled = compile_sources(
        [builtin_source(), src("## default: d\nPref.\n\n## must: m\nReq.\n")]
    )
    text = compiled.payload
    assert text.index("## MUST rules") < text.index("### m (project skill a.md)")
    assert text.index("### m (project skill a.md)") < text.index("## DEFAULT preferences")
    assert text.index("## DEFAULT preferences") < text.index("### d (project skill a.md)")
    assert "cannot override them" in text


def test_hashes_are_stable_and_distinct() -> None:
    sources = [builtin_source(), src("## default: d\nPref.\n")]
    first, second = compile_sources(sources), compile_sources(list(sources))
    assert first.source_hash == second.source_hash
    assert first.compiled_hash == second.compiled_hash
    assert first.source_hash != first.compiled_hash
    assert first.compiled_hash == skills.sha256_label(first.payload.encode())


def test_source_edit_changes_hashes() -> None:
    a = compile_sources([builtin_source(), src("## default: d\nPref.\n")])
    b = compile_sources([builtin_source(), src("## default: d\nPreference.\n")])
    assert a.source_hash != b.source_hash
    assert a.compiled_hash != b.compiled_hash


def test_whitespace_only_edit_changes_source_but_not_compiled_hash() -> None:
    a = compile_sources([builtin_source(), src("## default: d\nPref.\n")])
    b = compile_sources([builtin_source(), src("## default: d\nPref.   \n\n\n")])
    assert a.source_hash != b.source_hash
    assert a.compiled_hash == b.compiled_hash


def test_label_and_order_are_part_of_the_source_hash() -> None:
    one = src("## default: a\nA.\n", label="project:a.md")
    two = src("## default: b\nB.\n", label="project:b.md")
    renamed = src("## default: a\nA.\n", label="project:renamed.md")
    base = compile_sources([one, two]).source_hash
    assert compile_sources([two, one]).source_hash != base
    assert compile_sources([renamed, two]).source_hash != base


def test_compiler_version_is_part_of_both_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = [builtin_source(), src("## default: d\nPref.\n")]
    before = compile_sources(sources)
    monkeypatch.setattr(skills, "COMPILER_VERSION", skills.COMPILER_VERSION + 1)
    after = compile_sources(sources)
    assert before.source_hash != after.source_hash
    assert before.compiled_hash != after.compiled_hash


def test_store_compiled_is_private_content_addressed_and_verified(paths: TuaPaths) -> None:
    compiled = compile_sources([builtin_source()])
    path = store_compiled(paths, compiled)
    assert path.name == compiled.compiled_hash.removeprefix("sha256:") + ".md"
    assert path.read_text() == compiled.payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store_compiled(paths, compiled) == path  # idempotent
    path.write_text("tampered")
    with pytest.raises(SkillError, match="does not match"):
        verify_compiled_file(path, compiled.compiled_hash)
    assert store_compiled(paths, compiled) == path  # repaired from the in-memory payload
    verify_compiled_file(path, compiled.compiled_hash)


def test_skill_files_must_be_markdown(tmp_path: Path) -> None:
    from tua.config import GlobalConfig, parse_project_config

    (tmp_path / "rules.txt").write_text(HEADER + "## must: a\nb\n")
    config = parse_project_config(
        b"schema_version: 1\nname: x\nskills: [rules.txt]\n",
        root=tmp_path,
        config_path=tmp_path / "p.yaml",
    )
    with pytest.raises(SkillError, match="Markdown"):
        skills.compile_project_skills(config, GlobalConfig(tmp_path, (), None))
