from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from mewcode.permissions.dangerous import DangerousCommandDetector, is_safe_command
from mewcode.permissions.modes import DecisionEffect, PermissionMode, mode_decide
from mewcode.permissions.rules import RuleEngine, extract_content
from mewcode.permissions.sandbox import PathSandbox
from mewcode.tools.base import Tool

# 在 Plan 模式下，这些工具默认允许直接跑。
# Agent：启动子探索 Agent
# ToolSearch：查找并加载新工具
# AskUserQuestion：向用户提问
# ExitPlanMode：退出规划模式
_PLAN_MODE_ALLOWED_TOOLS = frozenset({"Agent", "ToolSearch", "AskUserQuestion", "ExitPlanMode"})


@dataclass
class Decision:
    effect: DecisionEffect
    reason: str


class PermissionChecker:


    def __init__(
        self,
        # 危险bash命令检测器，用来扫描bash指令里的高位操作
        detector: DangerousCommandDetector,
        # 路径沙箱，管控文件读写，防止访问项目外的文件（比如读 C:/Windows、/etc/passwd）
        sandbox: PathSandbox,
        rule_engine: RuleEngine,
        # mode权限模式枚举（DEFAULT / PLAN 等）
        mode: PermissionMode = PermissionMode.DEFAULT,
    ) -> None:
        self.detector = detector
        self.sandbox = sandbox
        self.rule_engine = rule_engine
        self.mode = mode
        self.plan_file_path: str = ""


    # check()主函数，权限校验入口
    # 每次AI发起工具调用，框架调用check（tool, argument），分层一次校验，一旦命中就直接return，后面层级不再跑

    def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        content = extract_content(tool.name, arguments)

        # Layer 0: Plan 模式例外放行
        if self.mode == PermissionMode.PLAN:
            if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
                return Decision(effect="allow", reason="Plan mode: allowed tool")
            if tool.name in ("WriteFile", "EditFile") and content:
                if self._is_plan_file(content):
                    return Decision(effect="allow", reason="Plan mode: plan file write")

        # Layer 1: 安全的只读命令（自动放行）
        if tool.category == "command" and is_safe_command(content or ""):
            return Decision(effect="allow", reason="Safe read-only command")

        # Layer 1b: 危险命令黑名单（仅 Bash）
        if tool.category == "command":
            hit, reason = self.detector.detect(content)
            if hit:
                return Decision(effect="deny", reason=f"危险命令拦截: {reason}")

        # Layer 2: 路径沙箱（仅文件类工具）
        if tool.category in ("read", "write") and content:
            ok, reason = self.sandbox.check(content)
            if not ok:
                return Decision(effect="deny", reason=f"路径沙箱拦截: {reason}")

        # Layer 3: 规则引擎匹配
        rule_result = self.rule_engine.evaluate(tool.name, content)
        if rule_result == "allow":
            return Decision(effect="allow", reason="权限规则放行")
        if rule_result == "deny":
            return Decision(effect="deny", reason="权限规则拒绝")

        # Layer 4: 权限模式兜底判定
        effect = mode_decide(self.mode, tool.category)
        if effect == "allow":
            return Decision(effect="allow", reason=f"权限模式 {self.mode.value} 放行")
        if effect == "deny":
            return Decision(effect="deny", reason=f"权限模式 {self.mode.value} 拒绝")

        # Layer 5: 触发人工确认（HITL）
        return Decision(effect="ask", reason="需要用户确认")

    # 判断目标路径，是不是 plan模式允许写的plan文件
    def _is_plan_file(self, target_path: str) -> bool:
        if not self.plan_file_path or not target_path:
            return ".mewcode/plans/" in target_path
        try:
            abs_target = os.path.abspath(target_path)
            abs_plan = os.path.abspath(self.plan_file_path)
            if abs_target == abs_plan:
                return True
        except Exception:
            pass
        if os.path.basename(target_path) == os.path.basename(self.plan_file_path):
            return True
        return ".mewcode/plans/" in target_path
