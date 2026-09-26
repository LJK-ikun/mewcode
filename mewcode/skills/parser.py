# 技能（Skill）文件加载解析模块
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

log = logging.getLogger(__name__)

# 技能名称正则
VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]*$")
# 支持的执行模式
VALID_MODES = {"inline", "fork"}
# 上下文范围选项
VALID_CONTEXTS = {"full", "recent", "none"}


# 自定义异常。所有解析、校验、读文件失败，统一抛这个异常。上层代码可以捕获这个异常，
# 把友好提示展示到 UI（Textual 弹窗里），区分普通 IO 错误和「技能文件格式写错」。
class SkillParseError(Exception):
    pass


@dataclass
class SkillDef:
    name: str
    description: str
    prompt_body: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    mode: Literal["inline", "fork"] = "inline"
    model: str | None = None
    context: Literal["full", "recent", "none"] = "full"
    source_path: Path | None = None
    is_directory: bool = False


# 作用:把原始文本切割成[YAML元信息]和[提示词正文],返回(meta:dict,body:str)
def parse_frontmatter(raw: str) -> tuple[dict, str]:
    stripped = raw.lstrip()
    if not stripped.startswith("---"):
        raise SkillParseError("Missing YAML frontmatter (must start with ---)")

    end = stripped.find("---", 3)
    if end == -1:
        raise SkillParseError("Unclosed YAML frontmatter (missing closing ---)")

    yaml_block = stripped[3:end]
    body = stripped[end + 3:].lstrip("\n")

    try:
        meta = yaml.safe_load(yaml_block)
    except yaml.YAMLError as e:
        raise SkillParseError(f"Invalid YAML in frontmatter: {e}") from e

    if not isinstance(meta, dict):
        raise SkillParseError("Frontmatter must be a YAML mapping")

    return meta, body


# 校验yaml元数据
# 私有校验数据.检查YAML里的字段是否合法
def _validate_meta(meta: dict, source: str = "") -> None:
    ctx = f" in {source}" if source else ""

    if "name" not in meta:
        raise SkillParseError(f"Missing required field 'name'{ctx}")
    if "description" not in meta:
        raise SkillParseError(f"Missing required field 'description'{ctx}")

    name = meta["name"]
    if not isinstance(name, str) or not VALID_NAME_RE.match(name):
        raise SkillParseError(
            f"Invalid skill name '{name}'{ctx}: "
            "must be lowercase letters, digits, and hyphens, starting with a letter"
        )

    mode = meta.get("mode", "inline")
    if mode not in VALID_MODES:
        raise SkillParseError(f"Invalid mode '{mode}'{ctx}: must be one of {VALID_MODES}")

    context = meta.get("context", "full")
    if context not in VALID_CONTEXTS:
        raise SkillParseError(f"Invalid context '{context}'{ctx}: must be one of {VALID_CONTEXTS}")


# 调用链路：读文件 → 切 frontmatter → 校验元信息 → 构造 SkillDef 对象返回。
def parse_skill_file(path: Path) -> SkillDef:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SkillParseError(f"Cannot read skill file {path}: {e}") from e

    meta, body = parse_frontmatter(raw)
    _validate_meta(meta, str(path))

    return SkillDef(
        name=meta["name"],
        description=meta["description"],
        prompt_body=body,
        allowed_tools=meta.get("allowedTools", []),
        mode=meta.get("mode", "inline"),
        model=meta.get("model"),
        context=meta.get("context", "full"),
        source_path=path,
        is_directory=False,
    )

# 最简单的字符串替换
def substitute_arguments(prompt_body: str, args: str) -> str:
    return prompt_body.replace("$ARGUMENTS", args)
