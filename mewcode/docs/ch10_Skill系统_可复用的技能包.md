# CH10: Skill 系统 - 可复用的技能包

## 从一个真实场景说起

你在开发一个 AI Agent 项目，经常需要执行类似的任务：

```
任务 1：代码审查
    → 读取代码文件
    → 检查代码质量
    → 生成审查报告

任务 2：文档生成
    → 分析代码结构
    → 提取注释和类型
    → 生成 API 文档

任务 3：性能分析
    → 运行性能测试
    → 收集指标数据
    → 生成优化建议
```

**问题来了：**

1. **如何避免重复劳动？** 每次都要写相同的 prompt
2. **如何分享给他人？** 如何打包成可复用的组件
3. **如何管理依赖？** 技能可能需要特定的工具或文件
4. **如何组合技能？** 一个复杂任务可能需要多个技能协作
5. **如何版本管理？** 技能更新后如何追踪变化

这就是为什么需要 **Skill 技能包系统**：

- **可复用性**：一次编写，多次使用
- **可组合性**：小技能组合成大技能
- **可分享性**：打包成文件，分享给团队
- **可配置性**：通过参数适配不同场景

---

## 核心挑战：如何设计一个可扩展的技能包系统？

### 挑战 1：技能的表示和存储

```
问题 1：单文件 vs 多文件
    单文件技能: review.md (简单，但受限)
    多文件技能: review/ (复杂，但灵活)
        ├── SKILL.md        (技能定义)
        ├── examples/       (示例文件)
        ├── templates/      (模板文件)
        └── rules/          (规则配置)

问题 2：如何引用外部资源
    技能需要使用示例文件
    技能需要加载配置文件
    技能需要引用其他技能

问题 3：如何组织大型 prompt
    prompt 太长（超过 1000 行）
    prompt 包含多个模块
    需要按需加载不同部分
```

### 挑战 2：技能的参数化

```
问题 1：固定 prompt vs 动态 prompt
    固定: "审查代码质量"
    动态: "审查代码的{aspect}方面"

问题 2：参数的传递方式
    方式 1: $ARGUMENTS - 替换整个参数
    方式 2: $ASPECT, $TARGET - 命名参数
    方式 3: 结构化参数 (JSON)

问题 3：默认值和验证
    参数缺失时使用默认值
    参数类型验证
    参数范围检查
```

### 挑战 3：技能的作用域和隔离

```
问题 1：工具访问权限
    技能 A: 只能读取文件 (Read, Grep)
    技能 B: 可以修改文件 (Read, Write, Edit)
    技能 C: 可以执行命令 (Bash)

问题 2：上下文隔离
    inline 模式: 共享当前对话上下文
    fork 模式: 独立运行，不污染主对话

问题 3：资源访问范围
    项目级技能: 只能访问当前项目
    用户级技能: 可以跨项目使用
    内置技能: 系统提供的标准技能
```

---

## 系统的解决方案：分层架构

```
Layer 1: 技能定义（SkillDef）
    ├─ Frontmatter 元数据
    ├─ Prompt 正文
    ├─ 参数占位符
    └─ 工具权限声明

Layer 2: 技能加载（SkillLoader）
    ├─ 三层优先级（项目 > 用户 > 内置）
    ├─ 目录技能支持
    ├─ 热重载机制
    └─ 缓存管理

Layer 3: 技能执行（SkillExecutor）
    ├─ inline 模式（注入当前对话）
    ├─ fork 模式（独立运行）
    ├─ 上下文构建
    └─ 工具过滤

Layer 4: 技能组合（Composition）
    ├─ 技能链（Sequential）
    ├─ 技能并行（Parallel）
    └─ 条件执行（Conditional）
```

---

## Layer 1: 技能定义 - 从 Markdown 到结构化数据

### 基本格式

```markdown
---
name: review
description: 代码审查技能
mode: fork
context: recent
allowedTools:
  - Read
  - Grep
  - Bash
---

请审查以下代码，重点关注：

1. **代码质量**：可读性、命名规范、注释
2. **潜在 Bug**：边界条件、空值处理、异常处理
3. **性能问题**：算法复杂度、不必要的循环
4. **安全问题**：SQL 注入、XSS、路径遍历

$ARGUMENTS

审查完成后，生成结构化报告。
```

### Frontmatter 字段详解

```python
# skills/parser.py:23-32
@dataclass
class SkillDef:
    name: str                   # 技能名称（必填，唯一标识）
    description: str            # 技能描述（必填，用于补全提示）
    prompt_body: str = ""       # Prompt 正文（必填）
    allowed_tools: list[str] = []  # 允许的工具列表（空表示无限制）
    mode: Literal["inline", "fork"] = "inline"  # 执行模式
    model: str | None = None    # 指定模型（可选，如 "claude-opus-5"）
    context: Literal["full", "recent", "none"] = "full"  # 上下文模式
    source_path: Path | None = None  # 来源文件路径（自动填充）
    is_directory: bool = False  # 是否为目录技能（自动填充）
```

**字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | ✓ | 技能名称，用于 `/skill <name>` 调用 |
| `description` | string | ✓ | 一句话描述，显示在命令补全中 |
| `prompt_body` | string | ✓ | Prompt 正文，支持 `$ARGUMENTS` 占位符 |
| `allowed_tools` | list | ✗ | 工具白名单，空表示无限制 |
| `mode` | enum | ✗ | `inline`（注入当前对话）或 `fork`（独立运行） |
| `model` | string | ✗ | 指定 LLM 模型，如 `claude-opus-5` |
| `context` | enum | ✗ | `full`（完整摘要）、`recent`（最近5条）、`none`（无上下文） |
| `source_path` | Path | ✗ | 自动填充，指向技能文件路径 |
| `is_directory` | bool | ✗ | 自动填充，标记是否为目录技能 |

### 解析 Frontmatter

```python
# skills/parser.py:37-78
def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """
    解析 YAML Frontmatter
    返回: (元数据字典, 正文)
    """
    # 1. 检查是否以 --- 开头
    if not content.startswith("---"):
        return {}, content
    
    # 2. 查找结束标记
    lines = content.splitlines(keepends=True)
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    
    if end_idx == -1:
        return {}, content  # 没有结束标记，不是有效 frontmatter
    
    # 3. 分离 frontmatter 和正文
    fm_lines = lines[1:end_idx]
    body_lines = lines[end_idx + 1:]
    
    # 4. 解析 YAML
    fm_text = "".join(fm_lines)
    try:
        metadata = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise SkillParseError(f"Invalid YAML frontmatter: {e}")
    
    # 5. 返回元数据和正文
    body = "".join(body_lines).strip()
    return metadata, body
```

**解析流程**：

```
输入:
---
name: review
description: 代码审查
---

请审查代码...

步骤:
1. 检查 "---" 开头 → ✓
2. 查找结束 "---" → 第 4 行
3. 提取 YAML (第 2-3 行)
4. 解析 YAML → {"name": "review", "description": "代码审查"}
5. 提取正文 (第 5 行之后)

输出:
metadata = {"name": "review", "description": "代码审查"}
body = "请审查代码..."
```

### 构建 SkillDef

```python
# skills/parser.py:81-103
def parse_skill_file(path: Path) -> SkillDef:
    """从文件解析技能定义"""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        raise SkillParseError(f"Cannot read file: {e}")
    
    # 1. 解析 frontmatter
    metadata, body = parse_frontmatter(content)
    
    # 2. 验证必填字段
    if "name" not in metadata:
        raise SkillParseError("Missing required field: name")
    if "description" not in metadata:
        raise SkillParseError("Missing required field: description")
    
    # 3. 构建 SkillDef
    return SkillDef(
        name=metadata["name"],
        description=metadata["description"],
        prompt_body=body,
        allowed_tools=metadata.get("allowedTools", []),
        mode=metadata.get("mode", "inline"),
        model=metadata.get("model"),
        context=metadata.get("context", "full"),
        source_path=path,
        is_directory=False,  # 由调用方设置
    )
```

---

## Layer 2: 技能存储 - 单文件 vs 目录技能

### 单文件技能

**格式**：`<name>.md`

**示例**：`review.md`

```markdown
---
name: review
description: 代码审查技能
---

请审查代码...
```

**适用场景**：

- ✓ 简单技能，prompt 不超过 500 行
- ✓ 不需要额外资源文件
- ✓ 快速原型和测试

**限制**：

- ✗ 无法包含示例文件
- ✗ 无法模块化组织
- ✗ 难以管理大型 prompt

### 目录技能

**格式**：`<name>/SKILL.md`

**示例**：`review/`

```
review/
├── SKILL.md              # 技能定义（必须）
├── examples/             # 示例文件（可选）
│   ├── good.py          # 好的代码示例
│   └── bad.py           # 坏的代码示例
├── templates/            # 模板文件（可选）
│   └── report.md        # 报告模板
└── rules/                # 规则配置（可选）
    ├── quality.yaml     # 质量规则
    └── security.yaml    # 安全规则
```

**SKILL.md 内容**：

```markdown
---
name: review
description: 代码审查技能（增强版）
mode: fork
context: recent
allowedTools:
  - Read
  - Grep
  - Bash
---

# 代码审查技能

## 审查维度

请审查以下代码，重点关注以下维度：

### 1. 代码质量

参考好的代码示例：examples/good.py
参考坏的代码示例：examples/bad.py

- 可读性：命名是否清晰？
- 结构：是否符合 SOLID 原则？
- 注释：是否有必要的注释？

### 2. 潜在 Bug

加载安全规则：rules/security.yaml

- 边界条件：数组越界、空指针
- 异常处理：是否有合适的 try-catch
- 资源管理：是否正确关闭文件/连接

### 3. 性能问题

- 算法复杂度：O(n²) 可以优化为 O(n)?
- 不必要的循环：是否可以合并？
- 缓存机会：重复计算是否可以缓存？

## 目标文件

$ARGUMENTS

## 输出格式

使用以下模板生成报告：templates/report.md
```

**适用场景**：

- ✓ 复杂技能，prompt 超过 500 行
- ✓ 需要示例文件辅助理解
- ✓ 需要模块化组织（质量、安全、性能）
- ✓ 团队协作，多人维护同一技能

**优势**：

- ✓ 结构清晰，易于维护
- ✓ 可以包含任意资源文件
- ✓ 支持模块化组织
- ✓ 便于版本控制（Git）

### 加载逻辑

```python
# skills/loader.py:44-65
def _scan_directory(self, path: Path, source: str) -> list[SkillDef]:
    results: list[SkillDef] = []
    if not path.is_dir():
        return results
    
    for entry in sorted(path.iterdir()):
        try:
            # 情况 1：单文件技能 (skill.md)
            if entry.is_file() and entry.suffix == ".md":
                skill = parse_skill_file(entry)
                skill.source_path = entry
                skill.is_directory = False
                results.append(skill)
            
            # 情况 2：目录技能 (skill/SKILL.md)
            elif entry.is_dir():
                skill_md = entry / "SKILL.md"
                if skill_md.is_file():
                    skill = parse_skill_file(skill_md)
                    skill.source_path = skill_md
                    skill.is_directory = True
                    results.append(skill)
        
        except SkillParseError as e:
            log.warning("Skipping %s skill '%s': %s", source, entry.name, e)
    
    return results
```

**扫描规则**：

```
目录结构:
skills/
├── review.md              ← 单文件技能（✓ 加载）
├── test.md                ← 单文件技能（✓ 加载）
├── docs/                  ← 普通目录（✗ 跳过）
└── backend-interview/     ← 目录技能
    └── SKILL.md           ← （✓ 加载）

加载结果:
1. review (单文件)
2. test (单文件)
3. backend-interview (目录)
```

---

## Layer 3: 技能加载 - 三层优先级

### 优先级系统

```
优先级 1: 项目技能 (.mewcode/skills/)
    → 当前项目特定的技能
    → 可能覆盖用户技能

优先级 2: 用户技能 (~/.mewcode/skills/)
    → 用户个人的技能库
    → 可以跨项目使用

优先级 3: 内置技能 (builtin/)
    → 系统提供的标准技能
    → 所有用户都可用
```

**为什么需要三层？**

```
场景 1：项目特定的审查规则
    项目 A: 需要检查 React 特定的规范
    项目 B: 需要检查 Go 特定的规范
    → 使用项目级技能 (.mewcode/skills/review.md)

场景 2：个人偏好的技能
    用户喜欢用特定的代码审查风格
    用户有自己的文档生成模板
    → 使用用户级技能 (~/.mewcode/skills/)

场景 3：通用技能
    所有用户都需要基本的代码审查
    所有用户都需要文档生成
    → 使用内置技能
```

### 加载流程

```python
# skills/loader.py:24-41
def load_all(self) -> dict[str, SkillDef]:
    seen: dict[str, SkillDef] = {}
    
    # 1. 加载项目技能（优先级最高）
    for skill in self._scan_directory(self._project_dir, "project"):
        if skill.name not in seen:
            seen[skill.name] = skill
    
    # 2. 加载用户技能
    for skill in self._scan_directory(self._user_dir, "user"):
        if skill.name not in seen:  # 不覆盖项目技能
            seen[skill.name] = skill
    
    # 3. 加载内置技能
    for skill in self._load_builtins():
        if skill.name not in seen:  # 不覆盖用户技能
            seen[skill.name] = skill
    
    self._skills = seen
    self._cache = {k: v for k, v in seen.items()}
    return seen
```

**实际效果**：

```
项目技能: .mewcode/skills/review.md
用户技能: ~/.mewcode/skills/review.md
内置技能: builtin/review.md

加载结果:
    使用项目技能（优先级最高）

项目技能: (无)
用户技能: ~/.mewcode/skills/review.md
内置技能: builtin/review.md

加载结果:
    使用用户技能（优先级第二）

项目技能: (无)
用户技能: (无)
内置技能: builtin/review.md

加载结果:
    使用内置技能（优先级最低）
```

### 热重载机制

```python
# skills/loader.py:103-122
def get(self, name: str) -> SkillDef | None:
    """获取技能（支持热重载）"""
    skill = self._skills.get(name)
    if skill is None:
        return None
    
    # 如果有源文件路径，尝试重新加载
    if skill.source_path is not None:
        try:
            fresh = parse_skill_file(skill.source_path)
            fresh.is_directory = skill.is_directory
            
            # 更新缓存
            self._skills[name] = fresh
            self._cache[name] = fresh
            return fresh
        
        except SkillParseError as e:
            log.warning(
                "Hot-reload failed for skill '%s', using cached version: %s",
                name, e,
            )
            # 热重载失败，使用缓存版本
            return self._cache.get(name, skill)
    
    return skill
```

**热重载的好处**：

```
开发流程:
1. 编写技能: review.md
2. 测试: /skill review
3. 发现问题，修改 review.md
4. 再次测试: /skill review ← 自动加载最新版本
5. 无需重启 MewCode

异常处理:
1. 修改 review.md，引入语法错误
2. 测试: /skill review
3. 解析失败 → 使用缓存版本（不崩溃）
4. 修复错误，再次测试
5. 解析成功 → 使用最新版本
```

---

## Layer 4: 参数化技能 - 让技能更灵活

### 基础参数替换

**占位符**：`$ARGUMENTS`

```markdown
---
name: review
description: 代码审查
---

请审查以下文件：

$ARGUMENTS

关注代码质量、潜在 Bug 和性能问题。
```

**替换逻辑**：

```python
# skills/parser.py:104-105
def substitute_arguments(prompt_body: str, args: str) -> str:
    """将 prompt 中的 $ARGUMENTS 替换为用户参数"""
    return prompt_body.replace("$ARGUMENTS", args)
```

**实际效果**：

```python
# 用户输入: /skill review src/main.py

# 替换前:
"请审查以下文件：\n\n$ARGUMENTS\n\n关注代码质量..."

# 替换后:
"请审查以下文件：\n\nsrc/main.py\n\n关注代码质量..."
```

### 命名参数（扩展设计）

**问题**：单一 `$ARGUMENTS` 无法支持多个参数

```
场景：代码对比技能
    需要两个参数：
        - 旧文件路径
        - 新文件路径
    
    /skill diff src/main.py src/main_new.py
    
    如何区分？
```

**方案 1：位置参数（当前实现）**

```python
args = "src/main.py src/main_new.py"
parts = args.split()
old_file = parts[0]  # src/main.py
new_file = parts[1]  # src/main_new.py
```

**方案 2：命名参数（扩展设计）**

```markdown
---
name: diff
description: 代码对比
params:
  - name: old_file
    description: 旧文件路径
    required: true
  - name: new_file
    description: 新文件路径
    required: true
---

请对比以下两个文件：

旧文件: $old_file
新文件: $new_file

突出显示差异，并说明变更的影响。
```

**使用方式**：

```
/skill diff --old-file=src/main.py --new-file=src/main_new.py
```

**解析逻辑**（扩展）：

```python
def parse_named_arguments(args: str) -> dict[str, str]:
    """解析命名参数"""
    result = {}
    parts = args.split()
    
    for part in parts:
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.lstrip("-")
            result[key] = value
        else:
            # 位置参数
            if "default" not in result:
                result["default"] = part
    
    return result

# 示例
parse_named_arguments("--old-file=src/main.py --new-file=src/main_new.py")
→ {"old_file": "src/main.py", "new_file": "src/main_new.py"}
```

### 默认值和验证（扩展设计）

```markdown
---
name: review
description: 代码审查
params:
  - name: target
    description: 目标文件或目录
    required: true
  - name: depth
    description: 审查深度 (quick/normal/deep)
    required: false
    default: normal
    enum: [quick, normal, deep]
---

审查深度: $depth
目标: $target
```

**验证逻辑**：

```python
def validate_params(skill: SkillDef, args: dict[str, str]) -> None:
    """验证参数"""
    for param in skill.params:
        # 1. 检查必填参数
        if param.required and param.name not in args:
            raise ValueError(f"Missing required parameter: {param.name}")
        
        # 2. 应用默认值
        if param.name not in args and param.default is not None:
            args[param.name] = param.default
        
        # 3. 枚举值验证
        if param.enum and args[param.name] not in param.enum:
            raise ValueError(
                f"Invalid value for {param.name}: {args[param.name]}, "
                f"must be one of {param.enum}"
            )
```

---

## Layer 5: 技能组合 - 从小技能到大工作流

### 顺序执行（Sequential）

**场景**：代码审查 → 自动修复 → 再次审查

```markdown
---
name: review-fix-review
description: 审查-修复-再审查工作流
mode: inline
---

# 第一步：代码审查

/skill review $ARGUMENTS

# 第二步：等待用户确认

请问是否要自动修复发现的问题？

# 第三步：自动修复

/skill auto-fix $ARGUMENTS

# 第四步：再次审查

/skill review $ARGUMENTS
```

**执行流程**：

```
用户: /skill review-fix-review src/main.py

步骤 1: 执行 review 技能
    → 发现 5 个问题

AI: 发现 5 个问题，是否要自动修复？

用户: 是

步骤 2: 执行 auto-fix 技能
    → 修复 5 个问题

步骤 3: 执行 review 技能
    → 确认问题已修复

AI: 所有问题已修复，代码质量良好。
```

### 并行执行（Parallel）

**场景**：同时执行多个独立的审查维度

```markdown
---
name: full-review
description: 全面代码审查（并行）
mode: fork
---

请并行执行以下审查任务：

1. 质量审查: /skill review-quality $ARGUMENTS
2. 安全审查: /skill review-security $ARGUMENTS
3. 性能审查: /skill review-performance $ARGUMENTS

最后汇总所有审查结果。
```

**执行流程**：

```
用户: /skill full-review src/main.py

并行启动 3 个 fork Agent:
    Agent 1: 质量审查
    Agent 2: 安全审查
    Agent 3: 性能审查

等待所有 Agent 完成

汇总结果:
    质量: 8/10
    安全: 9/10
    性能: 7/10
    
    综合评分: 8/10
```

### 条件执行（Conditional）

**场景**：根据文件类型选择不同的审查策略

```markdown
---
name: smart-review
description: 智能代码审查
mode: inline
---

# 检测文件类型

请先检测文件类型：$ARGUMENTS

# 根据类型选择策略

- 如果是 Python 文件 → /skill review-python
- 如果是 JavaScript 文件 → /skill review-javascript
- 如果是 Go 文件 → /skill review-go
- 其他 → /skill review-generic

# 执行对应的审查
```

**执行流程**：

```
用户: /skill smart-review src/main.py

步骤 1: 检测文件类型
    → Python 文件

步骤 2: 选择策略
    → 使用 review-python 技能

步骤 3: 执行审查
    → 检查 PEP 8 规范
    → 检查类型注解
    → 检查异常处理
```

---

## 实际应用场景

### 场景 1：代码审查技能包

**目录结构**：

```
.mewcode/skills/review/
├── SKILL.md              # 主技能定义
├── dimensions/           # 审查维度
│   ├── quality.md       # 质量规则
│   ├── security.md      # 安全规则
│   └── performance.md   # 性能规则
├── examples/             # 示例代码
│   ├── good/            # 好的示例
│   │   ├── naming.py
│   │   └── structure.py
│   └── bad/             # 坏的示例
│       ├── naming.py
│       └── structure.py
└── templates/            # 报告模板
    └── report.md
```

**SKILL.md**：

```markdown
---
name: review
description: 企业级代码审查技能包
mode: fork
context: recent
allowedTools:
  - Read
  - Grep
  - Bash
---

# 代码审查技能包

## 审查流程

### 1. 读取目标代码

$ARGUMENTS

### 2. 质量审查

参考规则: dimensions/quality.md
好的示例: examples/good/
坏的示例: examples/bad/

检查点:
- 命名规范
- 代码结构
- 注释质量
- 可读性

### 3. 安全审查

参考规则: dimensions/security.md

检查点:
- SQL 注入
- XSS 攻击
- 路径遍历
- 敏感信息泄露

### 4. 性能审查

参考规则: dimensions/performance.md

检查点:
- 算法复杂度
- 循环优化
- 缓存机会
- 资源管理

## 输出格式

使用模板: templates/report.md

生成结构化报告，包括：
- 总体评分
- 各维度得分
- 具体问题列表
- 修复建议
```

**使用**：

```
用户: /skill review src/api/user.py

AI 执行:
1. 读取 src/api/user.py
2. 加载 dimensions/quality.md
3. 加载 dimensions/security.md
4. 加载 dimensions/performance.md
5. 参考 examples/ 中的示例
6. 生成基于 templates/report.md 的报告

输出:
## 代码审查报告

文件: src/api/user.py
总体评分: 7.5/10

### 质量 (8/10)
✓ 命名规范良好
✓ 代码结构清晰
✗ 缺少部分注释

### 安全 (6/10)
✗ 第 42 行存在 SQL 注入风险
✗ 第 58 行未验证用户输入

### 性能 (8/10)
✓ 算法复杂度合理
✗ 第 35 行可以使用缓存优化
```

### 场景 2：文档生成技能包

**目录结构**：

```
~/.mewcode/skills/docs-gen/
├── SKILL.md
├── templates/
│   ├── api.md           # API 文档模板
│   ├── class.md         # 类文档模板
│   └── function.md      # 函数文档模板
└── styles/
    ├── google.yaml      # Google 风格
    └── numpy.yaml       # NumPy 风格
```

**SKILL.md**：

```markdown
---
name: docs-gen
description: 智能文档生成器
mode: fork
context: none
allowedTools:
  - Read
  - Grep
  - Write
---

# 文档生成器

## 输入

目标文件: $ARGUMENTS

## 流程

### 1. 分析代码结构

- 提取所有类定义
- 提取所有函数定义
- 提取类型注解
- 提取现有注释

### 2. 生成文档

- 对于类 → 使用 templates/class.md
- 对于函数 → 使用 templates/function.md
- 对于 API → 使用 templates/api.md

### 3. 应用风格

默认风格: Google (styles/google.yaml)

### 4. 写入文档

生成 docs/ 目录下的 Markdown 文件
```

**使用**：

```
用户: /skill docs-gen src/api/

AI 执行:
1. 扫描 src/api/ 目录
2. 分析每个文件的结构
3. 生成对应的文档
4. 写入 docs/api/ 目录

输出:
已生成以下文档:
- docs/api/user.md
- docs/api/auth.md
- docs/api/product.md

总计: 15 个类, 42 个函数
```

### 场景 3：面试准备技能包

**目录结构**：

```
~/.mewcode/skills/interview/
├── SKILL.md
├── topics/               # 面试主题
│   ├── algorithms.md
│   ├── system-design.md
│   ├── databases.md
│   └── networking.md
├── questions/            # 题库
│   ├── easy/
│   ├── medium/
│   └── hard/
└── solutions/            # 参考解答
    ├── python/
    └── javascript/
```

**SKILL.md**：

```markdown
---
name: interview
description: 技术面试准备助手
mode: inline
context: full
---

# 技术面试准备

## 选择主题

可用主题:
1. 算法 (topics/algorithms.md)
2. 系统设计 (topics/system-design.md)
3. 数据库 (topics/databases.md)
4. 网络 (topics/networking.md)

用户选择: $ARGUMENTS

## 流程

### 1. 随机抽取题目

从对应难度的题库中随机抽取: questions/{difficulty}/

### 2. 用户作答

给用户 30 分钟作答

### 3. 审查答案

- 对比参考解答: solutions/
- 指出优点和不足
- 给出改进建议

### 4. 知识点扩展

根据答案质量，扩展相关知识点
```

**使用**：

```
用户: /skill interview 算法 medium

AI:
好的，我从算法题库（medium）中随机抽取了一道题：

## 题目: 最长递增子序列

给定一个整数数组，找出最长递增子序列的长度。

示例:
输入: [10,9,2,5,3,7,101,18]
输出: 4 (子序列 [2,3,7,101])

请开始作答，我会在 30 分钟后审查你的代码。

用户: (提交代码)

AI:
很好！让我审查你的代码...

优点:
✓ 使用动态规划，复杂度 O(n²)
✓ 代码清晰易懂
✓ 边界条件处理正确

改进建议:
✗ 可以优化到 O(n log n)
✗ 建议使用二分查找

参考解答: solutions/python/longest-increasing-subsequence.py

知识点扩展:
- 动态规划的状态转移方程
- 二分查找优化技巧
- 贪心 + 二分的组合应用
```

---

## 代码亮点：设计模式与最佳实践

### 亮点 1：三层优先级的覆盖机制

```python
# ✅ 好做法：按优先级加载，不覆盖已有
def load_all(self) -> dict[str, SkillDef]:
    seen: dict[str, SkillDef] = {}
    
    # 优先级 1: 项目技能
    for skill in self._scan_directory(self._project_dir, "project"):
        if skill.name not in seen:  # 只添加，不覆盖
            seen[skill.name] = skill
    
    # 优先级 2: 用户技能
    for skill in self._scan_directory(self._user_dir, "user"):
        if skill.name not in seen:  # 不覆盖项目技能
            seen[skill.name] = skill
    
    # 优先级 3: 内置技能
    for skill in self._load_builtins():
        if skill.name not in seen:  # 不覆盖用户技能
            seen[skill.name] = skill
    
    return seen

# ❌ 坏做法：后面的覆盖前面的
def load_all(self) -> dict[str, SkillDef]:
    skills = {}
    
    skills.update(self._load_builtins())      # 内置
    skills.update(self._load_user())          # 用户覆盖内置
    skills.update(self._load_project())       # 项目覆盖用户
    
    return skills  # 优先级反了！
```

**好处**：

- **明确优先级**：项目 > 用户 > 内置
- **不覆盖已有**：先出现的优先级高
- **符合直觉**：越靠近项目，优先级越高

### 亮点 2：热重载的容错降级

```python
# ✅ 好做法：重载失败时使用缓存
def get(self, name: str) -> SkillDef | None:
    skill = self._skills.get(name)
    if skill is None:
        return None
    
    if skill.source_path is not None:
        try:
            # 尝试重新加载
            fresh = parse_skill_file(skill.source_path)
            fresh.is_directory = skill.is_directory
            
            # 更新缓存
            self._skills[name] = fresh
            self._cache[name] = fresh
            return fresh
        
        except SkillParseError as e:
            # 重载失败，使用缓存
            log.warning("Hot-reload failed, using cached version")
            return self._cache.get(name, skill)
    
    return skill

# ❌ 坏做法：重载失败就崩溃
def get(self, name: str) -> SkillDef | None:
    skill = self._skills.get(name)
    if skill.source_path is not None:
        return parse_skill_file(skill.source_path)  # 失败会崩溃
    return skill
```

**好处**：

- **开发友好**：修改技能时语法错误不会导致系统崩溃
- **优雅降级**：重载失败时仍能使用旧版本
- **双缓存机制**：_skills（当前）+ _cache（备份）

### 亮点 3：目录技能的自动识别

```python
# ✅ 好做法：自动识别单文件和目录技能
def _scan_directory(self, path: Path, source: str) -> list[SkillDef]:
    results: list[SkillDef] = []
    
    for entry in sorted(path.iterdir()):
        # 情况 1：单文件技能
        if entry.is_file() and entry.suffix == ".md":
            skill = parse_skill_file(entry)
            skill.is_directory = False
            results.append(skill)
        
        # 情况 2：目录技能
        elif entry.is_dir():
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                skill = parse_skill_file(skill_md)
                skill.is_directory = True  # 标记为目录技能
                results.append(skill)
    
    return results

# ❌ 坏做法：只支持单文件
def _scan_directory(self, path: Path, source: str) -> list[SkillDef]:
    results = []
    for entry in path.glob("*.md"):  # 只扫描 .md 文件
        results.append(parse_skill_file(entry))
    return results
```

**好处**：

- **灵活性**：同时支持单文件和目录技能
- **自动标记**：is_directory 字段自动设置
- **易于扩展**：未来可以基于 is_directory 实现不同行为

### 亮点 4：Frontmatter 的容错解析

```python
# ✅ 好做法：Frontmatter 不是必须的
def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    # 不是以 --- 开头，直接返回空元数据
    if not content.startswith("---"):
        return {}, content
    
    # 查找结束标记
    lines = content.splitlines(keepends=True)
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    
    # 没有结束标记，不是有效 frontmatter
    if end_idx == -1:
        return {}, content
    
    # 解析 YAML
    fm_text = "".join(lines[1:end_idx])
    try:
        metadata = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise SkillParseError(f"Invalid YAML: {e}")
    
    body = "".join(lines[end_idx + 1:]).strip()
    return metadata, body

# ❌ 坏做法：强制要求 frontmatter
def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        raise SkillParseError("Missing frontmatter")
    # ...
```

**好处**：

- **向后兼容**：旧的纯 Markdown 技能仍可用
- **容错性**：格式错误时返回空元数据，不崩溃
- **灵活性**：可以逐步迁移到新格式

### 亮点 5：参数替换的简单性

```python
# ✅ 好做法：简单直接的字符串替换
def substitute_arguments(prompt_body: str, args: str) -> str:
    return prompt_body.replace("$ARGUMENTS", args)

# ❌ 坏做法：过度设计的模板引擎
def substitute_arguments(prompt_body: str, args: str) -> str:
    # 解析复杂的模板语法
    template = Template(prompt_body)
    variables = parse_variables(args)
    return template.render(**variables)
```

**好处**：

- **简单**：一行代码，无需引入模板引擎
- **高效**：字符串替换，性能好
- **够用**：满足 90% 的场景
- **可扩展**：未来需要时再升级

### 亮点 6：扫描时的异常处理

```python
# ✅ 好做法：单个技能失败不影响其他
def _scan_directory(self, path: Path, source: str) -> list[SkillDef]:
    results: list[SkillDef] = []
    
    for entry in sorted(path.iterdir()):
        try:
            # 解析技能
            if entry.is_file() and entry.suffix == ".md":
                skill = parse_skill_file(entry)
                results.append(skill)
            elif entry.is_dir():
                skill_md = entry / "SKILL.md"
                if skill_md.is_file():
                    skill = parse_skill_file(skill_md)
                    results.append(skill)
        
        except SkillParseError as e:
            # 记录警告，继续处理其他技能
            log.warning("Skipping %s skill '%s': %s", source, entry.name, e)
    
    return results

# ❌ 坏做法：一个失败，全部失败
def _scan_directory(self, path: Path, source: str) -> list[SkillDef]:
    results = []
    for entry in sorted(path.iterdir()):
        # 没有 try-except，解析失败会中断整个加载
        if entry.suffix == ".md":
            results.append(parse_skill_file(entry))
    return results
```

**好处**：

- **鲁棒性**：一个技能格式错误不影响其他
- **用户友好**：显示警告，不是错误
- **部分可用**：即使部分技能有问题，其他仍可用

### 亮点 7：source_path 的自动记录

```python
# ✅ 好做法：自动记录源文件路径
def _scan_directory(self, path: Path, source: str) -> list[SkillDef]:
    for entry in sorted(path.iterdir()):
        if entry.is_file() and entry.suffix == ".md":
            skill = parse_skill_file(entry)
            skill.source_path = entry  # 记录路径
            skill.is_directory = False
            results.append(skill)

# ❌ 坏做法：不记录源路径
def _scan_directory(self, path: Path, source: str) -> list[SkillDef]:
    for entry in sorted(path.iterdir()):
        if entry.is_file() and entry.suffix == ".md":
            skill = parse_skill_file(entry)  # 缺少 source_path
            results.append(skill)
```

**好处**：

- **支持热重载**：知道文件路径，可以重新加载
- **调试友好**：错误时可以显示文件路径
- **追溯能力**：知道技能来自哪个文件

---

## 技能包的最佳实践

### 1. 单一职责原则

```
✓ 好的技能：代码审查
    → 只做一件事：审查代码

✗ 坏的技能：代码审查 + 自动修复 + 重构
    → 做太多事情，难以维护
```

### 2. 清晰的命名

```
✓ 好的命名：
    review-security  （安全审查）
    docs-gen-api     （API 文档生成）
    test-unit        （单元测试）

✗ 坏的命名：
    do-stuff         （太模糊）
    skill1           （无意义）
    my-skill         （不描述功能）
```

### 3. 完整的描述

```
✓ 好的描述：
    "审查代码安全问题，包括 SQL 注入、XSS、CSRF"

✗ 坏的描述：
    "代码审查"  （太简略）
    "审查"      （太短）
```

### 4. 合理的工具限制

```
✓ 好的限制：
    代码审查技能：[Read, Grep, Bash]
    → 只读取，不修改

✗ 坏的限制：
    代码审查技能：[Read, Write, Edit, Delete, Bash]
    → 权限过大，可能误操作
```

### 5. 清晰的参数说明

```
✓ 好的参数说明：
    """
    使用方法:
    /skill review <文件路径>
    
    示例:
    /skill review src/main.py
    /skill review src/api/
    """

✗ 坏的参数说明：
    """
    $ARGUMENTS
    """
```

### 6. 使用示例和模板

```
✓ 好的技能包：
    review/
    ├── SKILL.md
    ├── examples/      ← 提供示例
    └── templates/     ← 提供模板

✗ 坏的技能包：
    review.md          ← 只有一个文件，没有示例
```

---

## 总结

Skill 系统解决了什么问题：

1. **Layer 1 (技能定义)**：Frontmatter + Markdown，结构化定义
2. **Layer 2 (技能存储)**：单文件 vs 目录，灵活组织
3. **Layer 3 (技能加载)**：三层优先级 + 热重载
4. **Layer 4 (参数化)**：$ARGUMENTS 占位符，动态替换
5. **Layer 5 (技能组合)**：顺序、并行、条件执行

**代码亮点**：

- ✨ **三层优先级**：项目 > 用户 > 内置，覆盖机制清晰
- ✨ **热重载容错**：修改后立即生效，失败时降级到缓存
- ✨ **双技能格式**：单文件（简单）+ 目录（复杂），自动识别
- ✨ **容错解析**：Frontmatter 不是必须，格式错误不崩溃
- ✨ **简单参数**：字符串替换，无需模板引擎
- ✨ **异常隔离**：单个技能失败不影响其他
- ✨ **自动记录**：source_path 支持热重载和调试

**核心原则**：

- **可复用性**：一次编写，多次使用
- **可组合性**：小技能组合成大工作流
- **可分享性**：文件即技能，易于分享
- **可配置性**：参数化，适配不同场景

**下一章预告：CH11 工具系统与 MCP 协议**

Agent 如何调用外部工具？MCP 协议如何实现标准化工具接入？我们将深入 MewCode 的工具架构。