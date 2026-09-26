# CH12: SubAgent - 子Agent与任务分发

## 从一次任务分发说起

你问 AI："帮我重构这个10万行的代码库，同时修复所有的安全漏洞"

**问题**：
- 单个Agent处理太慢（串行执行）
- 上下文太大（10万行代码）
- 任务太复杂（重构+安全审查）

**解决方案**：启动多个SubAgent并行工作

```
主Agent：协调者
    ↓
Fork 3个SubAgent：
    ├─ SubAgent 1: 重构 auth 模块
    ├─ SubAgent 2: 重构 api 模块
    └─ SubAgent 3: 安全扫描全部代码
    ↓
并行执行（3倍速度）
    ↓
主Agent：汇总结果，验证一致性
```

这就是**SubAgent系统**要解决的问题：**将复杂任务分解，并行执行，提升效率**。

---

## SubAgent能做什么？

### 场景1：并行研究
```python
# 主Agent调用
Agent(prompt="研究React文档中的Hook用法")
Agent(prompt="研究Vue文档中的组合式API")
Agent(prompt="研究Angular文档中的依赖注入")
# 3个SubAgent同时搜索，3倍速度
```

### 场景2：隔离实验
```python
# 在隔离的worktree中尝试不同方案
Agent(
    prompt="尝试方案A：用Redis缓存",
    isolation="worktree"
)
Agent(
    prompt="尝试方案B：用Memcached缓存",
    isolation="worktree"
)
# 互不干扰，安全实验
```

### 场景3：专业分工
```python
# 不同类型的Agent做不同的事
Agent(
    prompt="审查代码安全性",
    subagent_type="security-reviewer"
)
Agent(
    prompt="优化性能",
    subagent_type="performance-optimizer"
)
# 专业Agent，专业能力
```

### 场景4：团队协作
```python
# 创建长期运行的团队
TeamCreate(name="feature-team")
Agent(
    prompt="实现前端",
    team_name="feature-team",
    name="frontend-dev"
)
Agent(
    prompt="实现后端",
    team_name="feature-team",
    name="backend-dev"
)
# 成员通过SendMessage通信
```

---

## 第一步：SubAgent的四种模式

### 模式1：前台同步执行（默认）

**特点**：阻塞等待，返回结果

```python
# AI调用
result = Agent(prompt="读取所有Python文件")

# 执行流程
主Agent调用AgentTool.execute()
    ↓
创建SubAgent
    ↓
await sub_agent.run_to_completion(prompt)  # 等待完成
    ↓
返回结果给主Agent
    ↓
主Agent继续执行
```

**使用场景**：
- 主Agent需要SubAgent的结果才能继续
- 任务简单，执行时间短
- 需要立即处理SubAgent的输出

**配置**：
```yaml
# 不设置任何特殊参数，默认就是前台执行
prompt: "分析代码结构"
```

### 模式2：后台异步执行

**特点**：立即返回，后台运行，完成时通知

```python
# AI调用
Agent(
    prompt="运行所有测试",
    run_in_background=True
)

# 执行流程
主Agent调用AgentTool.execute()
    ↓
创建SubAgent
    ↓
task_id = task_manager.launch(sub_agent)  # 立即返回
    ↓
返回task_id给主Agent
    ↓
主Agent继续执行其他任务
    ↓
[SubAgent在后台运行]
    ↓
完成时：notify_queue.put(task_id)
    ↓
主Agent下一轮收到通知
```

**使用场景**：
- 任务耗时长（测试、编译、大文件处理）
- 主Agent不需要立即等结果
- 多个任务可以并行

**配置**：
```yaml
prompt: "运行完整测试套件"
run_in_background: true
```

### 模式3：Fork模式（上下文继承）

**特点**：继承父Agent的完整对话历史

```python
# AI调用（不指定subagent_type）
Agent(prompt="基于我们刚才的讨论，实现功能A")

# 执行流程
检测：subagent_type is None
    ↓
build_forked_messages(parent_conversation, prompt)
    ↓
复制父Agent的完整对话历史
    ↓
添加Fork专用指令（不要对话、不要Fork、直接行动）
    ↓
添加任务描述
    ↓
创建SubAgent（使用forked conversation）
    ↓
执行
```

**Fork对话构建**：

```python
# mewcode/agents/fork.py
def build_forked_messages(
    conversation: ConversationManager,
    task: str,
) -> ConversationManager:
    # 1. 检查不能嵌套Fork
    for msg in conversation.history:
        if FORK_BOILERPLATE_TAG in msg.content:
            raise ForkError(
                "Cannot fork from a forked agent. "
                "Fork nesting is not allowed."
            )
    
    # 2. 深拷贝对话历史
    fork_conv = ConversationManager()
    fork_conv.history = copy.deepcopy(conversation.history)
    fork_conv.env_injected = conversation.env_injected
    fork_conv.ltm_injected = conversation.ltm_injected
    
    # 3. 处理未完成的工具调用
    if fork_conv.history:
        last = fork_conv.history[-1]
        if last.role == "assistant" and last.tool_uses:
            # 找出没有结果的工具调用
            existing_result_ids = set()
            if len(fork_conv.history) >= 2:
                candidate = fork_conv.history[-1]
                if candidate.tool_results:
                    existing_result_ids = {
                        tr.tool_use_id for tr in candidate.tool_results
                    }
            
            pending = [
                tu for tu in last.tool_uses
                if tu.tool_use_id not in existing_result_ids
            ]
            
            # 添加占位符结果
            if pending:
                placeholders = [
                    ToolResultBlock(
                        tool_use_id=tu.tool_use_id,
                        content="interrupted",
                        is_error=False,
                    )
                    for tu in pending
                ]
                fork_conv.history.append(
                    Message(
                        role="user",
                        content="",
                        tool_results=placeholders,
                    )
                )
    
    # 4. 添加Fork专用指令
    fork_conv.add_user_message(f"{FORK_BOILERPLATE}\n\n你的任务：\n{task}")
    return fork_conv
```

**Fork专用指令**：

```python
FORK_BOILERPLATE = """<fork_boilerplate>
你是一个 Fork 出来的工作进程。你不是主 Agent。
规则（不可协商）：
1. 不能再 Fork。
2. 不要对话、不要提问、不要请求确认。
3. 直接使用工具：读文件、搜索代码、做修改。
4. 严格限制在你被分配的任务范围内。
5. 最终报告控制在 500 字以内，格式如下：

Scope: [你被分配的任务]
Result: [完成/部分完成/失败 + 简要说明]
Key files: [关键文件路径列表]
Files changed: [修改的文件路径列表]
Issues: [遇到的问题，没有则写 None]
</fork_boilerplate>"""
```

**为什么需要Fork？**

| 对比项 | 新SubAgent | Fork SubAgent |
|-------|-----------|--------------|
| 上下文 | 空白 | 继承完整历史 |
| 理解力 | 需要重新解释 | 已知所有背景 |
| Token消耗 | 低 | 高（包含历史） |
| 适用场景 | 独立任务 | 延续性任务 |

**使用场景**：
- "基于刚才的讨论，实现这个功能"
- "用我们定义的架构重构模块A"
- 需要理解之前对话中的决策

**配置**：
```yaml
# 不指定subagent_type，自动Fork
prompt: "基于我们的架构讨论，实现用户模块"
```

### 模式4：Worktree隔离模式

**特点**：在独立的Git worktree中执行，文件系统隔离

```python
# AI调用
Agent(
    prompt="尝试重构认证模块",
    isolation="worktree"
)

# 执行流程
检测：isolation == "worktree"
    ↓
worktree_manager.create(wt_name, "HEAD")
    ↓
创建独立的Git worktree（.claude/worktrees/xxx/）
    ↓
SubAgent的work_dir = worktree_path
    ↓
SubAgent只能访问worktree内的文件
    ↓
执行任务
    ↓
检查是否有变更
    ↓
有变更 → 保留worktree
    无变更 → 自动删除
```

**隔离机制**：

```python
# SubAgent的权限沙箱
checker = PermissionChecker(
    sandbox=PathSandbox(wt.path),  # 限制在worktree目录
    mode=PermissionMode.DONT_ASK,
)

# SubAgent看到的提示
notice = build_worktree_notice(parent_work_dir, wt.path)
"""
You are working in an isolated Git worktree.
Your working directory: /path/to/.claude/worktrees/wt-abc123
Parent project: /path/to/project

All file paths you use MUST be relative to your current working directory.
Do NOT use absolute paths from the original project.
"""
```

**使用场景**：
- 实验性修改（可能失败）
- 危险操作（大规模重构）
- 需要回滚的任务
- 并行尝试多种方案

**配置**：
```yaml
prompt: "尝试将同步API改为异步"
isolation: "worktree"
```

---

## 第二步：TaskManager - 后台任务管理

### 数据模型

```python
# mewcode/agents/task_manager.py
@dataclass
class ProgressInfo:
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_activity: str = ""


@dataclass
class BackgroundTask:
    id: str                      # 任务ID（8字符）
    name: str                    # 任务名称
    agent: Agent                 # 执行的Agent实例
    task: str                    # 任务描述
    status: str = "running"      # 状态：running/completed/failed/cancelled
    result: str = ""             # 执行结果
    start_time: float            # 开始时间
    end_time: float | None = None  # 结束时间
    cancel: Callable            # 取消函数
    progress: ProgressInfo       # 进度信息
```

### 启动后台任务

```python
class TaskManager:
    def __init__(self):
        self._tasks: dict[str, BackgroundTask] = {}
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()
        self._async_tasks: dict[str, asyncio.Task[None]] = {}
    
    def launch(
        self,
        agent: Agent,
        task: str,
        name: str = "",
        fork_conversation: Any = None,
    ) -> str:
        # 1. 生成任务ID
        task_id = uuid.uuid4().hex[:8]
        
        # 2. 创建任务记录
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task,
        )
        self._tasks[task_id] = bg
        
        # 3. 创建asyncio任务（在后台运行）
        async_task = asyncio.create_task(
            self._run_background(task_id, fork_conversation)
        )
        self._async_tasks[task_id] = async_task
        
        # 4. 设置取消函数
        bg.cancel = async_task.cancel
        
        return task_id
```

### 后台执行逻辑

```python
async def _run_background(
    self, task_id: str, fork_conversation: Any = None
) -> None:
    bg = self._tasks.get(task_id)
    if bg is None:
        return
    
    try:
        # 1. 执行Agent
        if fork_conversation is not None:
            # Fork模式：使用已有对话
            result = await bg.agent.run_to_completion("", fork_conversation)
        else:
            # 普通模式：新对话
            result = await bg.agent.run_to_completion(bg.task)
        
        bg.result = result
        bg.status = "completed"
        
        # 2. 如果是Team成员，处理消息循环
        if bg.agent.team_name and bg.agent._team_manager:
            mailbox = bg.agent._team_manager.get_mailbox(bg.agent.team_name)
            if mailbox:
                # 2.1 通知lead已完成初始任务
                from mewcode.teams.mailbox import create_message
                msg = create_message(
                    from_agent=bg.name,
                    to_agent="lead",
                    content=f"[idle] {bg.name}: completed initial task",
                    summary=f"{bg.name} idle",
                )
                mailbox.write("lead", msg)
                
                # 2.2 进入消息监听循环（最多60秒）
                for _ in range(60):
                    await asyncio.sleep(1)
                    
                    # 检查是否有新消息
                    msgs = mailbox.consume(bg.agent.agent_id)
                    if not msgs:
                        continue
                    
                    # 处理消息
                    prompt = "\n\n".join(
                        f"[Message from {m.from_agent}] {m.content}" 
                        for m in msgs
                    )
                    result = await bg.agent.run_to_completion(prompt)
                    bg.result = result
                    
                    # 通知完成
                    msg = create_message(
                        from_agent=bg.name,
                        to_agent="lead",
                        content=f"[idle] {bg.name}: completed follow-up",
                        summary=f"{bg.name} idle",
                    )
                    mailbox.write("lead", msg)
    
    except asyncio.CancelledError:
        bg.status = "cancelled"
        bg.result = "Task was cancelled"
    
    except Exception as e:
        log.error("Background task %s failed: %s", task_id, e)
        bg.status = "failed"
        bg.result = f"Error: {e}"
    
    finally:
        # 3. 更新状态
        bg.end_time = time.monotonic()
        bg.progress.input_tokens = bg.agent.total_input_tokens
        bg.progress.output_tokens = bg.agent.total_output_tokens
        
        # 4. 清理asyncio任务
        self._async_tasks.pop(task_id, None)
        
        # 5. 发送完成通知
        await self._notify_queue.put(task_id)
```

### 任务管理操作

```python
class TaskManager:
    def get(self, task_id: str) -> BackgroundTask | None:
        """获取任务信息"""
        return self._tasks.get(task_id)
    
    def list_tasks(self) -> list[BackgroundTask]:
        """列出所有任务"""
        return list(self._tasks.values())
    
    def cancel(self, task_id: str) -> bool:
        """取消任务"""
        bg = self._tasks.get(task_id)
        if bg is None or bg.status != "running":
            return False
        
        async_task = self._async_tasks.get(task_id)
        if async_task and not async_task.done():
            async_task.cancel()
            return True
        
        return False
    
    def poll_completed(self) -> list[BackgroundTask]:
        """轮询已完成的任务"""
        completed: list[BackgroundTask] = []
        while not self._notify_queue.empty():
            try:
                task_id = self._notify_queue.get_nowait()
                bg = self._tasks.get(task_id)
                if bg is not None:
                    completed.append(bg)
            except asyncio.QueueEmpty:
                break
        return completed
```

### 通知机制

```
SubAgent完成
    ↓
notify_queue.put(task_id)
    ↓
主Agent下一轮turn_start时
    ↓
poll_completed()
    ↓
检查notify_queue
    ↓
有完成的任务
    ↓
添加system_reminder
    "Background task completed: [task_name]
     Result: [result_preview]"
    ↓
主Agent收到通知，可以处理结果
```

---

## 第三步：TraceManager - Agent调用追踪

### 为什么需要追踪？

当有多层SubAgent嵌套时：

```
MainAgent (ID: abc123)
    ├─ SubAgent-1 (ID: def456, parent: abc123)
    │   └─ SubAgent-1-1 (ID: ghi789, parent: def456)
    ├─ SubAgent-2 (ID: jkl012, parent: abc123)
    └─ SubAgent-3 (ID: mno345, parent: abc123)
```

**问题**：
- 如何知道哪个Agent是谁的子Agent？
- 如何统计整个调用链的Token消耗？
- 如何追踪任务的执行状态？

### 数据模型

```python
# mewcode/agents/trace.py
@dataclass
class TraceNode:
    agent_id: str                # Agent唯一ID（12字符）
    parent_id: str | None        # 父Agent ID
    trace_id: str                # 追踪ID（同一调用链共享）
    agent_type: str              # Agent类型（fork/general/security-reviewer）
    input_tokens: int = 0        # 输入Token
    output_tokens: int = 0       # 输出Token
    tool_call_count: int = 0     # 工具调用次数
    start_time: float            # 开始时间
    end_time: float | None = None  # 结束时间
    status: str = "running"      # 状态：running/completed/failed
```

### 核心操作

```python
class TraceManager:
    def __init__(self):
        self._nodes: dict[str, TraceNode] = {}
    
    def create(
        self,
        agent_type: str,
        parent_id: str | None = None,
        trace_id: str | None = None,
    ) -> TraceNode:
        """创建新的追踪节点"""
        # 生成唯一ID
        agent_id = uuid.uuid4().hex[:12]
        
        # 如果没有trace_id，说明是根Agent
        if trace_id is None:
            trace_id = uuid.uuid4().hex[:12]
        
        node = TraceNode(
            agent_id=agent_id,
            parent_id=parent_id,
            trace_id=trace_id,
            agent_type=agent_type,
        )
        self._nodes[agent_id] = node
        return node
    
    def update(self, agent_id: str, **kwargs: int | str) -> None:
        """更新节点信息"""
        node = self._nodes.get(agent_id)
        if node is None:
            return
        for key, value in kwargs.items():
            if hasattr(node, key):
                setattr(node, key, value)
    
    def complete(self, agent_id: str, status: str = "completed") -> None:
        """标记节点完成"""
        node = self._nodes.get(agent_id)
        if node is None:
            return
        node.end_time = time.monotonic()
        node.status = status
    
    def get_tree(self, trace_id: str) -> list[TraceNode]:
        """获取整个调用树"""
        return [n for n in self._nodes.values() if n.trace_id == trace_id]
    
    def get_total_tokens(self, trace_id: str) -> tuple[int, int]:
        """统计调用链的总Token"""
        total_in = 0
        total_out = 0
        for node in self._nodes.values():
            if node.trace_id == trace_id:
                total_in += node.input_tokens
                total_out += node.output_tokens
        return total_in, total_out
```

### 使用示例

```python
# 在AgentTool中创建SubAgent时
trace_node = self._trace_manager.create(
    agent_type=definition.agent_type,
    parent_id=self._parent_agent.agent_id,
    trace_id=self._parent_agent.trace_id or self._parent_agent.agent_id,
)
sub_agent.agent_id = trace_node.agent_id
sub_agent.trace_id = self._parent_agent.trace_id

# SubAgent完成后更新统计
self._trace_manager.update(
    trace_node.agent_id,
    input_tokens=sub_agent.total_input_tokens,
    output_tokens=sub_agent.total_output_tokens,
)
self._trace_manager.complete(trace_node.agent_id, "completed")
```

---

## 第四步：AgentTool - SubAgent调用入口

### 工具定义

```python
# mewcode/tools/agent_tool.py
class AgentToolParams(BaseModel):
    prompt: str                      # 任务描述（必需）
    description: str                 # 任务说明（必需）
    subagent_type: str | None = None # Agent类型（可选，不指定则Fork）
    model: str | None = None         # 模型覆盖（可选）
    run_in_background: bool = False  # 是否后台运行
    name: str | None = None          # Agent名称
    isolation: str | None = None     # 隔离模式（worktree）
    team_name: str | None = None     # 团队名称（创建teammate）


class AgentTool(Tool):
    name = "Agent"
    description = (
        "Launch a sub-agent to handle a task in an isolated context. "
        "Use subagent_type to select a predefined agent type, "
        "or leave it empty to fork the current conversation. "
        "Use team_name to spawn a teammate in an existing team."
    )
    params_model = AgentToolParams
    category = "command"
    is_concurrency_safe = False
```

### 执行流程

```python
async def execute(self, params: BaseModel) -> ToolResult:
    p: AgentToolParams = params
    
    # 分支1：创建Team成员
    if p.team_name:
        return await self._execute_as_teammate(p)
    
    # 分支2：Worktree隔离模式
    isolation = ""
    if p.subagent_type:
        defn = self._agent_loader.get(p.subagent_type)
        if defn and defn.isolation:
            isolation = defn.isolation
    
    if isolation == "worktree":
        return await self._execute_with_worktree(p)
    
    # 分支3：普通SubAgent或Fork
    return await self._execute_normal(p)
```

### 普通SubAgent执行

```python
async def _execute_normal(self, p: AgentToolParams) -> ToolResult:
    # 1. 确定是新Agent还是Fork
    conversation: ConversationManager
    definition: AgentDef | None = None
    
    if p.subagent_type:
        # 新SubAgent：加载定义
        definition = self._agent_loader.get(p.subagent_type)
        if definition is None:
            return ToolResult(
                output=f"Unknown agent type: '{p.subagent_type}'",
                is_error=True,
            )
        conversation = ConversationManager()
    else:
        # Fork模式：继承对话
        if not self._enable_fork:
            return ToolResult(
                output="Fork mode is not enabled",
                is_error=True,
            )
        
        parent_conv = getattr(self._parent_agent, '_current_conversation', None)
        if parent_conv is None:
            return ToolResult(
                output="Cannot fork: no active conversation",
                is_error=True,
            )
        
        conversation = build_forked_messages(parent_conv, p.prompt)
        
        definition = AgentDef(
            agent_type="fork",
            system_prompt="",
            model="inherit",
            max_turns=self._parent_agent.max_iterations,
            permission_mode="dontAsk",
        )
    
    # 2. 选择LLM客户端
    client = self._select_llm(p, definition)
    
    # 3. 判断是否后台运行
    is_background = p.run_in_background or definition.background
    if self._enable_fork:
        is_background = True  # Fork总是后台
    
    # 4. 过滤工具
    _base_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
    filtered_registry = resolve_agent_tools(
        _base_registry, definition, is_background
    )
    
    # 5. 创建权限检查器
    pm_str = definition.permission_mode
    pm_enum = getattr(
        PermissionMode,
        PERMISSION_MODE_MAP.get(pm_str, "DEFAULT"),
        PermissionMode.DEFAULT,
    )
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(self._parent_agent.work_dir),
        rule_engine=RuleEngine(),
        mode=pm_enum,
    )
    
    # 6. 创建SubAgent
    sub_agent = AgentClass(
        client=client,
        registry=filtered_registry,
        protocol=self._parent_agent.protocol,
        work_dir=self._parent_agent.work_dir,
        max_iterations=definition.max_turns,
        permission_checker=checker,
        context_window=self._parent_agent.context_window,
        instructions_content=definition.system_prompt,
        hook_engine=self._parent_agent.hook_engine,
    )
    sub_agent.parent_id = self._parent_agent.agent_id
    sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id
    
    # 7. Fork继承替换状态（保持prompt cache一致性）
    if p.subagent_type is None:
        from mewcode.context import clone_replacement_state
        sub_agent.replacement_state = clone_replacement_state(
            self._parent_agent.replacement_state
        )
    
    # 8. 注册追踪节点
    trace_node = self._trace_manager.create(
        agent_type=definition.agent_type,
        parent_id=self._parent_agent.agent_id,
        trace_id=sub_agent.trace_id,
    )
    sub_agent.agent_id = trace_node.agent_id
    
    agent_name = p.name or p.subagent_type or f"agent-{trace_node.agent_id}"
    is_fork = p.subagent_type is None
    
    # 9. 后台执行
    if is_background:
        if is_fork:
            sub_agent._fork_conversation = conversation
        
        task_id = self._task_manager.launch(
            agent=sub_agent,
            task="" if is_fork else p.prompt,
            name=agent_name,
            fork_conversation=conversation if is_fork else None,
        )
        
        return ToolResult(
            output=f"Sub-agent launched in background.\n"
                   f"Task ID: {task_id}\n"
                   f"Agent: {agent_name}\n"
                   f"Type: {definition.agent_type}\n"
                   f"The system will notify automatically when it completes.\n"
                   f"Do NOT wait, sleep, or poll."
        )
    
    # 10. 前台同步执行
    try:
        if is_fork:
            result_text = await sub_agent.run_to_completion("", conversation)
        else:
            result_text = await sub_agent.run_to_completion(p.prompt)
    except Exception as e:
        self._trace_manager.complete(trace_node.agent_id, "failed")
        return ToolResult(
            output=f"Sub-agent failed: {e}",
            is_error=True,
        )
    
    # 11. 更新追踪信息
    self._trace_manager.update(
        trace_node.agent_id,
        input_tokens=sub_agent.total_input_tokens,
        output_tokens=sub_agent.total_output_tokens,
    )
    self._trace_manager.complete(trace_node.agent_id, "completed")
    
    return ToolResult(output=result_text or "(sub-agent returned no output)")
```

### LLM模型选择

```python
def _select_llm(
    self,
    params: AgentToolParams,
    definition: AgentDef,
) -> LLMClient:
    """选择LLM客户端"""
    # 优先级：参数指定 > 定义指定 > 继承父Agent
    model_override = params.model or (
        definition.model if definition.model != "inherit" else None
    )
    
    if model_override and model_override != "inherit":
        # 尝试创建新客户端
        client = self._create_client_for_model(model_override)
        if client is not None:
            return client
    
    # 使用父Agent的客户端
    return self._parent_agent.client


def _create_client_for_model(self, model_alias: str) -> LLMClient | None:
    """为特定模型创建客户端"""
    if self._provider_config is None:
        return None
    
    # 模型别名映射
    model_map = {
        "haiku": "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-4-6-20250514",
        "opus": "claude-opus-4-6-20250514",
    }
    model_id = model_map.get(model_alias, model_alias)
    
    # 创建新配置
    config = ProviderConfig(
        name=f"sub-{model_alias}",
        protocol=self._provider_config.protocol,
        base_url=self._provider_config.base_url,
        model=model_id,
        api_key=self._provider_config.api_key,
        context_window=self._provider_config.context_window,
    )
    
    try:
        return create_client(config)
    except Exception:
        return None
```

---

## 第五步：Worktree隔离执行

### 为什么需要Worktree？

```
场景：尝试大规模重构
    ↓
问题1：可能失败，不想污染主分支
问题2：想同时尝试多种方案
问题3：需要安全回滚
    ↓
解决：在独立的Git worktree中执行
```

### Worktree是什么？

Git worktree允许同一个仓库有多个工作目录：

```
project/
├── .git/                    # Git数据库
├── src/                     # 主工作目录
├── .claude/worktrees/
│   ├── wt-abc123/          # Worktree 1（独立分支）
│   │   └── src/
│   └── wt-def456/          # Worktree 2（独立分支）
│       └── src/
```

每个worktree：
- 独立的工作目录
- 独立的分支
- 共享.git数据库（节省空间）
- 互不干扰

### 执行流程

```python
async def _execute_with_worktree(self, p: AgentToolParams) -> ToolResult:
    # 1. 检查WorktreeManager是否可用
    if self._worktree_manager is None:
        return ToolResult(
            output="Worktree isolation is not available",
            is_error=True,
        )
    
    # 2. 加载Agent定义
    definition: AgentDef | None = None
    if p.subagent_type:
        definition = self._agent_loader.get(p.subagent_type)
        if definition is None:
            return ToolResult(
                output=f"Unknown agent type: '{p.subagent_type}'",
                is_error=True,
            )
    else:
        # 默认worktree agent定义
        definition = AgentDef(
            agent_type="worktree-agent",
            system_prompt="",
            model="inherit",
            max_turns=self._parent_agent.max_iterations,
            permission_mode="dontAsk",
        )
    
    # 3. 创建worktree
    wt_name = generate_worktree_name()  # 如：wt-20240125-143022
    try:
        wt = await self._worktree_manager.create(wt_name, "HEAD")
    except Exception as e:
        return ToolResult(
            output=f"Failed to create worktree: {e}",
            is_error=True,
        )
    
    # 4. 构建worktree提示
    notice = build_worktree_notice(self._parent_agent.work_dir, wt.path)
    """
    You are working in an isolated Git worktree.
    Your working directory: /project/.claude/worktrees/wt-abc123
    Parent project: /project
    
    All file paths you use MUST be relative to your current working directory.
    Do NOT use absolute paths from the original project.
    """
    
    task = notice + "\n\n" + p.prompt
    
    # 5. 创建SubAgent
    client = self._select_llm(p, definition)
    
    _base_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
    filtered_registry = resolve_agent_tools(_base_registry, definition, False)
    
    # 权限沙箱限制在worktree目录
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(wt.path),  # 关键：限制路径
        rule_engine=RuleEngine(),
        mode=PermissionMode.DONT_ASK,
    )
    
    sub_agent = AgentClass(
        client=client,
        registry=filtered_registry,
        protocol=self._parent_agent.protocol,
        work_dir=wt.path,  # 工作目录指向worktree
        max_iterations=definition.max_turns,
        permission_checker=checker,
        context_window=self._parent_agent.context_window,
        instructions_content=definition.system_prompt,
        hook_engine=self._parent_agent.hook_engine,
    )
    sub_agent.parent_id = self._parent_agent.agent_id
    sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id
    
    # 6. 注册追踪
    trace_node = self._trace_manager.create(
        agent_type=definition.agent_type,
        parent_id=self._parent_agent.agent_id,
        trace_id=sub_agent.trace_id,
    )
    sub_agent.agent_id = trace_node.agent_id
    
    # 7. 执行任务
    try:
        result_text = await sub_agent.run_to_completion(task)
    except Exception as e:
        self._trace_manager.complete(trace_node.agent_id, "failed")
        return ToolResult(
            output=f"Sub-agent in worktree failed: {e}",
            is_error=True,
        )
    
    # 8. 更新追踪
    self._trace_manager.update(
        trace_node.agent_id,
        input_tokens=sub_agent.total_input_tokens,
        output_tokens=sub_agent.total_output_tokens,
    )
    self._trace_manager.complete(trace_node.agent_id, "completed")
    
    # 9. 自动清理worktree
    cleanup = await self._worktree_manager.auto_cleanup(wt_name, wt.head_commit)
    if cleanup.kept:
        # 有变更，保留worktree
        result_text = (result_text or "") + (
            f"\n[Worktree preserved at {cleanup.path}, branch {cleanup.branch}]"
        )
    # 无变更，自动删除
    
    return ToolResult(output=result_text or "(sub-agent returned no output)")
```

### 自动清理逻辑

```python
# WorktreeManager.auto_cleanup()
async def auto_cleanup(self, wt_name: str, original_commit: str):
    """自动清理worktree"""
    wt = self._worktrees.get(wt_name)
    if not wt:
        return
    
    # 检查是否有变更
    current_commit = get_current_commit(wt.path)
    has_uncommitted = has_uncommitted_changes(wt.path)
    
    if current_commit != original_commit or has_uncommitted:
        # 有变更：保留worktree
        return CleanupResult(
            kept=True,
            path=wt.path,
            branch=wt.branch,
        )
    else:
        # 无变更：删除worktree
        await self.remove(wt_name, discard_changes=True)
        return CleanupResult(kept=False)
```

---

## 第六步：完整执行流程

### 场景：并行代码审查

```
用户: "审查这个项目的代码质量"
    ↓
主Agent: "我需要从多个维度审查"
    决定：启动3个SubAgent并行工作
    ↓
调用1: Agent(
    prompt="审查代码安全性",
    subagent_type="security-reviewer",
    run_in_background=True
)
    ↓
    AgentTool.execute()
        ├─ 加载定义: security-reviewer
        ├─ 创建SubAgent（限制工具、设置权限）
        ├─ 创建TraceNode
        ├─ task_id_1 = task_manager.launch()
        └─ 返回: "Sub-agent launched, task_id: a1b2c3d4"
    ↓
调用2: Agent(
    prompt="审查代码性能",
    subagent_type="performance-reviewer",
    run_in_background=True
)
    ↓
    task_id_2 = task_manager.launch()
    返回: "Sub-agent launched, task_id: e5f6g7h8"
    ↓
调用3: Agent(
    prompt="审查代码可读性",
    subagent_type="readability-reviewer",
    run_in_background=True
)
    ↓
    task_id_3 = task_manager.launch()
    返回: "Sub-agent launched, task_id: i9j0k1l2"
    ↓
主Agent: "已启动3个审查任务，我继续其他工作"
    ↓
[3个SubAgent并行执行中...]
    SubAgent-1: 扫描安全漏洞
    SubAgent-2: 分析性能瓶颈
    SubAgent-3: 检查代码风格
    ↓
[SubAgent-1完成]
    task_manager._run_background()
        └─ notify_queue.put("a1b2c3d4")
    ↓
[SubAgent-2完成]
    notify_queue.put("e5f6g7h8")
    ↓
[SubAgent-3完成]
    notify_queue.put("i9j0k1l2")
    ↓
主Agent下一轮turn_start:
    completed = task_manager.poll_completed()
    → 3个任务都完成了
    ↓
添加system_reminder:
    "Background task completed: security-reviewer
     Result: Found 3 SQL injection vulnerabilities..."
    
    "Background task completed: performance-reviewer
     Result: Identified 5 slow queries..."
    
    "Background task completed: readability-reviewer
     Result: 120 functions need better naming..."
    ↓
主Agent看到3个结果:
    "根据审查结果，我发现以下问题..."
    汇总报告、优先级排序、生成修复计划
```

---

## 实战案例

### 案例1：并行文档搜索

```yaml
# 用户请求
"研究React、Vue、Angular的状态管理方案"

# 主Agent策略
prompt: |
  我需要研究3个框架，启动3个SubAgent并行搜索
  
  Agent(
    prompt="研究React的Redux、Context、Zustand状态管理",
    description="Research React state management",
    run_in_background=True,
    name="react-researcher"
  )
  
  Agent(
    prompt="研究Vue的Vuex、Pinia、Composition API状态管理",
    description="Research Vue state management",
    run_in_background=True,
    name="vue-researcher"
  )
  
  Agent(
    prompt="研究Angular的NgRx、Services、Signals状态管理",
    description="Research Angular state management",
    run_in_background=True,
    name="angular-researcher"
  )

# 结果
3个SubAgent并行搜索文档
总时间 = max(React搜索, Vue搜索, Angular搜索)
而不是 = React搜索 + Vue搜索 + Angular搜索
节省时间 = 66%
```

### 案例2：隔离实验不同方案

```yaml
# 用户请求
"对比Redis缓存和Memcached缓存的性能"

# 主Agent策略
prompt: |
  我启动2个隔离的worktree，分别实现两种方案
  
  Agent(
    prompt="将缓存层替换为Redis，实现并测试",
    isolation="worktree",
    description="Implement Redis caching",
    name="redis-experiment"
  )
  
  Agent(
    prompt="将缓存层替换为Memcached，实现并测试",
    isolation="worktree",
    description="Implement Memcached caching",
    name="memcached-experiment"
  )

# 执行
Worktree 1: .claude/worktrees/wt-redis/
    ├─ 修改 cache.py 使用Redis
    ├─ 运行测试
    └─ 记录性能数据

Worktree 2: .claude/worktrees/wt-memcached/
    ├─ 修改 cache.py 使用Memcached
    ├─ 运行测试
    └─ 记录性能数据

主分支：完全不受影响

# 结果对比
主Agent收到两份完整的实现和测试结果
可以安全对比，选择最佳方案
不需要的worktree自动清理
```

### 案例3：Fork延续任务

```yaml
# 对话历史
用户: "我们要用微服务架构，每个服务独立部署"
AI: "好的，我建议用API Gateway、服务注册、配置中心..."
用户: "就按这个架构，实现用户服务"

# 主Agent决策
prompt: |
  用户要基于我们刚讨论的架构实现用户服务
  这需要理解之前的架构决策
  我使用Fork模式，让SubAgent继承完整上下文
  
  Agent(
    prompt="实现用户服务，包括注册、登录、权限管理",
    description="Implement user service"
  )
  # 不指定subagent_type，自动Fork

# 执行
build_forked_messages(parent_conversation, task)
    ├─ 复制完整对话历史（包括架构讨论）
    ├─ 添加Fork指令（不要对话、直接行动）
    └─ 添加任务："实现用户服务..."

SubAgent继承了所有架构知识:
    ✅ 知道用API Gateway
    ✅ 知道用服务注册
    ✅ 知道用JWT认证
    ✅ 直接按既定架构实现
```

### 案例4：专业Agent协作

```yaml
# 用户请求
"全面审查这个Web应用"

# 主Agent策略
prompt: |
  我需要多维度审查，每个SubAgent专注一个领域
  
  Agent(
    prompt="审查前端代码：React组件、性能、可访问性",
    subagent_type="frontend-reviewer",
    run_in_background=True
  )
  
  Agent(
    prompt="审查后端代码：API设计、数据库查询、错误处理",
    subagent_type="backend-reviewer",
    run_in_background=True
  )
  
  Agent(
    prompt="审查安全性：XSS、CSRF、SQL注入、认证授权",
    subagent_type="security-reviewer",
    run_in_background=True
  )
  
  Agent(
    prompt="审查基础设施：Docker配置、CI/CD、监控告警",
    subagent_type="devops-reviewer",
    run_in_background=True
  )

# 每个SubAgent有专门的:
- system_prompt（领域知识）
- tools（专业工具）
- permission_mode（权限级别）

# 结果
4个专业Agent并行工作
每个聚焦自己的领域
主Agent汇总所有发现
生成综合报告
```

---

## 代码亮点

### 亮点1：统一的调用接口

```python
# 同一个Agent工具，支持4种模式

# 模式1：前台同步
Agent(prompt="简单任务")

# 模式2：后台异步
Agent(prompt="耗时任务", run_in_background=True)

# 模式3：Fork继承
Agent(prompt="延续任务")  # 不指定type

# 模式4：Worktree隔离
Agent(prompt="实验任务", isolation="worktree")
```

### 亮点2：自动通知机制

```python
# 不需要主Agent轮询
# TaskManager自动通知

async def _run_background(self, task_id: str):
    try:
        result = await bg.agent.run_to_completion(bg.task)
        bg.status = "completed"
    finally:
        # 自动推送通知
        await self._notify_queue.put(task_id)

# 主Agent在下一轮自动收到
completed = task_manager.poll_completed()
```

### 亮点3：追踪树结构

```python
# 自动维护调用关系
MainAgent (trace_id: root-123)
    ├─ SubAgent-1 (parent_id: main, trace_id: root-123)
    ├─ SubAgent-2 (parent_id: main, trace_id: root-123)
    └─ SubAgent-3 (parent_id: main, trace_id: root-123)
        └─ SubAgent-3-1 (parent_id: sub-3, trace_id: root-123)

# 统计整条链的Token
total_in, total_out = trace_manager.get_total_tokens("root-123")
```

### 亮点4：Fork防嵌套

```python
# 不允许Fork的SubAgent再Fork
def build_forked_messages(conversation, task):
    for msg in conversation.history:
        if FORK_BOILERPLATE_TAG in msg.content:
            raise ForkError(
                "Cannot fork from a forked agent. "
                "Fork nesting is not allowed."
            )
```

**为什么？**
- Fork继承完整历史（Token消耗大）
- Fork嵌套会指数级增长
- Fork的Fork没有实际意义

### 亮点5：Worktree自动清理

```python
# 智能决策：保留还是删除
cleanup = await worktree_manager.auto_cleanup(wt_name, original_commit)

# 逻辑
if has_changes:
    keep_worktree()  # 有价值，保留
    return path, branch
else:
    delete_worktree()  # 无变更，清理
```

### 亮点6：权限沙箱隔离

```python
# Worktree SubAgent被限制在独立目录
checker = PermissionChecker(
    sandbox=PathSandbox(wt.path),  # 只能访问worktree
    mode=PermissionMode.DONT_ASK,
)

# 尝试访问父目录
sub_agent.execute(ReadFile("/parent/project/file.txt"))
→ Error: Path outside sandbox
```

---

## 性能优化

### 优化1：并行执行

```
串行（无SubAgent）:
任务1(10s) → 任务2(10s) → 任务3(10s) = 30s

并行（3个SubAgent）:
任务1(10s) ┐
任务2(10s) ├→ max(10s, 10s, 10s) = 10s
任务3(10s) ┘

提速：3倍
```

### 优化2：Fork的Prompt Cache复用

```python
# Fork继承父Agent的replacement_state
if p.subagent_type is None:
    sub_agent.replacement_state = clone_replacement_state(
        self._parent_agent.replacement_state
    )
```

**为什么？**
- 父子共享相同的对话历史前缀
- Prompt Cache可以复用
- 节省Token和响应时间

### 优化3：后台任务的asyncio.Task

```python
# 使用asyncio.create_task而非线程
async_task = asyncio.create_task(
    self._run_background(task_id, fork_conversation)
)

# 优势
- 轻量级（协程）
- 共享事件循环
- 方便取消
- 内存占用小
```

---

## 总结

SubAgent系统解决了什么问题：

1. **并行执行** - 多个任务同时进行，提升速度
2. **任务隔离** - Worktree隔离实验，互不干扰
3. **上下文继承** - Fork模式延续对话
4. **专业分工** - 不同Agent类型做不同的事
5. **追踪管理** - 完整的调用链追踪

**核心组件**：

```
AgentTool (调用入口)
    ↓
TaskManager (后台任务管理)
    ↓
TraceManager (调用链追踪)
    ↓
Fork System (上下文继承)
    ↓
Worktree System (隔离执行)
```

**四种模式**：

| 模式 | 特点 | 使用场景 |
|------|------|---------|
| 前台同步 | 阻塞等待 | 需要结果才能继续 |
| 后台异步 | 立即返回 | 耗时长、可并行 |
| Fork继承 | 继承上下文 | 延续性任务 |
| Worktree隔离 | 文件隔离 | 实验、回滚 |

下一章我们将学习：如何通过Team系统实现长期运行的Agent团队协作？
