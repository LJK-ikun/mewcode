#!/usr/bin/env python3
"""批量删除文件开头的特定注释"""

import os
from pathlib import Path

# 要删除的注释行
LINES_TO_REMOVE = [
    "# 来源：公众号@小林coding",
    "# 后端八股网站：xiaolincoding.com",
    "# Agent网站：xiaolinnote.com",
    "# 简历模版：jianli.xiaolinnote.com",
]

def should_remove_line(line: str) -> bool:
    """判断是否应该删除这一行"""
    stripped = line.strip()
    return stripped in LINES_TO_REMOVE

def process_file(file_path: Path) -> bool:
    """处理单个文件，返回是否修改了文件"""
    try:
        content = file_path.read_text(encoding='utf-8')
        lines = content.splitlines(keepends=True)

        # 删除开头的注释行
        new_lines = []
        skip_mode = True  # 开头模式，跳过注释

        for line in lines:
            if skip_mode:
                if should_remove_line(line):
                    continue  # 跳过这行
                elif line.strip() == "":
                    continue  # 跳过空行
                else:
                    skip_mode = False  # 遇到非注释行，退出跳过模式
                    new_lines.append(line)
            else:
                new_lines.append(line)

        # 如果内容有变化，写回文件
        new_content = ''.join(new_lines)
        if new_content != content:
            file_path.write_text(new_content, encoding='utf-8')
            return True
        return False
    except Exception as e:
        print(f"Error processing {file_path}: {e}")
        return False

def main():
    """主函数"""
    root = Path('.')
    modified_count = 0

    # 遍历所有 Python 文件
    for py_file in root.rglob('*.py'):
        # 跳过虚拟环境和 git 目录
        if any(part in py_file.parts for part in ['.venv', '.git', '__pycache__', 'venv']):
            continue

        if process_file(py_file):
            print(f"Modified: {py_file}")
            modified_count += 1

    print(f"\n总共修改了 {modified_count} 个文件")

if __name__ == '__main__':
    main()
