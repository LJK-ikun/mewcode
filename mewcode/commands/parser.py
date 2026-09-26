from __future__ import annotations

from mewcode.commands.registry import CommandRegistry


# 把用户输入的原始文本,解析成命令名,参数是否合法斜杠命令,是处理斜杠命令的第一步
def parse_command(text: str) -> tuple[str, str, bool]:
    text = text.strip()
    if not text.startswith("/"):
        return "", "", False
    text = text[1:]
    if not text:
        return "", "", True
    parts = text.split(None, 1)
    name = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args, True


# 用户输入前缀时(比如输了/he),从所有命令里找匹配的命令,返回[给用户看的友好文本+实际要填充的命令],最多返回8条(避免界面太长)
def complete(registry: CommandRegistry, prefix: str) -> list[tuple[str, str]]:
    """返回匹配命令的 (display_text, command_value) 列表。"""
    prefix = prefix.lstrip("/")
    seen: set[str] = set()
    matches: list[tuple[str, str]] = []
    for cmd in registry.list_commands():
        if cmd.name in seen:
            continue
        if cmd.name.startswith(prefix) or any(a.startswith(prefix) for a in cmd.aliases):
            seen.add(cmd.name)
            desc = cmd.description
            if len(desc) > 30:
                desc = desc[:28] + "…"
            desc = desc.replace("[", "\\[")
            display = f"/{cmd.name:<16} — {desc}"
            matches.append((display, "/" + cmd.name))
    matches.sort(key=lambda x: x[1])
    return matches[:8]

