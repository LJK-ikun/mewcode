# 这段代码是Mewcode的指令加载器，加载 MEWCODE.md规则文件，并且支持语法@include 递归嵌入其他文件
# 按优先级从 3 个位置依次查找 MEWCODE.md，找到的全部拼在一起，用 --- 分隔，最后输出一整块文本，作为给大模型的系统提示词。
from __future__ import annotations

from pathlib import Path

MAX_INCLUDE_DEPTH = 5
INCLUDE_PREFIX = "@include "


# 本质：文本预处理器。输入md原文，输出替换完include之后的大文本

def process_includes(
    content: str,
    base_dir: Path,
    project_root: Path,
    depth: int = 0,
) -> str:
    
    if depth >= MAX_INCLUDE_DEPTH:
        return content

    resolved_root = project_root.resolve()
    lines = content.split("\n")
    result: list[str] = []


    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(INCLUDE_PREFIX):
            result.append(line)
            continue

        rel_path = stripped[len(INCLUDE_PREFIX) :].strip()
        abs_path = (base_dir / rel_path).resolve()

        try:
            abs_path.relative_to(resolved_root)
        except ValueError:
            result.append("<!-- @include blocked: path outside project -->")
            continue

        if not abs_path.exists() or not abs_path.is_file():
            result.append("<!-- @include skipped: file not found -->")
            continue

        included = abs_path.read_text(encoding="utf-8")
        processed = process_includes(included, abs_path.parent, project_root, depth + 1)
        result.append(processed)

    return "\n".join(result)

# load_instructions：从 3 个预设位置加载 MEWCODE.md，逐个展开里面@include引入的文件，再用---拼接所有内容，返回合并后的完整系统提示文本。
def load_instructions(project_root: str) -> str:
    root = Path(project_root)
    home = Path.home()

    paths = [
        root / "MEWCODE.md",
        root / ".mewcode" / "MEWCODE.md",
        home / ".mewcode" / "MEWCODE.md",
    ]

    sections: list[str] = []
    for path in paths:
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8")
            processed = process_includes(content, path.parent, root)
            sections.append(processed)

    return "\n---\n".join(sections)

