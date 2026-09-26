# 自定义权限规则引擎模块
# 再权限校验链路属于 Layer 3
# 当走到这一层时，会读取yaml权限规则文件，用通配符匹配工具调用；匹配成功就返回allow/deny；
# 没有匹配返回None，交给下一层继续判断

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

import yaml

# 这个变量的
Effect = Literal["allow", "deny"]

# 正则表达式,用来校验规则字符串格式
_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")

# 字典常量
_CONTENT_FIELDS: dict[str, str] = {
    "Bash": "command",
    "ReadFile": "file_path",
    "WriteFile": "file_path",
    "EditFile": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}


# 
@dataclass(frozen=True)
class Rule:
    tool_name: str
    pattern: str
    effect: Effect

    # 判断当前工具调用，是否匹配这条规则（用fn match通配比对）
    def matches(self, tool_name: str, content: str) -> bool:
        if self.tool_name != tool_name:
            return False
        return fnmatch(content, self.pattern)

# parse_rule 解析原始规则字符串，用上面的规则校验语法，解析成功就构造Rule实例；
# 语法错误就抛异常
def parse_rule(raw: str, effect: Effect) -> Rule:
    m = _RULE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"无效的规则语法: {raw}")
    return Rule(tool_name=m.group(1), pattern=m.group(2), effect=effect)

# 根据工具名,从工具参数字典提取要匹配的内容字符串.工具不在映射表里返回空串
def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    field = _CONTENT_FIELDS.get(tool_name)
    if field is None:
        return ""
    return str(arguments.get(field, ""))

# 读取一个yaml规则文件,把yaml里的规则条目解析成Rule对象列表返回.文件不存在或格式错误就返回空列表
def _load_rules_file(path: Path) -> list[Rule]:
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rule_str = entry.get("rule", "")
        effect = entry.get("effect", "")
        if effect not in ("allow", "deny"):
            continue
        try:
            rules.append(parse_rule(rule_str, effect))
        except ValueError:
            continue
    return rules

# 规则引擎主类。支持三层规则文件：user（用户全局）、project（项目）、local（本地私有）
class RuleEngine:


    def __init__(
        self,
        user_rules_path: Path | None = None,
        project_rules_path: Path | None = None,
        local_rules_path: Path | None = None,
    ) -> None:
        self._user_path = user_rules_path
        self._project_path = project_rules_path
        self._local_path = local_rules_path

    # 依次加载这三层yaml文件,返回三层规则列表
    def _load_tiers(self) -> list[list[Rule]]:
        tiers: list[list[Rule]] = []
        for p in (self._user_path, self._project_path, self._local_path):
            tiers.append(_load_rules_file(p) if p else [])
        return tiers


    # 核心方法. 按优先级遍历所有规则,同文件倒序遍历,后面写的规则有限
    def evaluate(self, tool_name: str, content: str) -> Effect | None:
        for rules in self._load_tiers():
            for rule in reversed(rules):
                if rule.matches(tool_name, content):
                    return rule.effect
        return None


    # 动态新增一条规则,追加写入本地私有规则yaml文件
    def append_local_rule(self, rule: Rule) -> None:
        if self._local_path is None:
            return
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_rules_file(self._local_path)
        existing.append(rule)
        entries = [{"rule": f"{r.tool_name}({r.pattern})", "effect": r.effect} for r in existing]
        self._local_path.write_text(yaml.dump(entries, allow_unicode=True), encoding="utf-8")
