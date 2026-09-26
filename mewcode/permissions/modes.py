# 这一段定义 权限模式枚举 + 模式决策矩阵 _MODE_MATRIX + mode_decide 函数
from __future__ import annotations

from enum import Enum
from typing import Literal

from mewcode.tools.base import ToolCategory


# DecisionEffect 类型别名，只能是这三个字符串之一
DecisionEffect = Literal["allow", "deny", "ask"]


# PermissionMode 枚举，代表MewCode全局权限模式，一共6种模式
class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypassPermissions"
    CUSTOM = "custom"
    DONT_ASK = "dontAsk"


_MODE_MATRIX: dict[PermissionMode, dict[ToolCategory, DecisionEffect]] = {
    PermissionMode.DEFAULT: {"read": "allow", "write": "ask", "command": "ask"},
    PermissionMode.ACCEPT_EDITS: {"read": "allow", "write": "allow", "command": "ask"},
    PermissionMode.PLAN: {"read": "allow", "write": "ask", "command": "ask"},
    PermissionMode.BYPASS: {"read": "allow", "write": "allow", "command": "allow"},
    PermissionMode.CUSTOM: {"read": "ask", "write": "ask", "command": "ask"},
    PermissionMode.DONT_ASK: {"read": "allow", "write": "allow", "command": "allow"},
}


def mode_decide(mode: PermissionMode, category: ToolCategory) -> DecisionEffect:
    return _MODE_MATRIX[mode][category]
