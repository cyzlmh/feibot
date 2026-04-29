from pathlib import Path

from feibot.agent.skills import SkillsLoader


def test_openclaw_metadata_is_supported(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: demo-skill
description: demo
metadata: {"openclaw":{"always":true,"requires":{"bins":[],"env":[]}}}
---
Use this skill.
""",
        encoding="utf-8",
    )

    loader = SkillsLoader(workspace=tmp_path, builtin_skills_dir=tmp_path / "builtin-empty")

    assert loader._get_skill_meta("demo-skill").get("always") is True
    assert "demo-skill" in loader.get_always_skills()


def test_skills_loader_can_disable_builtin_skills(tmp_path: Path) -> None:
    local_skill = tmp_path / "skills" / "local-skill"
    local_skill.mkdir(parents=True)
    (local_skill / "SKILL.md").write_text("# local\n", encoding="utf-8")

    builtin_dir = tmp_path / "builtin-skills"
    builtin_skill = builtin_dir / "builtin-skill"
    builtin_skill.mkdir(parents=True)
    (builtin_skill / "SKILL.md").write_text("# builtin\n", encoding="utf-8")

    loader = SkillsLoader(
        workspace=tmp_path,
        builtin_skills_dir=builtin_dir,
        include_builtin=False,
    )

    names = [item["name"] for item in loader.list_skills(filter_unavailable=False)]
    assert names == ["local-skill"]
