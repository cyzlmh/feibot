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
