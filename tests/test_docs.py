"""The docs list every `automation` switch with the default `core/config.py` gives it."""

import re
from pathlib import Path

import yaml

from apm.core.config import AutomationConfig

ROOT = Path(__file__).parent.parent
ROW_RE = re.compile(r"^\| `(\w+)` \| `([^`]+)` \|", re.MULTILINE)


def _defaults() -> dict[str, object]:
    return {name: field.default for name, field in AutomationConfig.model_fields.items()}


def test_user_guide_lists_every_automation_switch_with_its_default() -> None:
    guide = (ROOT / "docs" / "user-guide.md").read_text(encoding="utf-8")
    section = guide.split("### Automation", 1)[1].split("\n## ", 1)[0]
    documented = {key: yaml.safe_load(value) for key, value in ROW_RE.findall(section)}
    assert documented == _defaults()


def test_plans_md_apm_yaml_lists_every_automation_switch_with_its_default() -> None:
    plans = (ROOT / "plans.md").read_text(encoding="utf-8")
    section = plans.split("### 3.4 `apm.yaml`", 1)[1]
    example = yaml.safe_load(section.split("```yaml\n", 1)[1].split("```", 1)[0])
    assert example["automation"] == _defaults()
