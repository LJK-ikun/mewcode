# CH11: Hook系统 - Agent行为扩展机制

## 从一次危险操作说起

你问 AI："清理一下这个目录"

AI 回复："好的，让我清理"，然后：

```
[调用工具: Bash]
参数: {"command": "rm -rf /"}
正在执行...
```

**等等！这会删除整个系统！**

但如果你的系统里配置了这样一个Hook：

```yaml
- id: "block-dangerous-rm"
  event: "pre_tool_use"
  if: 'tool == "Bash" && args.command =~ /rm\s+-rf\s+\//'
  action:
    type: "command"
    command: "echo 'BLOCKED: dangerous rm -rf / command'"
  reject: true
```

那么执行会被拦截：

```
[Hook拦截]
工具: Bash
原因: BLOCKED: dangerous rm -rf / command
状态: 已阻止执行
```

AI看到拒绝信息："抱歉，这个命令太危险了，我换个方式"

这就是**Hook系统**要解决的问题：**在Agent生命周期的关键节点注入自定义逻辑**。

---

## Hook系统能做什么？

### 场景1：安全防护
```yaml
# 阻止危险命令
- event: "pre_tool_use"
  if: 'tool == "Bash" && args.command =~ /rm\s+-rf/'
  reject: true
```

### 场景2：自动化任务
```yaml
# Python文件保存后自动格式化
- event: "post_tool_use"
  if: 'tool == "WriteFile" && args.file_path ~= "*.py"'
  action:
    type: "command"
    command: "black $FILE_PATH"
  async: true
```

### 场景3：外部集成
```yaml
# 错误发生时通知Slack
- event: "error"
  action:
    type: "http"
    url: "https://hooks.slack.com/services/xxx"
    body: '{"text": "Error: $ERROR"}'
```

### 场景4：上下文注入
```yaml
# 会话开始时注入项目信息
- event: "session_start"
  action:
    type: "prompt"
    message: "This is a Django project. Follow Django best practices."
  once: true
```

---

## 第一步：Hook的生命周期事件

### 什么是生命周期事件？

想象Agent是一个人，他的工作过程有很多关键时刻：

```
早上醒来（session_start）
    ↓
开始处理任务（turn_start）
    ↓
准备使用工具（pre_tool_use）
    ↓
工具执行完成（post_tool_use）
    ↓
任务完成（turn_end）
    ↓
晚上休息（session_end）
```

Hook系统在这些时刻插入你的自定义逻辑。

### 15个生命周期事件

```python
# mewcode/hooks/events.py
class LifecycleEvent(StrEnum):
    # 会话级别（整个对话）
    SESSION_START = "session_start"      # 对话开始
    SESSION_END = "session_end"          # 对话结束
    
    # 轮次级别（每一轮AI思考和回复）
    TURN_START = "turn_start"            # 开始新一轮
    TURN_END = "turn_end"                # 一轮结束
    
    # 工具级别（每次工具调用）
    PRE_TOOL_USE = "pre_tool_use"        # 工具执行前（可拦截）
    POST_TOOL_USE = "post_tool_use"      # 工具执行后
    
    # 消息级别（与LLM通信）
    PRE_SEND = "pre_send"                # 发送请求到LLM前
    POST_RECEIVE = "post_receive"        # 收到LLM响应后
    
    # 系统级别（特殊事件）
    STARTUP = "startup"                  # 系统启动
    SHUTDOWN = "shutdown"                # 系统关闭
    ERROR = "error"                      # 发生错误
    COMPACT = "compact"                  # 上下文压缩
    PERMISSION_REQUEST = "permission_request"  # 权限请求
    FILE_CHANGE = "file_change"          # 文件变更
    COMMAND_EXECUTE = "command_execute"  # 命令执行
```

### 事件触发时机详解

让我们看一个完整的Agent执行流程：

```python
# mewcode/agent.py - run()方法

async def run(self, conversation: ConversationManager):
    # 1. 会话开始
    if self.hook_engine:
        ctx = self._build_hook_context("session_start")
        await self.hook_engine.run_hooks("session_start", ctx)
    
    while True:  # 主循环
        iteration += 1
        
        # 2. 轮次开始
        if self.hook_engine:
            ctx = self._build_hook_context("turn_start")
            await self.hook_engine.run_hooks("turn_start", ctx)
        
        # 3. 发送请求前
        if self.hook_engine:
            ctx = self._build_hook_context("pre_send")
            await self.hook_engine.run_hooks("pre_send", ctx)
        
        # 调用LLM
        llm_stream = self.client.stream(conversation, system=system, tools=tools)
        
        # 4. 收到响应后
        if self.hook_engine:
            ctx = self._build_hook_context("post_receive", message=response.text)
            await self.hook_engine.run_hooks("post_receive", ctx)
        
        # 5. 执行工具调用
        for tool_call in response.tool_calls:
            # 5.1 工具执行前（可以拒绝）
            if self.hook_engine:
                ctx = self._build_hook_context(
                    "pre_tool_use",
                    tool_name=tool_call.tool_name,
                    tool_args=tool_call.arguments
                )
                rejection = await self.hook_engine.run_pre_tool_hooks(ctx)
                if rejection:
                    # 工具被拒绝，跳过执行
                    yield ToolResultEvent(
                        tool_id=tool_call.tool_id,
                        output=f"Hook rejected: {rejection.reason}",
                        is_error=True
                    )
                    continue
            
            # 执行工具
            result = await tool.execute(params)
            
            # 5.2 工具执行后
            if self.hook_engine:
                ctx = self._build_hook_context(
                    "post_tool_use",
                    tool_name=tool_call.tool_name,
                    tool_args=tool_call.arguments,
                    file_path=infer_file_path(tool_call.arguments)
                )
                await self.hook_engine.run_hooks("post_tool_use", ctx)
        
        # 6. 轮次结束
        if self.hook_engine:
            ctx = self._build_hook_context("turn_end")
            await self.hook_engine.run_hooks("turn_end", ctx)
        
        if stop_condition:
            break
```

**关键时刻**：

| 事件 | 触发时机 | 可以做什么 | 是否可拒绝 |
|------|---------|-----------|----------|
| `session_start` | 对话刚开始 | 注入项目信息 | ❌ |
| `turn_start` | 每轮开始前 | 准备环境 | ❌ |
| `pre_send` | 发送给LLM前 | 修改上下文 | ❌ |
| `post_receive` | 收到LLM响应 | 分析回复内容 | ❌ |
| `pre_tool_use` | **工具执行前** | **安全检查** | ✅ |
| `post_tool_use` | 工具执行后 | 自动化任务 | ❌ |
| `turn_end` | 每轮结束后 | 清理、统计 | ❌ |
| `error` | 发生错误时 | 告警通知 | ❌ |

**只有`pre_tool_use`可以拒绝操作！**

---

## 第二步：HookContext - 上下文数据传递

### 问题：Hook需要知道什么？

当Hook被触发时，它需要知道当前的上下文信息：

- 触发了什么事件？
- 正在调用哪个工具？
- 工具的参数是什么？
- 操作的是哪个文件？
- 发生了什么错误？

### HookContext的定义

```python
# mewcode/hooks/models.py
@dataclass
class HookContext:
    event_name: str = ""                        # 事件名称
    tool_name: str = ""                         # 工具名称
    tool_args: dict[str, Any] = field(default_factory=dict)  # 工具参数
    file_path: str = ""                         # 文件路径
    message: str = ""                           # 消息内容
    error: str = ""                             # 错误信息
```

### 字段访问：简化的路径语法

```python
def get_field(self, name: str) -> str:
    """获取上下文字段"""
    if name == "tool":
        return self.tool_name
    if name == "event":
        return self.event_name
    if name.startswith("args."):
        # 支持args.xxx访问工具参数
        key = name[5:]
        value = self.tool_args.get(key, "")
        return str(value) if value else ""
    return ""
```

**使用示例**：

```python
ctx = HookContext(
    tool_name="WriteFile",
    tool_args={"file_path": "src/main.py", "content": "print('hello')"}
)

# 简单访问
ctx.get_field("tool")              # → "WriteFile"
ctx.get_field("args.file_path")    # → "src/main.py"
ctx.get_field("args.content")      # → "print('hello')"
```

### 变量展开：动态文本替换

Hook的action中可以使用变量，在执行时会被替换为实际值：

```python
def expand(self, template: str) -> str:
    """展开模板中的变量"""
    result = template
    result = result.replace("$EVENT", self.event_name)
    result = result.replace("$TOOL_NAME", self.tool_name)
    result = result.replace("$FILE_PATH", self.file_path)
    result = result.replace("$MESSAGE", self.message)
    result = result.replace("$ERROR", self.error)
    
    # 展开工具参数
    for key, value in self.tool_args.items():
        result = result.replace(f"$TOOL_ARGS.{key}", str(value))
    
    return result
```

**实战案例**：

```yaml
# Hook配置
action:
  type: "command"
  command: "echo 'Tool $TOOL_NAME called with file $FILE_PATH'"
```

```python
# 执行时
ctx = HookContext(
    tool_name="WriteFile",
    file_path="src/app.py"
)
command = ctx.expand("echo 'Tool $TOOL_NAME called with file $FILE_PATH'")
# → "echo 'Tool WriteFile called with file src/app.py'"
```

**所有可用变量**：

| 变量 | 含义 | 示例 |
|------|------|------|
| `$EVENT` | 事件名称 | `post_tool_use` |
| `$TOOL_NAME` | 工具名称 | `WriteFile` |
| `$FILE_PATH` | 文件路径 | `src/main.py` |
| `$MESSAGE` | 消息内容 | `File written` |
| `$ERROR` | 错误信息 | `Permission denied` |
| `$TOOL_ARGS.xxx` | 工具参数 | `$TOOL_ARGS.file_path` |

---

## 第三步：条件系统 - 精准匹配

### 问题：我只想Hook特定情况

不是所有`post_tool_use`事件都需要Hook，我只想：

- Hook写入Python文件的操作
- Hook包含`rm -rf`的Bash命令
- Hook特定目录下的文件变更

这就需要**条件系统**。

### Condition：单个条件

```python
# mewcode/hooks/conditions.py
@dataclass
class Condition:
    field: str       # 要比较的字段（如 "tool", "args.command"）
    operator: str    # 运算符（如 "==", "=~"）
    value: str       # 要匹配的值
```

### 四种运算符

#### 1. `==` 精确匹配

```python
Condition(field="tool", operator="==", value="Bash")

# 示例
ctx = HookContext(tool_name="Bash")
condition.evaluate(ctx)  # → True

ctx = HookContext(tool_name="WriteFile")
condition.evaluate(ctx)  # → False
```

#### 2. `!=` 不等于

```python
Condition(field="tool", operator="!=", value="ReadFile")

# 匹配所有非ReadFile的工具
```

#### 3. `=~` 正则匹配

```python
Condition(field="args.command", operator="=~", value="/rm\\s+-rf/")

# 实现
def evaluate(self, ctx: HookContext) -> bool:
    if self.operator == "=~":
        pattern = self.value
        # 去掉前后的斜杠
        if pattern.startswith("/") and pattern.endswith("/"):
            pattern = pattern[1:-1]
        try:
            return bool(re.search(pattern, field_value))
        except re.error:
            return False
```

**使用场景**：

```yaml
# 匹配危险命令
if: 'args.command =~ /rm\s+-rf/'

# 匹配Git push
if: 'args.command =~ /git\s+push/'

# 匹配IP地址
if: 'args.url =~ /\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/'
```

#### 4. `~=` Glob模式匹配

```python
Condition(field="args.file_path", operator="~=", value="*.py")

# 实现
def evaluate(self, ctx: HookContext) -> bool:
    if self.operator == "~=":
        return fnmatch.fnmatch(field_value, self.value)
```

**使用场景**：

```yaml
# 匹配Python文件
if: 'args.file_path ~= "*.py"'

# 匹配src目录下的文件
if: 'args.file_path ~= "src/*"'

# 匹配所有测试文件
if: 'args.file_path ~= "test_*.py"'
```

### ConditionGroup：组合条件

单个条件不够用？用`AND`或`OR`组合：

```python
@dataclass
class ConditionGroup:
    conditions: list[Condition]      # 条件列表
    logic: str = "and"               # 逻辑关系："and" 或 "or"
    
    def evaluate(self, ctx: HookContext) -> bool:
        if not self.conditions:
            return True  # 空条件组总是true
        
        if self.logic == "and":
            return all(c.evaluate(ctx) for c in self.conditions)
        else:  # or
            return any(c.evaluate(ctx) for c in self.conditions)
```

**AND示例**：

```yaml
# 同时满足两个条件
if: 'tool == "WriteFile" && args.file_path ~= "*.py"'
```

```python
# 解析为
ConditionGroup(
    conditions=[
        Condition("tool", "==", "WriteFile"),
        Condition("args.file_path", "~=", "*.py")
    ],
    logic="and"
)
```

**OR示例**：

```yaml
# 满足任一条件
if: 'tool == "WriteFile" || tool == "EditFile"'
```

```python
# 解析为
ConditionGroup(
    conditions=[
        Condition("tool", "==", "WriteFile"),
        Condition("tool", "==", "EditFile")
    ],
    logic="or"
)
```

### 条件解析器

系统会自动解析条件字符串：

```python
# mewcode/hooks/conditions.py
def parse_condition(expr: str) -> ConditionGroup | None:
    """解析条件表达式"""
    if not expr or not expr.strip():
        return None
    
    expr = expr.strip()
    has_and = "&&" in expr
    has_or = "||" in expr
    
    # 不允许混用AND和OR
    if has_and and has_or:
        raise ConditionParseError(
            "Cannot mix '&&' and '||' in a single condition expression. "
            "Split into separate hooks instead."
        )
    
    # 分割表达式
    if has_and:
        parts = expr.split("&&")
        logic = "and"
    elif has_or:
        parts = expr.split("||")
        logic = "or"
    else:
        parts = [expr]
        logic = "and"
    
    # 解析每个部分
    conditions = [_parse_single(p) for p in parts]
    return ConditionGroup(conditions=conditions, logic=logic)


def _parse_single(expr: str) -> Condition:
    """解析单个条件"""
    expr = expr.strip()
    
    # 按优先级尝试每个运算符
    for op in ["==", "!=", "=~", "~="]:
        idx = expr.find(op)
        if idx == -1:
            continue
        
        field_part = expr[:idx].strip()
        value_part = expr[idx + len(op):].strip()
        
        # 去掉引号
        if value_part.startswith('"') and value_part.endswith('"'):
            value_part = value_part[1:-1]
        
        return Condition(field=field_part, operator=op, value=value_part)
    
    raise ConditionParseError(f"No valid operator found in condition: '{expr}'")
```

**解析示例**：

```python
# 输入
'tool == "Bash" && args.command =~ /rm\\s+-rf/'

# 输出
ConditionGroup(
    conditions=[
        Condition(field="tool", operator="==", value="Bash"),
        Condition(field="args.command", operator="=~", value="/rm\\s+-rf/")
    ],
    logic="and"
)
```

---

## 第四步：Action执行器 - 做什么

当Hook被触发且条件匹配时，该做什么？系统提供4种执行器。

### Action模型

```python
# mewcode/hooks/models.py
@dataclass
class Action:
    type: str                                    # 执行器类型
    
    # command执行器
    command: str = ""
    timeout: int = 30
    
    # prompt执行器
    message: str = ""
    
    # http执行器
    url: str = ""
    method: str = "POST"
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    
    # agent执行器
    prompt: str = ""
```

### 执行器1：command - 执行Shell命令

**最常用的执行器**，可以运行任何Shell命令。

```python
# mewcode/hooks/executors.py
async def execute_command(action: Action, ctx: HookContext) -> ActionResult:
    # 1. 展开变量
    command = ctx.expand(action.command)
    
    try:
        # 2. 创建子进程
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        
        # 3. 等待完成（带超时）
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), 
                timeout=action.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ActionResult(
                output=f"Command timed out after {action.timeout}s: {command}",
                success=False,
            )
        
        # 4. 返回结果
        output = stdout.decode(errors="replace").strip() if stdout else ""
        return ActionResult(output=output, success=proc.returncode == 0)
        
    except Exception as e:
        return ActionResult(output=f"Command execution error: {e}", success=False)
```

**使用示例**：

```yaml
# 示例1：自动格式化Python文件
action:
  type: "command"
  command: "black $FILE_PATH"
  timeout: 30

# 示例2：记录日志
action:
  type: "command"
  command: "echo '[$(date)] Tool: $TOOL_NAME' >> /tmp/hook.log"

# 示例3：运行测试
action:
  type: "command"
  command: "pytest tests/test_$TOOL_ARGS.module.py -v"
  timeout: 60
```

### 执行器2：prompt - 向AI注入消息

**给AI发送额外的上下文信息**。

```python
async def execute_prompt(action: Action, ctx: HookContext) -> ActionResult:
    # 展开变量后直接返回
    message = ctx.expand(action.message)
    return ActionResult(output=message, success=True)
```

```python
# 在HookEngine中，prompt类型的结果会被收集
if hook.action.type == "prompt" and result.success:
    self._prompt_messages.append(result.output)

# 在Agent中，这些消息会被注入到system prompt
hook_prompts = self.hook_engine.get_prompt_messages()
system = build_system_prompt(hook_prompts=hook_prompts, ...)
```

**使用示例**：

```yaml
# 示例1：注入项目信息
- event: "session_start"
  action:
    type: "prompt"
    message: |
      This is a Django project with the following structure:
      - apps/: Django apps
      - config/: Configuration files
      - Please follow Django best practices.
  once: true

# 示例2：动态提示
- event: "post_receive"
  if: 'message =~ /database/'
  action:
    type: "prompt"
    message: "Remember: this project uses PostgreSQL, not MySQL."

# 示例3：错误提示
- event: "error"
  action:
    type: "prompt"
    message: "An error occurred: $ERROR. Please check the logs."
```

### 执行器3：http - 发送HTTP请求

**集成外部服务**，如Slack、Discord、监控系统。

```python
async def execute_http(action: Action, ctx: HookContext) -> ActionResult:
    # 1. 展开所有变量
    url = ctx.expand(action.url)
    body = ctx.expand(action.body) if action.body else None
    method = action.method or "POST"
    
    # 2. 展开headers
    headers = dict(action.headers)
    for k, v in headers.items():
        headers[k] = ctx.expand(v)
    
    # 3. 自动添加Content-Type
    if body and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    
    # 4. 执行请求（在线程池中，避免阻塞）
    def _do_request() -> ActionResult:
        try:
            data = body.encode() if body else None
            req = Request(url, data=data, headers=headers, method=method)
            
            with urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode(errors="replace")[:500]
                return ActionResult(
                    output=f"HTTP {resp.status}: {resp_body}",
                    success=200 <= resp.status < 300,
                )
        except URLError as e:
            return ActionResult(output=f"HTTP error: {e}", success=False)
        except Exception as e:
            return ActionResult(output=f"HTTP error: {e}", success=False)
    
    # 5. 在executor中运行
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _do_request)
```

**使用示例**：

```yaml
# 示例1：Slack通知
- event: "error"
  action:
    type: "http"
    url: "https://hooks.slack.com/services/T00/B00/xxx"
    method: "POST"
    body: |
      {
        "text": "🚨 Error in MewCode",
        "attachments": [{
          "color": "danger",
          "fields": [{
            "title": "Error",
            "value": "$ERROR",
            "short": false
          }]
        }]
      }

# 示例2：Discord Webhook
- event: "post_tool_use"
  if: 'tool == "WriteFile"'
  action:
    type: "http"
    url: "https://discord.com/api/webhooks/xxx/yyy"
    body: |
      {
        "content": "File written: $FILE_PATH"
      }

# 示例3：自定义API
- event: "post_tool_use"
  action:
    type: "http"
    url: "https://api.example.com/track"
    headers:
      Authorization: "Bearer $API_KEY"
      X-Session-ID: "$SESSION_ID"
    body: |
      {
        "event": "$EVENT",
        "tool": "$TOOL_NAME",
        "timestamp": "$TIMESTAMP"
      }
```

### 执行器4：agent - 启动子Agent

**委托给另一个Agent处理**（未完整实现）。

```python
async def execute_agent(action: Action, ctx: HookContext) -> ActionResult:
    prompt = ctx.expand(action.prompt)
    log.info("Agent executor stub called with prompt: %s", prompt[:100])
    
    # TODO: 实际启动子Agent
    return ActionResult(
        output="agent executor not yet implemented",
        success=True,
    )
```

**设计目标**（未来）：

```yaml
# 示例：代码审查
- event: "post_tool_use"
  if: 'tool == "WriteFile" && args.file_path ~= "src/*.py"'
  action:
    type: "agent"
    prompt: "Review the code in $FILE_PATH for security issues"
    timeout: 120
```

### 执行器调度

```python
# mewcode/hooks/executors.py
_EXECUTOR_MAP = {
    "command": execute_command,
    "prompt": execute_prompt,
    "http": execute_http,
    "agent": execute_agent,
}

async def execute_action(action: Action, ctx: HookContext) -> ActionResult:
    executor = _EXECUTOR_MAP.get(action.type)
    if executor is None:
        return ActionResult(
            output=f"Unknown action type: {action.type}",
            success=False,
        )
    return await executor(action, ctx)
```

---

## 第五步：Hook配置加载与验证

### Hook模型

```python
# mewcode/hooks/models.py
@dataclass
class Hook:
    id: str                                      # Hook唯一标识
    event: str                                   # 触发事件
    action: Action                               # 要执行的动作
    condition: ConditionGroup | None = None      # 触发条件
    reject: bool = False                         # 是否拒绝操作（仅pre_tool_use）
    once: bool = False                           # 是否只执行一次
    async_exec: bool = False                     # 是否异步执行
    executed: bool = False                       # 是否已执行（once标记用）
    
    def should_run(self) -> bool:
        """判断是否应该执行"""
        if self.once and self.executed:
            return False
        return True
    
    def mark_executed(self) -> None:
        """标记为已执行"""
        self.executed = True
```

### 配置格式

```yaml
# hook_config.yaml
hooks:
  - id: "auto-format"                    # 必需：唯一标识
    event: "post_tool_use"               # 必需：事件类型
    if: 'tool == "WriteFile"'            # 可选：触发条件
    action:                               # 必需：执行动作
      type: "command"
      command: "black $FILE_PATH"
    async: true                          # 可选：异步执行
    once: false                          # 可选：只执行一次
    reject: false                        # 可选：拒绝操作
```

### 加载器实现

```python
# mewcode/hooks/loader.py
def load_hooks(raw_hooks: list[dict] | None) -> list[Hook]:
    """加载并验证Hook配置"""
    if not raw_hooks:
        return []
    
    hooks: list[Hook] = []
    
    for i, entry in enumerate(raw_hooks):
        label = _identify(entry, i)  # 用于错误消息
        
        # ===== 验证event字段 =====
        event = entry.get("event")
        if not event:
            raise HookConfigError(f"{label}: missing 'event' field")
        
        if event not in _VALID_EVENTS:
            raise HookConfigError(
                f"{label}: invalid event '{event}', "
                f"must be one of: {', '.join(sorted(_VALID_EVENTS))}"
            )
        
        # ===== 验证action字段 =====
        raw_action = entry.get("action")
        if not isinstance(raw_action, dict):
            raise HookConfigError(f"{label}: missing or invalid 'action' field")
        
        action_type = raw_action.get("type")
        if action_type not in _VALID_ACTION_TYPES:
            raise HookConfigError(
                f"{label}: invalid action type '{action_type}', "
                f"must be one of: {', '.join(sorted(_VALID_ACTION_TYPES))}"
            )
        
        # ===== 验证必需字段 =====
        required = _REQUIRED_FIELDS[action_type]
        for field_name in required:
            if not raw_action.get(field_name):
                raise HookConfigError(
                    f"{label}: action type '{action_type}' requires "
                    f"'{field_name}' field"
                )
        
        # ===== 验证reject字段 =====
        reject = bool(entry.get("reject", False))
        if reject and event != "pre_tool_use":
            raise HookConfigError(
                f"{label}: 'reject' can only be used with 'pre_tool_use' event"
            )
        
        # ===== 验证async字段 =====
        async_exec = bool(entry.get("async", False))
        if async_exec and event == "pre_tool_use":
            raise HookConfigError(
                f"{label}: 'async' cannot be used with 'pre_tool_use' event"
            )
        
        # ===== 解析条件 =====
        condition = None
        raw_if = entry.get("if")
        if raw_if:
            try:
                condition = parse_condition(str(raw_if))
            except ConditionParseError as e:
                raise HookConfigError(f"{label}: condition error: {e}") from e
        
        # ===== 创建Hook =====
        hook_id = entry.get("id", f"{event}_{i}")
        
        action = Action(
            type=action_type,
            command=raw_action.get("command", ""),
            message=raw_action.get("message", ""),
            url=raw_action.get("url", ""),
            method=raw_action.get("method", "POST"),
            body=raw_action.get("body", ""),
            headers=raw_action.get("headers", {}),
            prompt=raw_action.get("prompt", ""),
            timeout=raw_action.get("timeout", 30),
        )
        
        hooks.append(
            Hook(
                id=hook_id,
                event=event,
                action=action,
                condition=condition,
                reject=reject,
                once=bool(entry.get("once", False)),
                async_exec=async_exec,
            )
        )
    
    return hooks
```

**配置验证规则**：

| 验证项 | 规则 | 错误示例 |
|-------|------|---------|
| `event`字段 | 必须是15个有效事件之一 | `event: "bad_event"` |
| `action.type` | 必须是4个有效类型之一 | `type: "unknown"` |
| 必需字段 | command需要`command`，http需要`url`等 | `type: "command"` 但没有`command` |
| `reject`限制 | 只能用于`pre_tool_use` | `event: "post_tool_use", reject: true` |
| `async`限制 | 不能用于`pre_tool_use` | `event: "pre_tool_use", async: true` |
| 条件语法 | 不能混用`&&`和`||` | `if: 'a && b || c'` |

---

## 第六步：HookEngine - 执行引擎

### 核心职责

```python
# mewcode/hooks/engine.py
class HookEngine:
    def __init__(self, hooks: list[Hook] | None = None):
        self.hooks: list[Hook] = hooks or []
        self._prompt_messages: list[str] = []         # 收集prompt消息
        self._notifications: list[HookNotification] = []  # 收集执行通知
```

### 功能1：查找匹配的Hook

```python
def find_matching_hooks(self, event: str, ctx: HookContext) -> list[Hook]:
    """找出所有匹配的Hook"""
    matched: list[Hook] = []
    
    for hook in self.hooks:
        # 1. 事件类型匹配
        if hook.event != event:
            continue
        
        # 2. once语义检查
        if not hook.should_run():
            continue
        
        # 3. 条件评估
        if hook.condition is not None and not hook.condition.evaluate(ctx):
            continue
        
        matched.append(hook)
    
    return matched
```

**匹配流程**：

```
检查1: 事件类型匹配？
    ↓ YES
检查2: 如果是once，是否已执行？
    ↓ NO（未执行）
检查3: 条件是否满足？
    ↓ YES
匹配成功 → 加入列表
```

### 功能2：执行普通Hook

```python
async def run_hooks(self, event: str, ctx: HookContext) -> None:
    """执行指定事件的所有Hook"""
    matched = self.find_matching_hooks(event, ctx)
    
    for hook in matched:
        # 标记为已执行（once语义）
        hook.mark_executed()
        
        # 异步执行 vs 同步执行
        if hook.async_exec:
            # 在后台执行，不等待结果
            asyncio.ensure_future(self._run_single(hook, ctx))
        else:
            # 等待执行完成
            await self._run_single(hook, ctx)


async def _run_single(self, hook: Hook, ctx: HookContext) -> None:
    """执行单个Hook"""
    try:
        # 1. 执行action
        result = await execute_action(hook.action, ctx)
        
        # 2. 如果是prompt类型，收集消息
        if hook.action.type == "prompt" and result.success:
            self._prompt_messages.append(result.output)
        
        # 3. 记录通知
        self._notifications.append(
            HookNotification(
                hook_id=hook.id,
                event=hook.event,
                output=result.output,
                success=result.success,
            )
        )
        
        # 4. 失败时记录日志
        if not result.success:
            log.warning(
                "Hook '%s' action failed: %s", hook.id, result.output
            )
            
    except Exception as e:
        # 5. 异常不应该终止整个流程
        log.warning("Hook '%s' execution error: %s", hook.id, e)
        self._notifications.append(
            HookNotification(
                hook_id=hook.id,
                event=hook.event,
                output=str(e),
                success=False,
            )
        )
```

**错误容忍设计**：

```
Hook执行失败
    ↓
记录日志 + 通知
    ↓
继续执行下一个Hook
    ↓
不影响Agent主流程
```

### 功能3：pre_tool_use拒绝机制

**这是Hook系统最强大的功能**：可以阻止工具执行。

```python
async def run_pre_tool_hooks(
    self, ctx: HookContext
) -> ToolRejectedError | None:
    """执行pre_tool_use Hook，返回拒绝错误（如果有）"""
    matched = self.find_matching_hooks("pre_tool_use", ctx)
    
    for hook in matched:
        hook.mark_executed()
        
        try:
            # 执行action
            result = await execute_action(hook.action, ctx)
            
            # 记录通知
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event="pre_tool_use",
                    output=result.output,
                    success=result.success,
                )
            )
            
            # 如果标记为reject，返回拒绝错误
            if hook.reject:
                return ToolRejectedError(
                    tool=ctx.tool_name,
                    reason=result.output,
                    hook_id=hook.id,
                )
                
        except Exception as e:
            log.warning("Hook '%s' execution error: %s", hook.id, e)
    
    # 没有Hook拒绝
    return None
```

**在Agent中的使用**：

```python
# mewcode/agent.py
for tool_call in response.tool_calls:
    # 执行前检查
    if self.hook_engine:
        ctx = self._build_hook_context(
            "pre_tool_use",
            tool_name=tool_call.tool_name,
            tool_args=tool_call.arguments
        )
        rejection = await self.hook_engine.run_pre_tool_hooks(ctx)
        
        if rejection:
            # 工具被拒绝！
            yield ToolResultEvent(
                tool_id=tool_call.tool_id,
                tool_name=tool_call.tool_name,
                output=f"Hook rejected tool use: {rejection.reason}",
                is_error=True,
                elapsed=0.0
            )
            # 跳过实际执行
            continue
    
    # 正常执行工具
    result = await self._execute_single_tool_direct(tool_call)
```

**拒绝流程**：

```
AI: 调用 Bash(command="rm -rf /")
    ↓
Agent: 执行run_pre_tool_hooks()
    ↓
HookEngine: 找到匹配的Hook（reject=true）
    ↓
Hook: 执行action（输出"BLOCKED"）
    ↓
HookEngine: 返回ToolRejectedError
    ↓
Agent: 跳过工具执行，返回错误给AI
    ↓
AI: 看到错误，换一个方法
```

### 功能4：消息收集与分发

```python
def get_prompt_messages(self) -> list[str]:
    """获取并清空prompt消息"""
    messages = list(self._prompt_messages)
    self._prompt_messages.clear()
    return messages


def drain_notifications(self) -> list[HookNotification]:
    """获取并清空通知"""
    notifications = list(self._notifications)
    self._notifications.clear()
    return notifications
```

**在Agent中的使用**：

```python
# 获取prompt消息，注入到system prompt
hook_prompts = self.hook_engine.get_prompt_messages()
system = build_system_prompt(hook_prompts=hook_prompts, ...)

# 获取通知，添加到对话
for note in self.hook_engine.drain_notifications():
    conversation.add_system_reminder(
        f"Hook [{note.hook_id}] {note.event}: {note.output}"
    )
```

---

## 第七步：与Agent集成

### Agent如何使用HookEngine

```python
# mewcode/agent.py
class Agent:
    def __init__(
        self,
        hook_engine: HookEngine | None = None,
        ...
    ):
        self.hook_engine = hook_engine
```

### 集成点1：会话开始

```python
async def run(self, conversation: ConversationManager):
    if self.hook_engine:
        ctx = self._build_hook_context("session_start")
        await self.hook_engine.run_hooks("session_start", ctx)
        for he in self._drain_hook_events():
            yield he
```

### 集成点2：每轮开始/结束

```python
while True:
    iteration += 1
    
    # 轮次开始
    if self.hook_engine:
        ctx = self._build_hook_context("turn_start")
        await self.hook_engine.run_hooks("turn_start", ctx)
    
    # ... 执行AI对话 ...
    
    # 轮次结束
    if self.hook_engine:
        ctx = self._build_hook_context("turn_end")
        await self.hook_engine.run_hooks("turn_end", ctx)
```

### 集成点3：工具执行前后

```python
# 执行前
if self.hook_engine:
    ctx = self._build_hook_context(
        "pre_tool_use",
        tool_name=tc.tool_name,
        tool_args=tc.arguments
    )
    rejection = await self.hook_engine.run_pre_tool_hooks(ctx)
    if rejection:
        # 拒绝执行
        return ToolResultEvent(is_error=True, output=rejection.reason)

# 执行工具
result = await tool.execute(params)

# 执行后
if self.hook_engine:
    file_path = self._infer_file_path(tc.arguments)
    ctx = self._build_hook_context(
        "post_tool_use",
        tool_name=tc.tool_name,
        tool_args=tc.arguments,
        file_path=file_path,
    )
    await self.hook_engine.run_hooks("post_tool_use", ctx)
```

### 集成点4：LLM通信前后

```python
# 发送前
if self.hook_engine:
    ctx = self._build_hook_context("pre_send")
    await self.hook_engine.run_hooks("pre_send", ctx)

# 调用LLM
llm_stream = self.client.stream(conversation, system=system, tools=tools)

# 收到响应后
if self.hook_engine:
    ctx = self._build_hook_context("post_receive", message=response.text)
    await self.hook_engine.run_hooks("post_receive", ctx)
```

### HookContext构建

```python
def _build_hook_context(
    self,
    event_name: str,
    tool_name: str = "",
    tool_args: dict[str, Any] | None = None,
    file_path: str = "",
    message: str = "",
    error: str = "",
) -> HookContext:
    """构建Hook上下文"""
    return HookContext(
        event_name=event_name,
        tool_name=tool_name,
        tool_args=tool_args or {},
        file_path=file_path,
        message=message,
        error=error,
    )
```

---

## 实战案例

### 案例1：安全防护系统

```yaml
# .mewcode/hooks.yaml
hooks:
  # 1. 阻止危险的rm命令
  - id: "block-rm-rf"
    event: "pre_tool_use"
    if: 'tool == "Bash" && args.command =~ /rm\s+-rf\s+\//'
    action:
      type: "command"
      command: "echo '🚫 BLOCKED: rm -rf / is extremely dangerous'"
    reject: true
  
  # 2. 阻止git push --force到main分支
  - id: "block-force-push-main"
    event: "pre_tool_use"
    if: 'tool == "Bash" && args.command =~ /git\s+push\s+.*--force.*main/'
    action:
      type: "command"
      command: "echo '🚫 BLOCKED: force push to main branch is not allowed'"
    reject: true
  
  # 3. 阻止直接修改生产配置
  - id: "block-prod-config"
    event: "pre_tool_use"
    if: 'tool == "WriteFile" && args.file_path ~= "config/production.yaml"'
    action:
      type: "prompt"
      message: "⚠️ WARNING: You are about to modify production config!"
    reject: true
```

### 案例2：自动化工作流

```yaml
hooks:
  # 1. Python文件自动格式化
  - id: "auto-format-python"
    event: "post_tool_use"
    if: 'tool == "WriteFile" && args.file_path ~= "*.py"'
    action:
      type: "command"
      command: |
        black $FILE_PATH && isort $FILE_PATH
    async: true
  
  # 2. JavaScript文件自动Lint
  - id: "auto-lint-js"
    event: "post_tool_use"
    if: 'tool == "WriteFile" && args.file_path ~= "*.{js,jsx,ts,tsx}"'
    action:
      type: "command"
      command: "eslint --fix $FILE_PATH"
    async: true
  
  # 3. Git自动提交
  - id: "auto-git-add"
    event: "post_tool_use"
    if: 'tool == "WriteFile"'
    action:
      type: "command"
      command: "git add $FILE_PATH"
    async: true
```

### 案例3：监控与告警

```yaml
hooks:
  # 1. Slack错误通知
  - id: "slack-error-notification"
    event: "error"
    action:
      type: "http"
      url: "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
      body: |
        {
          "text": "🚨 MewCode Error Alert",
          "blocks": [{
            "type": "section",
            "text": {
              "type": "mrkdwn",
              "text": "*Error:* $ERROR\n*Time:* $(date)"
            }
          }]
        }
  
  # 2. 记录所有工具调用
  - id: "log-tool-usage"
    event: "post_tool_use"
    action:
      type: "command"
      command: |
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Tool: $TOOL_NAME" >> /tmp/mewcode_audit.log
    async: true
  
  # 3. 统计API调用
  - id: "track-api-calls"
    event: "post_receive"
    action:
      type: "http"
      url: "https://analytics.example.com/track"
      body: |
        {
          "event": "llm_call",
          "input_tokens": "$INPUT_TOKENS",
          "output_tokens": "$OUTPUT_TOKENS"
        }
    async: true
```

### 案例4：项目特定提示

```yaml
hooks:
  # 1. 会话开始时注入项目信息
  - id: "project-context"
    event: "session_start"
    action:
      type: "prompt"
      message: |
        # Project Context
        
        This is a Django REST API project:
        - Python 3.11
        - Django 4.2
        - PostgreSQL database
        - Redis cache
        
        Code style:
        - Use Black for formatting
        - Follow PEP 8
        - Type hints required
        - Docstrings in Google style
    once: true
  
  # 2. 提醒测试覆盖率
  - id: "remind-tests"
    event: "post_tool_use"
    if: 'tool == "WriteFile" && args.file_path ~= "src/*.py"'
    action:
      type: "prompt"
      message: "Remember to write tests for the new code in tests/ directory."
    once: true
  
  # 3. 数据库操作提醒
  - id: "database-reminder"
    event: "post_receive"
    if: 'message =~ /database|migration/'
    action:
      type: "prompt"
      message: |
        Database best practices:
        - Always create migrations: python manage.py makemigrations
        - Test migrations: python manage.py migrate --check
        - Use transactions for data operations
```

---

## 完整流程图

### 从用户请求到Hook执行

```
用户: "清理/tmp目录"
    ↓
AI: "好的，让我清理"
    决定调用: Bash(command="rm -rf /tmp/*")
    ↓
Agent.run() - 主循环
    ↓
[1] turn_start Hook触发
    matched = find_matching_hooks("turn_start", ctx)
    → 执行所有匹配的Hook
    ↓
[2] 准备调用LLM
    ↓
[3] pre_send Hook触发
    → 可以修改上下文
    ↓
[4] 调用LLM API
    ↓
[5] 收到响应
    ↓
[6] post_receive Hook触发
    ctx = HookContext(message=response.text)
    → 可以分析AI回复
    ↓
[7] 解析工具调用: Bash(command="rm -rf /tmp/*")
    ↓
[8] pre_tool_use Hook触发 ⚠️ 关键！
    ctx = HookContext(
        event_name="pre_tool_use",
        tool_name="Bash",
        tool_args={"command": "rm -rf /tmp/*"}
    )
    ↓
    HookEngine.run_pre_tool_hooks(ctx)
        ↓
        find_matching_hooks("pre_tool_use", ctx)
            检查事件: ✅
            检查once: ✅
            检查条件: 'tool == "Bash" && args.command =~ /rm\s+-rf/'
                → evaluate(ctx)
                → tool_name == "Bash" ✅
                → "rm -rf" 匹配正则 ✅
            → 匹配成功！
        ↓
        execute_action(hook.action, ctx)
            type: "command"
            command: ctx.expand("echo 'BLOCKED'")
            → 执行: "echo 'BLOCKED'"
            → 返回: ActionResult(output="BLOCKED", success=True)
        ↓
        检查: hook.reject == True
        → 返回: ToolRejectedError(
            tool="Bash",
            reason="BLOCKED",
            hook_id="block-rm-rf"
        )
    ↓
Agent收到拒绝错误
    ↓
跳过工具执行！
    ↓
返回给AI: ToolResultEvent(
    output="Hook rejected tool use: BLOCKED",
    is_error=True
)
    ↓
[9] post_tool_use Hook不触发（因为工具没执行）
    ↓
[10] turn_end Hook触发
    ↓
AI: 看到错误 "Hook rejected tool use: BLOCKED"
    ↓
AI: "抱歉，这个命令被系统阻止了。让我换一个安全的方式..."
```

---

## 代码亮点

### 亮点1：声明式配置

**问题**：硬编码的if-else逻辑难以维护

```python
# ❌ 硬编码
if tool_name == "Bash" and "rm -rf" in command:
    print("BLOCKED")
    return
```

**解决**：YAML配置，无需改代码

```yaml
# ✅ 声明式
- if: 'tool == "Bash" && args.command =~ /rm\s+-rf/'
  action:
    type: "command"
    command: "echo 'BLOCKED'"
  reject: true
```

### 亮点2：类型安全验证

```python
# Pydantic验证
@dataclass
class Action:
    type: str
    command: str = ""
    timeout: int = 30

# 加载时自动验证
action = Action(**raw_action)
# 如果type不是str，或timeout不是int，自动报错
```

### 亮点3：错误容忍设计

```python
async def _run_single(self, hook: Hook, ctx: HookContext):
    try:
        result = await execute_action(hook.action, ctx)
        # ...
    except Exception as e:
        # Hook失败不应该终止Agent
        log.warning("Hook '%s' execution error: %s", hook.id, e)
        self._notifications.append(
            HookNotification(..., success=False)
        )
        # 继续执行，不抛出异常
```

### 亮点4：异步执行支持

```python
if hook.async_exec:
    # 后台执行，不阻塞主流程
    asyncio.ensure_future(self._run_single(hook, ctx))
else:
    # 同步执行，等待完成
    await self._run_single(hook, ctx)
```

**使用场景**：

- 同步：安全检查（必须等待结果）
- 异步：格式化代码（不需要等待）

### 亮点5：条件组合的清晰实现

```python
# 不允许混用AND和OR
if has_and and has_or:
    raise ConditionParseError(
        "Cannot mix '&&' and '||' in a single condition expression."
    )

# 原因：避免优先级混淆
# ❌ 'a && b || c' 是 (a && b) || c 还是 a && (b || c) ？
# ✅ 拆成两个Hook，逻辑清晰
```

### 亮点6：变量展开的简单实现

```python
def expand(self, template: str) -> str:
    result = template
    result = result.replace("$EVENT", self.event_name)
    result = result.replace("$TOOL_NAME", self.tool_name)
    # ...
    return result
```

**为什么不用正则或模板引擎？**

- 简单：30行代码
- 快速：字符串替换很快
- 够用：满足所有需求
- 安全：不执行代码

---

## 测试覆盖

### 测试文件结构

```python
# tests/test_hooks.py

# 1. 事件定义测试（3个）
class TestLifecycleEvent:
    def test_has_15_events()
    def test_string_comparison()
    def test_all_values()

# 2. 上下文测试（5个）
class TestHookContext:
    def test_get_field_tool()
    def test_get_field_event()
    def test_get_field_args()
    def test_get_field_unknown()
    def test_expand_all_variables()
    def test_expand_undefined_variable()

# 3. 条件解析测试（6个）
class TestParseCondition:
    def test_single_condition()
    def test_and_combination()
    def test_or_combination()
    def test_mixed_operators_error()
    def test_empty_condition()
    def test_regex_format()
    def test_no_valid_operator()

# 4. 条件求值测试（8个）
class TestConditionEvaluate:
    def test_eq()
    def test_neq()
    def test_regex()
    def test_glob()

class TestConditionGroupEvaluate:
    def test_and_all_pass()
    def test_and_partial_fail()
    def test_or_any_pass()
    def test_or_all_fail()
    def test_empty_group()

# 5. 执行器测试（8个）
class TestCommandExecutor:
    @pytest.mark.asyncio
    async def test_normal_execution()
    async def test_variable_substitution()
    async def test_timeout()

class TestPromptExecutor:
    async def test_returns_message()

class TestHttpExecutor:
    async def test_mock_request()

class TestAgentExecutor:
    async def test_stub()

class TestExecuteAction:
    async def test_dispatch()
    async def test_unknown_type()

# 6. 配置加载测试（11个）
class TestLoadHooks:
    def test_full_config()
    def test_auto_id()
    def test_empty()
    def test_invalid_event()
    def test_invalid_action_type()
    def test_reject_on_non_pre_tool_use()
    def test_async_on_pre_tool_use()
    def test_missing_required_field()

# 7. 引擎测试（8个）
class TestHookEngine:
    def test_find_matching_hooks()
    def test_find_with_condition_filter()
    def test_once_filter()
    
    @pytest.mark.asyncio
    async def test_run_pre_tool_hooks_reject()
    async def test_run_pre_tool_hooks_no_reject()
    async def test_prompt_message_collection()
    async def test_error_does_not_raise()
    async def test_async_hook_does_not_block()

# 8. Agent集成测试（1个）
class TestAgentHookIntegration:
    @pytest.mark.asyncio
    async def test_pre_tool_use_reject_skips_tool()
```

**总计**：50个测试用例

### 关键测试案例

#### 测试1：pre_tool_use拒绝

```python
async def test_pre_tool_use_reject_skips_tool(self):
    # 创建Hook：拒绝包含"rm -rf"的命令
    hook = Hook(
        id="block-rm",
        event="pre_tool_use",
        action=Action(type="command", command="echo dangerous"),
        condition=parse_condition('tool == "Bash" && args.command =~ /rm\\s+-rf/'),
        reject=True,
    )
    engine = HookEngine([hook])
    
    # 模拟Agent
    agent = Agent(hook_engine=engine, ...)
    
    # AI尝试调用危险命令
    conversation.add_user_message("delete everything")
    
    # 执行
    events = []
    async for event in agent.run(conversation):
        events.append(event)
    
    # 验证：工具调用被拒绝
    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) >= 1
    rejected = tool_results[0]
    assert rejected.is_error is True
    assert "Hook rejected" in rejected.output
```

#### 测试2：变量展开

```python
def test_expand_all_variables(self):
    ctx = HookContext(
        event_name="post_tool_use",
        tool_name="WriteFile",
        tool_args={"file_path": "src/main.py"},
        file_path="src/main.py",
        message="done",
        error="",
    )
    
    template = "Event=$EVENT Tool=$TOOL_NAME File=$FILE_PATH"
    result = ctx.expand(template)
    
    assert "Event=post_tool_use" in result
    assert "Tool=WriteFile" in result
    assert "File=src/main.py" in result
```

#### 测试3：条件组合

```python
def test_and_combination(self):
    group = parse_condition('tool == "Bash" && args.command =~ /rm/')
    assert group is not None
    assert len(group.conditions) == 2
    assert group.logic == "and"
    
    # 测试求值
    ctx = HookContext(
        tool_name="Bash",
        tool_args={"command": "rm -rf /"}
    )
    assert group.evaluate(ctx) is True
```

---

## 总结

Hook系统解决了什么问题：

1. **安全防护** - 阻止危险操作
2. **自动化** - 文件保存后自动格式化、测试
3. **集成** - 通知外部系统（Slack、监控）
4. **上下文注入** - 向AI提供项目特定信息
5. **可扩展** - 无需修改核心代码

**代码亮点**：

- ✨ **声明式配置** - YAML描述行为
- ✨ **类型安全** - Pydantic自动验证
- ✨ **事件驱动** - 松耦合架构
- ✨ **错误容忍** - Hook失败不影响主流程
- ✨ **灵活执行** - 同步/异步/拒绝

**核心组件**：

```
LifecycleEvent (15个事件)
    ↓
HookContext (上下文+变量展开)
    ↓
Condition System (4种运算符+组合)
    ↓
Action Executors (command/prompt/http/agent)
    ↓
HookEngine (执行引擎)
    ↓
Agent Integration (生命周期集成)
```

下一章我们将学习：如何通过命令系统扩展MewCode的交互能力？
