# 工具启动
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewcode.tools.base import Tool

if TYPE_CHECKING:
    from mewcode.cache import FileCache


# ToolRegistry = 工具注册表
# 注册工具，按名字查找工具，启用/禁用工具包，处理【延迟工具（defer）工具】，批量导出schema（给LLM请求用）
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
        self._discovered: set[str] = set()

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    # agent收到 ToolCallComplete的时候，就调用这个
    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    # 判断工具是否可用：必须存在，并且不在金庸集合
    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    # 从禁用集合溢出。discard：就算名字不存在也不抛出异常
    def enable(self, name: str) -> None:
        self._disabled.discard(name)


    def disable(self, name: str) -> None:
        if name in self._tools:
            self._disabled.add(name)

    def enable_all(self) -> None:
        self._disabled.clear()

    # 标记grep延迟工具：现在可以暴露给LLM了
    def mark_discovered(self, name: str) -> None:
        self._discovered.add(name)

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered

    # 找出 已经注册，是延迟工具，还没被发现，没有被禁用的工具名字列表
    def get_deferred_tool_names(self) -> list[str]:
        return [
            name
            for name, tool in self._tools.items()
            if getattr(tool, "should_defer", False)
            and name not in self._discovered
            and name not in self._disabled
        ]

    # 延迟工具检索
    # 遍历所有工具，只保留should_defer=True并且没有被禁用的工具。
    # 全部转小写，大小写不敏感。
    # 打分规则（简单词袋检索，不是向量）：
    # 查询串完整包含在工具 name：+10 分（最高权重）
    # 查询串完整包含在描述：+5
    # 查询里面单个单词命中 name：+3
    # 查询里面单个单词命中描述：+1
    # 只保留 score>0 的。按分数从高到低排序，取前 max_results 个。
    # 调用tool.get_schema()拿到基础 schema，然后根据协议（anthropic /openai）转换格式，返回 schema 字典列表。
    def search_deferred(
        self, query: str, max_results: int, protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        query_lower = query.lower()
        scored: list[tuple[int, str, Tool]] = []
        for name, tool in self._tools.items():
            if not getattr(tool, "should_defer", False):
                continue
            if name in self._disabled:
                continue
            score = 0
            name_lower = name.lower()
            desc_lower = (tool.description or "").lower()
            if query_lower in name_lower:
                score += 10
            if query_lower in desc_lower:
                score += 5
            for word in query_lower.split():
                if word in name_lower:
                    score += 3
                if word in desc_lower:
                    score += 1
            if score > 0:
                scored.append((score, name, tool))
        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for _, _name, tool in scored[:max_results]:
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        return results

    # 按指定工具名字列表，批量取出延迟工具的schema
    def find_deferred_by_names(
        self, names: list[str], protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            if not getattr(tool, "should_defer", False):
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        return results

    # 返回全部注册的工具实例列表
    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    # 导出全部注册的工具的 schema 列表，按协议格式化
    def get_all_schemas(self, protocol: str = "anthropic") -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if name in self._disabled:
                continue
            if getattr(tool, "should_defer", False) and name not in self._discovered:
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                schemas.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                schemas.append(base)
        return schemas

# 默认注册表工厂
# 延迟导入:函数内部import,避免启动的时候一次性加载所有工具模块,防止循环导入
def create_default_registry(file_cache: FileCache | None = None, file_history: Any = None) -> ToolRegistry:
    from mewcode.tools.bash import Bash
    from mewcode.tools.edit_file import EditFile
    from mewcode.tools.file_state_cache import FileStateCache
    from mewcode.tools.glob import Glob
    from mewcode.tools.grep import Grep
    from mewcode.tools.read_file import ReadFile
    from mewcode.tools.write_file import WriteFile

    file_state_cache = FileStateCache()

    registry = ToolRegistry()
    registry.register(ReadFile(file_cache=file_cache, file_state_cache=file_state_cache))
    registry.register(WriteFile(file_cache=file_cache, file_history=file_history, file_state_cache=file_state_cache))
    registry.register(EditFile(file_cache=file_cache, file_history=file_history, file_state_cache=file_state_cache))
    registry.register(Bash())
    registry.register(Glob())
    registry.register(Grep())
    return registry
