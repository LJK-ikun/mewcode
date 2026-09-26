# CH12: SubAgent - 子Agent与任务分发

## 从一次真实调用说起

你在主Agent中说："帮我同时研究React和Vue的文档"

```python
# 主Agent调用
Agent(prompt="研究React文档中的Hook用法")
Agent(prompt="研究Vue文档中的组合式API")
```

**现在我带你看看系统内部到底发生了什么**：

```
[时刻T0] 主Agent收到用户请求
    ↓
[T1] 主Agent的LLM返回工具调用: Agent(prompt="研究React...")
    ↓
[T2] agent_tool.py的execute()方法被调用
    ↓
[T3] 创建SubAgent对象 + 复制工具注册表
    ↓
[T4] 判断执行模式：
    - 有subagent_type? → 新对话
    - 没有? → fork父Agent的上下文
    - 有isolation="worktree"? → 创建Git工作树
    - 有run_in_background=True? → 后台执行
    ↓
[T5] 启动SubAgent执行
    - 同步: await sub_agent.run_to_completion()
    - 异步: task_manager.launch()
    ↓
[T6] SubAgent独立运行（有自己的对话历史）
    ↓
[T7] SubAgent完成，返回结果给主Agent
    ↓
[T8] 主Agent收到结果，继续处理下一个任务
```

**这就是SubAgent系统的核心**：每个SubAgent是完全独立的Agent实例，有自己的对话、工具、权限，可以并行运行。

---

## 核心概念：SubAgent执行的4个模式

### 模式1：同步执行（Sync - 默认）

**什么时候用**：你需要SubAgent的结果才能继续

```python
# 用户请求：分析这个文件的复杂度
result = Agent(prompt="分析 utils.py 的圈复杂度")
# ↑ 主Agent会等待SubAgent完成，拿到结果后继续

# 内部流程：
# 1. 主Agent暂停
# 2. SubAgent同步执行：await sub_agent.run_to_completion(prompt)
# 3. SubAgent完成，返回结果
# 4. 主Agent恢复，继续处理
```

**代码路径**：`agent_tool.py:244-262`

```python
# 前台同步执行
try:
    if is_fork:
        result_text = await sub_agent.run_to_completion("", conversation)
    else:
        result_text = await sub_agent.run_to_completion(p.prompt)
except Exception as e:
    self._trace_manager.complete(trace_node.agent_id, "failed")
    return ToolResult(output=f"Sub-agent failed: {e}", is_error=True)

# 更新token统计
self._trace_manager.update(
    trace_node.agent_id,
    input_tokens=sub_agent.total_input_tokens,
    output_tokens=sub_agent.total_output_tokens,
)
self._trace_manager.complete(trace_node.agent_id, "completed")

return ToolResult(output=result_text or "(sub-agent returned no output)")
```

**关键点**：

- 使用 `await` 等待完成
- 结果直接返回给主Agent
- 阻塞主Agent的执行

---

### 模式2：异步后台执行（Background）

**什么时候用**：任务耗时长，不需要立即结果

```python
# 用户请求：后台扫描所有文件的安全漏洞
Agent(
    prompt="扫描整个代码库的SQL注入漏洞",
    run_in_background=True
)
# ↑ 主Agent立即继续，不等待

# 内部流程：
# 1. TaskManager.launch() 创建后台任务
# 2. asyncio.create_task() 启动异步任务
# 3. 主Agent立即返回task_id
# 4. SubAgent在后台独立运行
# 5. 完成后通过notify_queue通知主Agent
```

**代码路径**：`agent_tool.py:225-241`

```python
if is_background:
    if is_fork:
        sub_agent._fork_conversation = conversation
    # 启动后台任务
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
        f"Do NOT wait, sleep, or poll. Report the task ID to the user and move on.",
    )
```

**TaskManager的后台执行**：`task_manager.py:47-69`

```python
def launch(
    self,
    agent: Agent,
    task: str,
    name: str = "",
    fork_conversation: Any = None,
) -> str:
    # 生成唯一task_id
    task_id = uuid.uuid4().hex[:8]
  
    # 创建后台任务对象
    bg = BackgroundTask(
        id=task_id,
        name=name or task_id,
        agent=agent,
        task=task,
    )
    self._tasks[task_id] = bg

    # 启动异步任务
    async_task = asyncio.create_task(
        self._run_background(task_id, fork_conversation)
    )
    self._async_tasks[task_id] = async_task

    bg.cancel = async_task.cancel
    return task_id
```

**后台执行的核心循环**：`task_manager.py:72-130`

```python
async def _run_background(
    self, task_id: str, fork_conversation: Any = None
) -> None:
    bg = self._tasks.get(task_id)
    if bg is None:
        return

    try:
        # 执行SubAgent
        if fork_conversation is not None:
            result = await bg.agent.run_to_completion("", fork_conversation)
        else:
            result = await bg.agent.run_to_completion(bg.task)
    
        bg.result = result
        bg.status = "completed"

        # 如果是团队成员，进入消息循环
        if bg.agent.team_name and bg.agent._team_manager:
            mailbox = bg.agent._team_manager.get_mailbox(bg.agent.team_name)
            if mailbox:
                # 通知团队：我空闲了
                msg = create_message(
                    from_agent=bg.name,
                    to_agent="lead",
                    content=f"[idle] {bg.name}: completed initial task",
                    summary=f"{bg.name} idle",
                )
                mailbox.write("lead", msg)

                # 等待新消息（最多60秒）
                for _ in range(60):
                    await asyncio.sleep(1)
                    msgs = mailbox.consume(bg.agent.agent_id)
                    if not msgs:
                        continue
                
                    # 处理收到的消息
                    prompt = "\n\n".join(
                        f"[Message from {m.from_agent}] {m.content}" for m in msgs
                    )
                    result = await bg.agent.run_to_completion(prompt)
                    bg.result = result
                
                    # 再次通知空闲
                    mailbox.write("lead", msg)

    except asyncio.CancelledError:
        bg.status = "cancelled"
        bg.result = "Task was cancelled"
    except Exception as e:
        log.error("Background task %s failed: %s", task_id, e)
        bg.status = "failed"
        bg.result = f"Error: {e}"
    finally:
        # 记录完成状态
        bg.end_time = time.monotonic()
        bg.progress.input_tokens = bg.agent.total_input_tokens
        bg.progress.output_tokens = bg.agent.total_output_tokens
        self._async_tasks.pop(task_id, None)
    
        # 通知主Agent任务完成
        await self._notify_queue.put(task_id)
```

**关键点**：

- 不阻塞主Agent
- 通过 `asyncio.Queue` 异步通知完成
- 团队成员会进入消息循环，等待协作

---

### 模式3：Fork继承上下文

**什么时候用**：SubAgent需要知道主Agent的对话历史

```python
# 用户：你刚才说的那个Bug，帮我修复一下
Agent(prompt="修复刚才提到的空指针Bug")
# ↑ SubAgent需要知道"刚才提到的Bug"是什么

# 内部流程：
# 1. 检测到subagent_type=None（没指定类型）
# 2. 调用 build_forked_messages() 复制父Agent的对话
# 3. SubAgent继承完整上下文
# 4. SubAgent可以理解"刚才提到的"是什么
```

**代码路径**：`agent_tool.py:134-161`

```python
if p.subagent_type:
    # 新对话：创建空白ConversationManager
    definition = self._agent_loader.get(p.subagent_type)
    if definition is None:
        return ToolResult(output=f"Unknown agent type: '{p.subagent_type}'", is_error=True)
    conversation = ConversationManager()
else:
    # Fork模式：复制父Agent的对话历史
    if not self._enable_fork:
        return ToolResult(output="Fork mode is not enabled.", is_error=True)
  
    try:
        parent_conv = getattr(self._parent_agent, '_current_conversation', None)
        if parent_conv is None:
            return ToolResult(output="Cannot fork: no active conversation.", is_error=True)
    
        # 关键：复制对话历史
        conversation = build_forked_messages(parent_conv, p.prompt)
    except ForkError as e:
        return ToolResult(output=str(e), is_error=True)

    # 创建fork定义
    definition = AgentDef(
        agent_type="fork",
        when_to_use="Forked from parent agent",
        system_prompt="",
        disallowed_tools=[],
        model="inherit",
        max_turns=self._parent_agent.max_iterations,
        permission_mode="dontAsk",
        source="builtin",
    )
```

**Fork的对话复制逻辑**：`agents/fork.py:build_forked_messages()`

```python
def build_forked_messages(
    parent_conv: ConversationManager,
    fork_prompt: str,
) -> ConversationManager:
    """
    复制父Agent的对话历史，添加fork专用指令
    """
    # 1. 检查是否已经是fork（防止fork的fork）
    if _is_already_fork(parent_conv):
        raise ForkError("Cannot fork from a fork agent")
  
    # 2. 复制所有消息
    forked_conv = ConversationManager()
    for msg in parent_conv.messages:
        forked_conv.add_message(msg)
  
    # 3. 添加fork专用的系统提示
    forked_conv.add_message({
        "role": "user",
        "content": FORK_BOILERPLATE + "\n\n" + fork_prompt
    })
  
    return forked_conv
```

**FORK_BOILERPLATE（反嵌套保护）**：

```python
FORK_BOILERPLATE = """
You are now running as a forked sub-agent.
You inherit the full conversation context from the parent.

CRITICAL: Do NOT spawn further sub-agents or forks.
Your job is to execute the task directly, not delegate it.
"""
```

**关键点**：

- 复制父Agent的完整对话历史
- 继承prompt cache（提升性能）
- 防止fork嵌套（fork不能再fork）

---

### 模式4：Worktree隔离执行

**什么时候用**：需要修改文件但不影响主工作区

```python
# 用户：试试把这个API改成异步的
Agent(
    prompt="将 /api/users 改成异步实现",
    isolation="worktree"
)
# ↑ SubAgent在独立的Git工作树中修改代码

# 内部流程：
# 1. WorktreeManager.create() 创建.claude/worktrees/xxx
# 2. Git创建新分支并checkout
# 3. 复制.env等本地配置
# 4. SubAgent在隔离环境中工作
# 5. 完成后自动检测是否有变更
# 6. 有变更 → 保留worktree；无变更 → 自动删除
```

**代码路径**：`agent_tool.py:514-633`

```python
async def _execute_with_worktree(self, p: AgentToolParams) -> ToolResult:
    if self._worktree_manager is None:
        return ToolResult(
            output="Worktree isolation is not available.",
            is_error=True,
        )

    # 1. 加载agent定义
    definition: AgentDef | None = None
    if p.subagent_type:
        definition = self._agent_loader.get(p.subagent_type)
        if definition is None:
            return ToolResult(output=f"Unknown agent type: '{p.subagent_type}'", is_error=True)
    else:
        definition = AgentDef(
            agent_type="worktree-agent",
            when_to_use="Isolated worktree agent",
            system_prompt="",
            disallowed_tools=[],
            model="inherit",
            max_turns=self._parent_agent.max_iterations,
            permission_mode="dontAsk",
            source="builtin",
        )

    # 2. 创建worktree
    wt_name = generate_worktree_name()  # 生成随机名称
    try:
        wt = await self._worktree_manager.create(wt_name, "HEAD")
    except Exception as e:
        return ToolResult(output=f"Failed to create worktree: {e}", is_error=True)

    # 3. 构建任务提示（告诉SubAgent它在worktree中）
    notice = build_worktree_notice(self._parent_agent.work_dir, wt.path)
    task = notice + "\n\n" + p.prompt

    # 4. 创建SubAgent（工作目录 = worktree路径）
    client = self._select_llm(p, definition)
  
    _base_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
    filtered_registry = resolve_agent_tools(_base_registry, definition, False)

    pm_str = definition.permission_mode
    pm_enum = getattr(PermissionMode, PERMISSION_MODE_MAP.get(pm_str, "DEFAULT"), PermissionMode.DEFAULT)
  
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(wt.path),  # 沙箱限制在worktree内
        rule_engine=RuleEngine(),
        mode=pm_enum,
    )

    sub_agent = AgentClass(
        client=client,
        registry=filtered_registry,
        protocol=self._parent_agent.protocol,
        work_dir=wt.path,  # 关键：工作目录是worktree
        max_iterations=definition.max_turns,
        permission_checker=checker,
        context_window=self._parent_agent.context_window,
        instructions_content=definition.system_prompt,
        hook_engine=self._parent_agent.hook_engine,
    )
    sub_agent.parent_id = self._parent_agent.agent_id
    sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id

    # 5. 注册追踪节点
    trace_node = self._trace_manager.create(
        agent_type=definition.agent_type,
        parent_id=self._parent_agent.agent_id,
        trace_id=sub_agent.trace_id,
    )
    sub_agent.agent_id = trace_node.agent_id

    # 6. 执行SubAgent
    try:
        result_text = await sub_agent.run_to_completion(task)
    except Exception as e:
        self._trace_manager.complete(trace_node.agent_id, "failed")
        return ToolResult(output=f"Sub-agent in worktree failed: {e}", is_error=True)

    # 7. 更新统计
    self._trace_manager.update(
        trace_node.agent_id,
        input_tokens=sub_agent.total_input_tokens,
        output_tokens=sub_agent.total_output_tokens,
    )
    self._trace_manager.complete(trace_node.agent_id, "completed")

    # 8. 自动清理逻辑
    cleanup = await self._worktree_manager.auto_cleanup(wt_name, wt.head_commit)
    if cleanup.kept:
        result_text = (result_text or "") + (
            f"\n[Worktree preserved at {cleanup.path}, branch {cleanup.branch}]"
        )

    return ToolResult(output=result_text or "(sub-agent returned no output)")
```

**Worktree自动清理逻辑**：`worktree/manager.py:auto_cleanup()`

```python
async def auto_cleanup(self, name: str, original_head: str) -> CleanupResult:
    """
    决策：保留还是删除worktree？
  
    判断依据：
    1. 有未提交的文件？ → 保留
    2. 有新的commit？ → 保留
    3. 什么都没改？ → 删除
    """
    wt = self._worktrees.get(name)
    if wt is None:
        return CleanupResult(kept=False)
  
    # 检测变更
    has_changes = has_worktree_changes(wt.path, original_head)
  
    if has_changes:
        # 有变更，保留
        return CleanupResult(
            kept=True,
            path=wt.path,
            branch=wt.branch,
        )
    else:
        # 无变更，删除
        await self.remove(name)
        return CleanupResult(kept=False)
```

**关键点**：

- SubAgent在隔离的Git工作树中工作
- 不影响主工作区的文件
- 自动检测变更并决定保留或删除
- 适合"试验性"修改

---

## 深入理解：SubAgent的完整生命周期

### 生命周期阶段图

```
┌─────────────────────────────────────────────────────────────┐
│ 阶段1: 创建 (agent_tool.py:execute)                         │
├─────────────────────────────────────────────────────────────┤
│ 1. 解析参数: subagent_type, isolation, run_in_background   │
│ 2. 加载AgentDef定义                                         │
│ 3. 创建ConversationManager（新对话或fork）                 │
│ 4. 选择LLM客户端（model参数）                               │
│ 5. 过滤工具注册表（disallowed_tools）                       │
│ 6. 创建权限检查器（permission_mode）                        │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 阶段2: 初始化 (Agent.__init__)                              │
├─────────────────────────────────────────────────────────────┤
│ 1. 分配agent_id（TraceManager.create）                     │
│ 2. 设置parent_id和trace_id                                 │
│ 3. 复制replacement_state（fork模式）                       │
│ 4. 初始化hook_engine                                        │
│ 5. 设置工作目录（worktree模式）                             │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 阶段3: 执行 (Agent.run_to_completion)                       │
├─────────────────────────────────────────────────────────────┤
│ 同步模式:                                                    │
│   await sub_agent.run_to_completion(prompt)                │
│   ↓ 主Agent等待                                            │
│   ↓ SubAgent独立运行LLM对话循环                            │
│   ↓ 返回result_text                                        │
│                                                              │
│ 异步模式:                                                    │
│   task_id = task_manager.launch(sub_agent, prompt)        │
│   ↓ 主Agent立即继续                                        │
│   ↓ asyncio.create_task() 后台执行                         │
│   ↓ 完成后notify_queue.put(task_id)                        │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 阶段4: 完成 (task_manager._run_background finally)          │
├─────────────────────────────────────────────────────────────┤
│ 1. 记录end_time                                             │
│ 2. 更新token统计（input_tokens, output_tokens）            │
│ 3. TraceManager.complete(agent_id, status)                 │
│ 4. 清理async_task                                           │
│ 5. 通知主Agent（notify_queue）                             │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 阶段5: 清理 (worktree模式: auto_cleanup)                    │
├─────────────────────────────────────────────────────────────┤
│ 1. 检测worktree变更（git status, git rev-list）            │
│ 2. 有变更 → 保留worktree和分支                             │
│ 3. 无变更 → 删除worktree（git worktree remove）            │
└─────────────────────────────────────────────────────────────┘
```

---

## 代码实战：追踪一次完整调用

### 场景：用户请求后台扫描代码

**用户输入**：

```
扫描整个代码库的SQL注入漏洞，后台执行
```

**主Agent的LLM返回**：

```json
{
  "type": "tool_use",
  "name": "Agent",
  "input": {
    "prompt": "扫描整个代码库的SQL注入漏洞",
    "description": "Security scan for SQL injection",
    "run_in_background": true
  }
}
```

**现在开始追踪代码执行**：

#### Step 1: 进入AgentTool.execute() - `agent_tool.py:93`

```python
async def execute(self, params: BaseModel) -> ToolResult:
    p: AgentToolParams = params
  
    # 检查1：是否是团队成员？
    if p.team_name:
        return await self._execute_as_teammate(p)  # 不是，跳过
  
    # 检查2：是否需要worktree隔离？
    isolation = ""
    if p.subagent_type:
        defn = self._agent_loader.get(p.subagent_type)
        if defn and defn.isolation:
            isolation = defn.isolation
  
    if isolation == "worktree":
        return await self._execute_with_worktree(p)  # 不是，跳过
  
    # 继续普通SubAgent流程...
```

#### Step 2: 加载Agent定义 - `agent_tool.py:124-133`

```python
definition: AgentDef | None = None
conversation: ConversationManager

if p.subagent_type:
    # 用户指定了agent类型（如"security-reviewer"）
    definition = self._agent_loader.get(p.subagent_type)
    if definition is None:
        return ToolResult(
            output=f"Unknown agent type: '{p.subagent_type}'...",
            is_error=True,
        )
    conversation = ConversationManager()  # 新对话
```

**假设用户没有指定subagent_type**，继续：

```python
else:
    # Fork模式：复制父Agent的对话历史
    if not self._enable_fork:
        return ToolResult(output="Fork mode is not enabled.", is_error=True)
  
    try:
        parent_conv = getattr(self._parent_agent, '_current_conversation', None)
        if parent_conv is None:
            return ToolResult(output="Cannot fork: no active conversation.", is_error=True)
    
        # 调用fork模块
        conversation = build_forked_messages(parent_conv, p.prompt)
    except ForkError as e:
        return ToolResult(output=str(e), is_error=True)
  
    # 使用默认fork定义
    definition = AgentDef(
        agent_type="fork",
        when_to_use="Forked from parent agent",
        system_prompt="",
        disallowed_tools=[],
        model="inherit",
        max_turns=self._parent_agent.max_iterations,
        permission_mode="dontAsk",
        source="builtin",
    )
```

**本例中**：假设是新SubAgent（有subagent_type="general"），所以创建空白conversation。

#### Step 3: 选择LLM客户端 - `agent_tool.py:164`

```python
client = self._select_llm(p, definition)

# _select_llm内部逻辑：
def _select_llm(self, params: AgentToolParams, definition: AgentDef) -> LLMClient:
    model_override = params.model or (
        definition.model if definition.model != "inherit" else None
    )
  
    if model_override and model_override != "inherit":
        # 用户指定了model="haiku"
        client = self._create_client_for_model(model_override)
        if client is not None:
            return client
  
    # 否则继承父Agent的client
    return self._parent_agent.client
```

#### Step 4: 判断执行模式 - `agent_tool.py:167-169`

```python
is_background = p.run_in_background or definition.background
if self._enable_fork:
    is_background = True  # fork模式强制后台执行

# 本例：p.run_in_background=True，所以is_background=True
```

#### Step 5: 过滤工具注册表 - `agent_tool.py:172-175`

```python
_base_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
filtered_registry = resolve_agent_tools(
    _base_registry, definition, is_background
)

# resolve_agent_tools内部：
# 1. 如果definition指定了tools列表 → 只包含这些工具
# 2. 如果指定了disallowed_tools → 排除这些工具
# 3. 后台模式 → 移除Agent工具（防止SubAgent再启动SubAgent）
```

#### Step 6: 创建权限检查器 - `agent_tool.py:177-189`

```python
pm_str = definition.permission_mode  # 例如："dontAsk"
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

# 本例：pm_enum = PermissionMode.DONT_ASK（SubAgent自动批准所有操作）
```

#### Step 7: 创建SubAgent实例 - `agent_tool.py:192-202`

```python
sub_agent = AgentClass(
    client=client,                    # LLM客户端
    registry=filtered_registry,       # 过滤后的工具
    protocol=self._parent_agent.protocol,
    work_dir=self._parent_agent.work_dir,  # 继承工作目录
    max_iterations=definition.max_turns,
    permission_checker=checker,
    context_window=self._parent_agent.context_window,
    instructions_content=definition.system_prompt,
    hook_engine=self._parent_agent.hook_engine,
)

# 设置父子关系
sub_agent.parent_id = self._parent_agent.agent_id
sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id
```

#### Step 8: Fork特殊处理（如果是fork模式）- `agent_tool.py:207-212`

```python
if p.subagent_type is None:  # 是fork
    from mewcode.context import clone_replacement_state
    # 继承父Agent的替换状态（用于prompt cache一致性）
    sub_agent.replacement_state = clone_replacement_state(
        self._parent_agent.replacement_state
    )
```

#### Step 9: 注册追踪节点 - `agent_tool.py:215-220`

```python
trace_node = self._trace_manager.create(
    agent_type=definition.agent_type,
    parent_id=self._parent_agent.agent_id,
    trace_id=sub_agent.trace_id,
)
sub_agent.agent_id = trace_node.agent_id

# TraceManager内部创建树形结构：
# TraceNode(
#     agent_id="abc123",
#     parent_id="parent_xyz",
#     agent_type="general",
#     status="running",
#     input_tokens=0,
#     output_tokens=0,
# )
```

#### Step 10: 启动后台任务 - `agent_tool.py:225-241`

```python
agent_name = p.name or p.subagent_type or f"agent-{trace_node.agent_id}"
is_fork = p.subagent_type is None

if is_background:
    if is_fork:
        sub_agent._fork_conversation = conversation
  
    # 关键：启动后台任务
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
        f"Do NOT wait, sleep, or poll. Report the task ID to the user and move on.",
    )

# 本例：task_id = "a1b2c3d4"，立即返回给主Agent
```

#### Step 11: TaskManager.launch() - `task_manager.py:47-69`

```python
def launch(
    self,
    agent: Agent,
    task: str,
    name: str = "",
    fork_conversation: Any = None,
) -> str:
    # 生成8位task_id
    task_id = uuid.uuid4().hex[:8]  # "a1b2c3d4"
  
    # 创建BackgroundTask对象
    bg = BackgroundTask(
        id=task_id,
        name=name or task_id,
        agent=agent,
        task=task,
        status="running",
        start_time=time.monotonic(),  # 记录开始时间
    )
    self._tasks[task_id] = bg

    # 创建异步任务
    async_task = asyncio.create_task(
        self._run_background(task_id, fork_conversation)
    )
    self._async_tasks[task_id] = async_task

    # 保存cancel函数（用于TaskStop）
    bg.cancel = async_task.cancel
  
    return task_id
```

#### Step 12: 异步执行开始 - `task_manager.py:72-130`

```python
async def _run_background(
    self, task_id: str, fork_conversation: Any = None
) -> None:
    bg = self._tasks.get(task_id)
    if bg is None:
        return

    try:
        # 执行SubAgent的主循环
        if fork_conversation is not None:
            result = await bg.agent.run_to_completion("", fork_conversation)
        else:
            result = await bg.agent.run_to_completion(bg.task)
            # ↑ 本例：bg.task = "扫描整个代码库的SQL注入漏洞"
    
        bg.result = result
        bg.status = "completed"

        # 团队成员特殊处理（本例不涉及）
        if bg.agent.team_name and bg.agent._team_manager:
            # ... 消息循环逻辑 ...
            pass

    except asyncio.CancelledError:
        bg.status = "cancelled"
        bg.result = "Task was cancelled"
    except Exception as e:
        log.error("Background task %s failed: %s", task_id, e)
        bg.status = "failed"
        bg.result = f"Error: {e}"
    finally:
        # 记录完成状态
        bg.end_time = time.monotonic()
        bg.progress.input_tokens = bg.agent.total_input_tokens
        bg.progress.output_tokens = bg.agent.total_output_tokens
        self._async_tasks.pop(task_id, None)
    
        # 通知主Agent
        await self._notify_queue.put(task_id)
```

#### Step 13: SubAgent执行主循环 - `agent.py:run_to_completion()`

```python
async def run_to_completion(
    self, initial_prompt: str, conversation: ConversationManager | None = None
) -> str:
    """
    SubAgent的主执行循环
    """
    if conversation is None:
        conversation = ConversationManager()
  
    # 添加用户消息
    if initial_prompt:
        conversation.add_message({
            "role": "user",
            "content": initial_prompt
        })
  
    # 主循环
    for turn in range(self.max_iterations):
        # 1. 调用LLM
        response = await self.client.send_message(
            messages=conversation.messages,
            tools=self.registry.get_all_schemas(),
            system=self._build_system_prompt(),
        )
    
        # 2. 处理LLM响应
        conversation.add_message(response)
    
        # 3. 执行工具调用
        if response.get("stop_reason") == "tool_use":
            tool_results = []
            for tool_call in response.get("content", []):
                if tool_call["type"] == "tool_use":
                    result = await self._execute_tool(tool_call)
                    tool_results.append(result)
        
            # 添加工具结果
            conversation.add_message({
                "role": "user",
                "content": tool_results
            })
    
        # 4. 检查是否完成
        if response.get("stop_reason") == "end_turn":
            # 提取文本响应
            text_blocks = [
                block["text"] for block in response.get("content", [])
                if block["type"] == "text"
            ]
            return "\n".join(text_blocks)
  
    return "(max iterations reached)"
```

#### Step 14: 主Agent收到通知

```python
# 主Agent的主循环中：
completed_tasks = self._task_manager.poll_completed()

for task in completed_tasks:
    # 构建通知消息
    notification = (
        f"[Task completed: {task.name}]\n"
        f"Status: {task.status}\n"
        f"Result:\n{task.result}"
    )
  
    # 添加到对话
    conversation.add_message({
        "role": "user",
        "content": notification
    })
  
    # 下一轮LLM调用时，主Agent会看到这个通知
```

---

## 关键技术点详解

### 1. Fork如何继承上下文？

**问题**：SubAgent需要知道父Agent的对话历史

**解决方案**：`agents/fork.py:build_forked_messages()`

```python
def build_forked_messages(
    parent_conv: ConversationManager,
    fork_prompt: str,
) -> ConversationManager:
    """
    复制父Agent的消息列表，并添加fork指令
    """
    # 1. 防止fork的fork
    for msg in parent_conv.messages:
        if msg.get("role") == "user" and FORK_BOILERPLATE in msg.get("content", ""):
            raise ForkError("Cannot fork from a fork agent (nesting not allowed)")
  
    # 2. 浅拷贝所有消息
    forked_conv = ConversationManager()
    for msg in parent_conv.messages:
        forked_conv.add_message(msg)
  
    # 3. 添加fork专用指令
    forked_conv.add_message({
        "role": "user",
        "content": FORK_BOILERPLATE + "\n\n" + fork_prompt
    })
  
    return forked_conv
```

**FORK_BOILERPLATE的作用**：

```python
FORK_BOILERPLATE = """
You are now running as a forked sub-agent.
You inherit the full conversation context from the parent.

CRITICAL: Do NOT spawn further sub-agents or forks.
Your job is to execute the task directly, not delegate it.
"""
```

**为什么需要反嵌套保护？**

```
主Agent
  ↓ fork
SubAgent1（继承了主Agent的对话）
  ↓ fork again?
SubAgent2（继承了SubAgent1的对话，包括主Agent的对话）
  ↓ fork again??
SubAgent3...
  ↓ 无限递归，上下文爆炸
```

**检测逻辑**：

```python
def _is_already_fork(conv: ConversationManager) -> bool:
    """检查对话中是否已经包含FORK_BOILERPLATE"""
    for msg in conv.messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if FORK_BOILERPLATE in content:
                return True
    return False
```

---

### 2. Prompt Cache如何共享？

**问题**：Fork模式下，父子Agent的对话历史相同，如何避免重复计算？

**解决方案**：共享replacement_state

```python
# agent_tool.py:207-212
if p.subagent_type is None:  # fork模式
    from mewcode.context import clone_replacement_state
    sub_agent.replacement_state = clone_replacement_state(
        self._parent_agent.replacement_state
    )
```

**replacement_state是什么？**

```python
# context.py
@dataclass
class ReplacementState:
    """
    记录每个tool_use_id的替换决策
  
    Claude的prompt cache依赖完全相同的消息序列。
    如果父Agent把某个tool_use_id的内容替换成了"[omitted]"，
    子Agent必须做同样的替换，否则cache失效。
    """
    decisions: dict[str, bool]  # tool_use_id -> 是否替换

def clone_replacement_state(state: ReplacementState) -> ReplacementState:
    """深拷贝替换状态"""
    return ReplacementState(decisions=state.decisions.copy())
```

**为什么重要？**

```
父Agent的消息序列：
[
  {"role": "user", "content": "帮我分析这个文件"},
  {"role": "assistant", "content": [..., tool_use_id="abc123"]},
  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "abc123", "content": "[omitted 50KB]"}]},
]

SubAgent必须做同样的替换：
[
  ...（继承父Agent的消息）
  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "abc123", "content": "[omitted 50KB]"}]},
]

如果SubAgent写成了完整内容（50KB），cache key不匹配，无法复用父Agent的cache。
```

---

### 3. 工具过滤如何工作？

**问题**：不同类型的SubAgent需要不同的工具集

**解决方案**：`agents/tool_filter.py:resolve_agent_tools()`

```python
def resolve_agent_tools(
    base_registry: ToolRegistry,
    definition: AgentDef,
    is_background: bool,
) -> ToolRegistry:
    """
    根据AgentDef定义过滤工具
  
    规则：
    1. definition.tools指定 → 只包含这些工具（白名单）
    2. definition.disallowed_tools指定 → 排除这些工具（黑名单）
    3. is_background=True → 移除Agent工具（防止递归）
    """
    # 获取所有工具
    all_tools = base_registry.list_tools()
  
    # 规则1：白名单
    if definition.tools:
        allowed = set(definition.tools)
        all_tools = [t for t in all_tools if t.name in allowed]
  
    # 规则2：黑名单
    if definition.disallowed_tools:
        disallowed = set(definition.disallowed_tools)
        all_tools = [t for t in all_tools if t.name not in disallowed]
  
    # 规则3：后台模式禁用Agent工具
    if is_background:
        all_tools = [t for t in all_tools if t.name != "Agent"]
  
    # 创建新的注册表
    filtered_registry = ToolRegistry()
    for tool in all_tools:
        filtered_registry.register(tool)
  
    return filtered_registry
```

**实际例子**：

```yaml
# agents/security-reviewer.yaml
agent_type: security-reviewer
when_to_use: Review code for security vulnerabilities
tools:
  - Read
  - Grep
  - Bash
disallowed_tools:
  - Write
  - Edit
  - Agent
```

结果：security-reviewer只能读取和搜索代码，不能修改文件或启动SubAgent。

---

### 4. 团队成员的消息循环

**问题**：团队成员完成初始任务后，如何等待新消息？

**解决方案**：`task_manager.py:87-115`

```python
# SubAgent完成初始任务
result = await bg.agent.run_to_completion(bg.task)
bg.result = result
bg.status = "completed"

# 如果是团队成员，进入消息循环
if bg.agent.team_name and bg.agent._team_manager:
    mailbox = bg.agent._team_manager.get_mailbox(bg.agent.team_name)
    if mailbox:
        # 通知团队lead：我空闲了
        msg = create_message(
            from_agent=bg.name,
            to_agent="lead",
            content=f"[idle] {bg.name}: completed initial task",
            summary=f"{bg.name} idle",
        )
        mailbox.write("lead", msg)

        # 等待新消息（最多60秒）
        for _ in range(60):
            await asyncio.sleep(1)
        
            # 检查邮箱
            msgs = mailbox.consume(bg.agent.agent_id)
            if not msgs:
                continue
        
            # 收到消息，继续处理
            prompt = "\n\n".join(
                f"[Message from {m.from_agent}] {m.content}" for m in msgs
            )
            result = await bg.agent.run_to_completion(prompt)
            bg.result = result
        
            # 再次通知空闲
            msg = create_message(
                from_agent=bg.name,
                to_agent="lead",
                content=f"[idle] {bg.name}: completed follow-up",
                summary=f"{bg.name} idle",
            )
            mailbox.write("lead", msg)
```

**消息循环示意图**：

```
[T0] 团队成员完成初始任务
  ↓
[T1] 写入mailbox: "[idle] worker-1: completed"
  ↓
[T2] 进入循环，每秒检查mailbox
  ↓
[T3] Lead发送消息: "帮我测试auth模块"
  ↓
[T4] worker-1读取消息
  ↓
[T5] 执行新任务
  ↓
[T6] 完成后再次通知idle
  ↓
[T7] 继续循环...
```

---

## 性能优化技巧

### 1. Fork模式的Prompt Cache复用

**问题**：Fork模式下，父子Agent的对话历史几乎相同，如何避免重复计算？

**Claude的Prompt Cache机制**：

- 前N个token相同 → cache命中
- cache有效期：5分钟

**优化策略**：

```python
# 父Agent的对话
messages = [
    {"role": "user", "content": "帮我分析项目结构"},
    {"role": "assistant", "content": "...（5000 tokens）"},
    {"role": "user", "content": "分析utils.py"},
    {"role": "assistant", "content": "...（3000 tokens）"},
]

# Fork SubAgent时
forked_messages = messages.copy()  # 继承8000 tokens
forked_messages.append({
    "role": "user",
    "content": FORK_BOILERPLATE + "修复utils.py的Bug"
})

# 第一次LLM调用：前8000 tokens cache命中，只计算最后200 tokens
# 节省：8000 tokens的计算时间
```

**关键代码**：`agent_tool.py:207-212`

```python
if p.subagent_type is None:  # fork模式
    # 继承replacement_state，确保cache key一致
    sub_agent.replacement_state = clone_replacement_state(
        self._parent_agent.replacement_state
    )
```

---

### 2. 工具结果的智能压缩

**问题**：大文件读取的结果会占用大量上下文

**解决方案**：replacement_state跟踪哪些tool_result可以压缩

```python
# Read工具返回了50KB内容
tool_result = {
    "type": "tool_result",
    "tool_use_id": "abc123",
    "content": "[50KB file content...]"
}

# 后续对话中，如果这个结果不再需要
replacement_state.decisions["abc123"] = True

# 下次构建messages时
messages.append({
    "type": "tool_result",
    "tool_use_id": "abc123",
    "content": "[omitted 50KB]"  # 压缩
})

# 节省：50KB上下文空间
```

---

### 3. 后台任务的异步通知

**问题**：主Agent如何知道后台任务完成了？

**传统方案（差）**：

```python
# 主Agent轮询
while True:
    await asyncio.sleep(1)
    task = task_manager.get(task_id)
    if task.status == "completed":
        break
# 浪费CPU，延迟高
```

**MewCode方案（好）**：

```python
# TaskManager使用asyncio.Queue通知
async def _run_background(self, task_id: str, ...) -> None:
    try:
        result = await bg.agent.run_to_completion(bg.task)
        bg.status = "completed"
    finally:
        # 完成时推送通知
        await self._notify_queue.put(task_id)

# 主Agent在每轮开始时检查
completed_tasks = self._task_manager.poll_completed()
for task in completed_tasks:
    # 立即处理完成的任务
    ...
```

**优势**：

- 零轮询，零延迟
- 异步通知，不阻塞主Agent
- 批量处理多个完成的任务

---

## 常见陷阱与解决方案

### 陷阱1：Fork的Fork导致上下文爆炸

**错误代码**：

```python
# 主Agent
Agent(prompt="分析代码结构")  # fork
  ↓
# SubAgent1继承主Agent的10000 tokens
Agent(prompt="深入分析auth模块")  # fork again
  ↓
# SubAgent2继承SubAgent1的10000 + 5000 = 15000 tokens
Agent(prompt="进一步分析...")  # fork again
  ↓
# SubAgent3继承20000 tokens...
```

**问题**：

- 上下文指数增长
- prompt cache失效（每层都不同）
- 性能急剧下降

**解决方案**：`agents/fork.py:_is_already_fork()`

```python
def build_forked_messages(
    parent_conv: ConversationManager,
    fork_prompt: str,
) -> ConversationManager:
    # 检测是否已经是fork
    if _is_already_fork(parent_conv):
        raise ForkError(
            "Cannot fork from a fork agent (nesting not allowed). "
            "This agent was itself forked from a parent. "
            "If you need to delegate work, use a non-fork subagent_type instead."
        )
    ...

def _is_already_fork(conv: ConversationManager) -> bool:
    for msg in conv.messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if FORK_BOILERPLATE in content:
                return True
    return False
```

---

### 陷阱2：后台任务启动SubAgent

**错误场景**：

```python
# 用户：后台扫描代码
Agent(prompt="扫描代码", run_in_background=True)
  ↓
# SubAgent1后台运行
Agent(prompt="分析每个文件")  # SubAgent1又启动SubAgent2
  ↓
# SubAgent2后台运行
Agent(prompt="...")  # SubAgent2又启动SubAgent3
  ↓
# 无限递归
```

**问题**：

- 后台任务失控
- 资源耗尽
- 难以追踪

**解决方案**：`agents/tool_filter.py`

```python
def resolve_agent_tools(..., is_background: bool) -> ToolRegistry:
    ...
  
    # 后台模式禁用Agent工具
    if is_background:
        all_tools = [t for t in all_tools if t.name != "Agent"]
  
    return filtered_registry
```

**结果**：后台SubAgent看不到Agent工具，无法再启动SubAgent。

---

### 陷阱3：Worktree路径混淆

**错误代码**：

```python
# 用户：在worktree中修改文件
Agent(prompt="修改/home/user/project/src/main.py", isolation="worktree")
  ↓
# SubAgent在worktree中：/home/user/project/.claude/worktrees/abc123
# 但它尝试访问：/home/user/project/src/main.py（主工作区）
  ↓
# 修改了主工作区的文件！
```

**问题**：

- SubAgent应该在worktree中工作，但访问了主工作区
- 破坏了隔离性

**解决方案**：PathSandbox限制

```python
# agent_tool.py:583-588
checker = PermissionChecker(
    detector=DangerousCommandDetector(),
    sandbox=PathSandbox(wt.path),  # 沙箱限制在worktree路径
    rule_engine=RuleEngine(),
    mode=pm_enum,
)

# PathSandbox会检查所有文件操作
class PathSandbox:
    def __init__(self, allowed_root: str):
        self.allowed_root = Path(allowed_root).resolve()
  
    def is_allowed(self, path: str) -> bool:
        target = Path(path).resolve()
        return target.is_relative_to(self.allowed_root)
```

**结果**：SubAgent尝试访问主工作区的文件时被拒绝。

---

## 最佳实践

### 1. 何时使用同步vs异步？

**同步（默认）**：

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

| 对比项    | 新SubAgent   | Fork SubAgent  |
| --------- | ------------ | -------------- |
| 上下文    | 空白         | 继承完整历史   |
| 理解力    | 需要重新解释 | 已知所有背景   |
| Token消耗 | 低           | 高（包含历史） |
| 适用场景  | 独立任务     | 延续性任务     |

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

| 模式         | 特点       | 使用场景         |
| ------------ | ---------- | ---------------- |
| 前台同步     | 阻塞等待   | 需要结果才能继续 |
| 后台异步     | 立即返回   | 耗时长、可并行   |
| Fork继承     | 继承上下文 | 延续性任务       |
| Worktree隔离 | 文件隔离   | 实验、回滚       |

下一章我们将学习：如何通过Team系统实现长期运行的Agent团队协作？

---

## 附录：SubAgent核心代码执行流程详解

### 一、从一次真实调用说起

**用户输入**：

```
扫描整个代码库的SQL注入漏洞，后台执行
```

**主Agent的LLM返回**：

```json
{
  "type": "tool_use",
  "name": "Agent",
  "input": {
    "prompt": "扫描整个代码库的SQL注入漏洞",
    "description": "Security scan",
    "run_in_background": true
  }
}
```

**现在追踪完整的15步执行流程**。

---

### 二、完整执行流程（15步）

#### Step 1: 进入AgentTool.execute()

**文件**: `mewcode/tools/agent_tool.py:93`

```python
async def execute(self, params: BaseModel) -> ToolResult:
    p: AgentToolParams = params
  
    # 检查1：是否是团队成员？
    if p.team_name:
        return await self._execute_as_teammate(p)
  
    # 检查2：是否需要worktree隔离？
    isolation = ""
    if p.subagent_type:
        defn = self._agent_loader.get(p.subagent_type)
        if defn and defn.isolation:
            isolation = defn.isolation
  
    if isolation == "worktree":
        return await self._execute_with_worktree(p)
  
    # 继续普通SubAgent流程...
```

**本例**：没有team_name，没有isolation，继续执行。

---

#### Step 2: 加载Agent定义

**文件**: `agent_tool.py:124-161`

```python
definition: AgentDef | None = None
conversation: ConversationManager

if p.subagent_type:
    # 用户指定了agent类型
    definition = self._agent_loader.get(p.subagent_type)
    conversation = ConversationManager()  # 新对话
else:
    # Fork模式：复制父Agent的对话历史
    if not self._enable_fork:
        return ToolResult(output="Fork mode is not enabled.", is_error=True)
  
    parent_conv = getattr(self._parent_agent, '_current_conversation', None)
    if parent_conv is None:
        return ToolResult(output="Cannot fork: no active conversation.", is_error=True)
  
    # 关键：调用fork模块复制对话
    conversation = build_forked_messages(parent_conv, p.prompt)
  
    # 使用默认fork定义
    definition = AgentDef(
        agent_type="fork",
        system_prompt="",
        model="inherit",
        max_turns=self._parent_agent.max_iterations,
        permission_mode="dontAsk",
    )
```

**本例**：假设是fork模式，继承父Agent的对话。

---

#### Step 3: 选择LLM客户端

**文件**: `agent_tool.py:164`

```python
client = self._select_llm(p, definition)

# _select_llm内部逻辑
def _select_llm(self, params, definition) -> LLMClient:
    model_override = params.model or (
        definition.model if definition.model != "inherit" else None
    )
  
    if model_override and model_override != "inherit":
        # 创建新client
        client = self._create_client_for_model(model_override)
        if client is not None:
            return client
  
    # 继承父Agent的client
    return self._parent_agent.client
```

**本例**：没有指定model，使用父Agent的client。

---

#### Step 4: 判断执行模式

**文件**: `agent_tool.py:167-169`

```python
is_background = p.run_in_background or definition.background
if self._enable_fork:
    is_background = True  # fork模式强制后台

# 本例：p.run_in_background=True，所以is_background=True
```

**关键决策点**：

- `run_in_background=True` → 异步执行
- `definition.background=True` → 默认后台
- fork模式 → 强制后台

---

#### Step 5: 过滤工具注册表

**文件**: `agent_tool.py:172-175`

```python
_base_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
filtered_registry = resolve_agent_tools(
    _base_registry, definition, is_background
)
```

**resolve_agent_tools内部**：

```python
def resolve_agent_tools(base_registry, definition, is_background):
    all_tools = base_registry.list_tools()
  
    # 规则1：白名单
    if definition.tools:
        allowed = set(definition.tools)
        all_tools = [t for t in all_tools if t.name in allowed]
  
    # 规则2：黑名单
    if definition.disallowed_tools:
        disallowed = set(definition.disallowed_tools)
        all_tools = [t for t in all_tools if t.name not in disallowed]
  
    # 规则3：后台模式禁用Agent工具（防止递归）
    if is_background:
        all_tools = [t for t in all_tools if t.name != "Agent"]
  
    return filtered_registry
```

**本例**：因为is_background=True，Agent工具被移除。

---

#### Step 6: 创建权限检查器

**文件**: `agent_tool.py:177-189`

```python
pm_str = definition.permission_mode  # "dontAsk"
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
```

**本例**：fork的permission_mode="dontAsk"，SubAgent自动批准所有操作。

---

#### Step 7: 创建SubAgent实例

**文件**: `agent_tool.py:192-202`

```python
sub_agent = AgentClass(
    client=client,
    registry=filtered_registry,       # 没有Agent工具
    protocol=self._parent_agent.protocol,
    work_dir=self._parent_agent.work_dir,
    max_iterations=definition.max_turns,
    permission_checker=checker,       # DONT_ASK模式
    context_window=self._parent_agent.context_window,
    instructions_content=definition.system_prompt,
    hook_engine=self._parent_agent.hook_engine,
)

# 设置父子关系
sub_agent.parent_id = self._parent_agent.agent_id
sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id
```

---

#### Step 8: Fork特殊处理

**文件**: `agent_tool.py:207-212`

```python
if p.subagent_type is None:  # fork模式
    from mewcode.context import clone_replacement_state
    # 继承父Agent的替换状态（prompt cache一致性）
    sub_agent.replacement_state = clone_replacement_state(
        self._parent_agent.replacement_state
    )
```

**replacement_state的作用**：

如果父Agent把某个tool_result压缩成"[omitted]"，子Agent必须做同样的压缩，否则prompt cache失效。

---

#### Step 9: 注册追踪节点

**文件**: `agent_tool.py:215-220`

```python
trace_node = self._trace_manager.create(
    agent_type=definition.agent_type,
    parent_id=self._parent_agent.agent_id,
    trace_id=sub_agent.trace_id,
)
sub_agent.agent_id = trace_node.agent_id
```

**TraceManager内部**：创建树形结构

```
主Agent(agent_id="main123")
  └─ SubAgent(agent_id="sub456", parent_id="main123")
```

---

#### Step 10: 启动后台任务

**文件**: `agent_tool.py:225-241`

```python
agent_name = p.name or p.subagent_type or f"agent-{trace_node.agent_id}"
is_fork = p.subagent_type is None

if is_background:
    if is_fork:
        sub_agent._fork_conversation = conversation
  
    # 关键：启动后台任务
    task_id = self._task_manager.launch(
        agent=sub_agent,
        task="" if is_fork else p.prompt,
        name=agent_name,
        fork_conversation=conversation if is_fork else None,
    )
  
    return ToolResult(
        output=f"Sub-agent launched in background.\n"
        f"Task ID: {task_id}\n"
        f"The system will notify automatically when it completes.",
    )
```

**本例**：返回task_id="a1b2c3d4"，主Agent立即继续。

---

#### Step 11: TaskManager.launch()

**文件**: `agents/task_manager.py:47-69`

```python
def launch(self, agent, task, name="", fork_conversation=None) -> str:
    # 生成8位task_id
    task_id = uuid.uuid4().hex[:8]
  
    # 创建BackgroundTask对象
    bg = BackgroundTask(
        id=task_id,
        name=name or task_id,
        agent=agent,
        task=task,
        status="running",
        start_time=time.monotonic(),
    )
    self._tasks[task_id] = bg

    # 创建异步任务（关键！）
    async_task = asyncio.create_task(
        self._run_background(task_id, fork_conversation)
    )
    self._async_tasks[task_id] = async_task

    bg.cancel = async_task.cancel
    return task_id
```

**关键**：`asyncio.create_task()` 立即返回，SubAgent在后台异步执行。

---

#### Step 12: 异步执行 - _run_background()

**文件**: `task_manager.py:72-130`

```python
async def _run_background(self, task_id, fork_conversation=None):
    bg = self._tasks.get(task_id)
  
    try:
        # 执行SubAgent的主循环
        if fork_conversation is not None:
            result = await bg.agent.run_to_completion("", fork_conversation)
        else:
            result = await bg.agent.run_to_completion(bg.task)
      
        bg.result = result
        bg.status = "completed"
      
        # 团队成员会进入消息循环（本例不涉及）
      
    except asyncio.CancelledError:
        bg.status = "cancelled"
    except Exception as e:
        bg.status = "failed"
        bg.result = f"Error: {e}"
    finally:
        # 记录完成状态
        bg.end_time = time.monotonic()
        bg.progress.input_tokens = bg.agent.total_input_tokens
        bg.progress.output_tokens = bg.agent.total_output_tokens
        self._async_tasks.pop(task_id, None)
      
        # 通知主Agent（关键！）
        await self._notify_queue.put(task_id)
```

---

#### Step 13: SubAgent执行主循环

**文件**: `mewcode/agent.py:run_to_completion()`

```python
async def run_to_completion(self, initial_prompt, conversation=None):
    if conversation is None:
        conversation = ConversationManager()
  
    if initial_prompt:
        conversation.add_message({
            "role": "user",
            "content": initial_prompt
        })
  
    # 主循环
    for turn in range(self.max_iterations):
        # 1. 调用LLM
        response = await self.client.send_message(
            messages=conversation.messages,
            tools=self.registry.get_all_schemas(),
            system=self._build_system_prompt(),
        )
      
        # 2. 处理LLM响应
        conversation.add_message(response)
      
        # 3. 执行工具调用
        if response.get("stop_reason") == "tool_use":
            tool_results = []
            for tool_call in response.get("content", []):
                if tool_call["type"] == "tool_use":
                    result = await self._execute_tool(tool_call)
                    tool_results.append(result)
          
            conversation.add_message({
                "role": "user",
                "content": tool_results
            })
            continue
      
        # 4. 检查是否完成
        if response.get("stop_reason") == "end_turn":
            text_blocks = [
                block["text"] for block in response.get("content", [])
                if block["type"] == "text"
            ]
            return "\n".join(text_blocks)
  
    return "(max iterations reached)"
```

**本例**：SubAgent循环调用LLM，执行Grep/Read/Bash等工具，最终返回扫描结果。

---

#### Step 14: 完成并通知

**回到**: `task_manager.py:_run_background()` 的 `finally` 块

```python
finally:
    bg.end_time = time.monotonic()
    bg.progress.input_tokens = bg.agent.total_input_tokens
    bg.progress.output_tokens = bg.agent.total_output_tokens
    self._async_tasks.pop(task_id, None)
  
    # 通知主Agent（推送到队列）
    await self._notify_queue.put(task_id)
```

**notify_queue**：异步队列，SubAgent完成时推送task_id。

---

#### Step 15: 主Agent收到通知

**文件**: `mewcode/agent.py` 主循环

```python
# 主Agent的每一轮开始时
completed_tasks = self._task_manager.poll_completed()

for task in completed_tasks:
    notification = (
        f"[Task completed: {task.name}]\n"
        f"Status: {task.status}\n"
        f"Result:\n{task.result}"
    )
  
    # 添加到对话（下一轮LLM会看到）
    conversation.add_message({
        "role": "user",
        "content": notification
    })
```

**poll_completed()实现**：

```python
def poll_completed(self):
    completed = []
    while not self._notify_queue.empty():
        task_id = self._notify_queue.get_nowait()
        bg = self._tasks.get(task_id)
        if bg is not None:
            completed.append(bg)
    return completed
```

**本例**：主Agent收到通知，提取结果：

```
[Task completed: agent-sub456]
Status: completed
Result:
发现3个SQL注入漏洞：
1. api/users.py:45 - 直接拼接SQL
2. api/posts.py:78 - 未参数化查询
3. utils/db.py:123 - format字符串注入
```

---

### 三、关键技术点深度解析

#### 1. Fork如何继承上下文？

**文件**: `agents/fork.py:build_forked_messages()`

```python
def build_forked_messages(parent_conv, fork_prompt):
    # 1. 防止fork的fork（反嵌套检查）
    for msg in parent_conv.messages:
        if msg.get("role") == "user" and FORK_BOILERPLATE in msg.get("content", ""):
            raise ForkError("Cannot fork from a fork agent")
  
    # 2. 浅拷贝所有消息
    forked_conv = ConversationManager()
    for msg in parent_conv.messages:
        forked_conv.add_message(msg)
  
    # 3. 添加fork专用指令
    forked_conv.add_message({
        "role": "user",
        "content": FORK_BOILERPLATE + "\n\n" + fork_prompt
    })
  
    return forked_conv
```

**FORK_BOILERPLATE**：

```python
FORK_BOILERPLATE = """
You are now running as a forked sub-agent.
You inherit the full conversation context from the parent.

CRITICAL: Do NOT spawn further sub-agents or forks.
Your job is to execute the task directly, not delegate it.
"""
```

**为什么需要反嵌套保护？**

```
主Agent (10000 tokens)
  ↓ fork
SubAgent1 (10000 + 5000 = 15000 tokens)
  ↓ fork again?
SubAgent2 (15000 + 3000 = 18000 tokens)
  ↓
上下文指数增长，cache失效
```

---

#### 2. Prompt Cache如何共享？

**优化策略**：

```python
# 父Agent的对话（8000 tokens）
messages = [
    {"role": "user", "content": "帮我分析项目结构"},
    {"role": "assistant", "content": "...（5000 tokens）"},
    {"role": "user", "content": "分析utils.py"},
    {"role": "assistant", "content": "...（3000 tokens）"},
]

# Fork SubAgent时
forked_messages = messages.copy()  # 继承8000 tokens
forked_messages.append({
    "role": "user",
    "content": FORK_BOILERPLATE + "修复Bug"  # +200 tokens
})

# 第一次LLM调用：
# 前8000 tokens cache命中（不计算）
# 只计算200 tokens
# 节省：8000 tokens的计算时间和费用
```

**关键**：replacement_state确保cache key一致

```python
# agent_tool.py:207-212
if p.subagent_type is None:  # fork模式
    sub_agent.replacement_state = clone_replacement_state(
        self._parent_agent.replacement_state
    )
```

如果父Agent压缩了某个tool_result，子Agent必须做同样的压缩，否则cache失效。

---

#### 3. 工具过滤的三层规则

**文件**: `agents/tool_filter.py`

```python
def resolve_agent_tools(base_registry, definition, is_background):
    all_tools = base_registry.list_tools()
  
    # 规则1：白名单
    if definition.tools:
        allowed = set(definition.tools)
        all_tools = [t for t in all_tools if t.name in allowed]
  
    # 规则2：黑名单
    if definition.disallowed_tools:
        disallowed = set(definition.disallowed_tools)
        all_tools = [t for t in all_tools if t.name not in disallowed]
  
    # 规则3：后台模式禁用Agent工具
    if is_background:
        all_tools = [t for t in all_tools if t.name != "Agent"]
  
    return filtered_registry
```

**实际例子**：

```yaml
# agents/security-scanner.yaml
tools:
  - Read
  - Grep
  - Bash
disallowed_tools:
  - Write
  - Edit
background: true  # 后台模式再次禁用Agent
```

结果：只有Read, Grep, Bash三个工具。

---

#### 4. 异步通知的零延迟设计

**传统方案（差）**：

```python
# 轮询
while True:
    await asyncio.sleep(1)
    if task.status == "completed":
        break
# 问题：浪费CPU，延迟高
```

**MewCode方案（好）**：

```python
# 生产者（SubAgent完成时）
await self._notify_queue.put(task_id)

# 消费者（主Agent检查时）
completed_tasks = []
while not self._notify_queue.empty():
    task_id = self._notify_queue.get_nowait()
    completed_tasks.append(task_id)
```

**优势**：

- 零轮询：不浪费CPU
- 零延迟：任务完成立即通知
- 批量处理：一次获取所有完成任务

---

### 四、常见陷阱与解决方案

#### 陷阱1：Fork的Fork导致上下文爆炸

**问题**：

```
主Agent (10000 tokens)
  ↓ fork
SubAgent1 (15000 tokens)
  ↓ fork again
SubAgent2 (18000 tokens)
  ↓
上下文指数增长
```

**解决方案**：反嵌套检查

```python
if FORK_BOILERPLATE in content:
    raise ForkError("Cannot fork from a fork agent")
```

---

#### 陷阱2：后台任务启动SubAgent

**问题**：后台SubAgent又启动SubAgent，无限递归

**解决方案**：后台模式禁用Agent工具

```python
if is_background:
    all_tools = [t for t in all_tools if t.name != "Agent"]
```

---

#### 陷阱3：Worktree路径混淆

**问题**：SubAgent在worktree中访问了主工作区文件

**解决方案**：PathSandbox限制

```python
checker = PermissionChecker(
    sandbox=PathSandbox(wt.path),  # 限制在worktree内
)
```

---

### 五、性能优化总结

#### 1. 并行执行 → 10倍速度

```
串行：15分钟
并行：max(5分钟) = 5分钟
```

#### 2. Prompt Cache复用 → 节省8000+ tokens

```
父Agent：8000 tokens
Fork SubAgent：继承8000 + 新任务200
Cache命中：只计算200 tokens
```

#### 3. 异步通知 → 零延迟

```
asyncio.Queue推送
poll_completed()批量获取
```

---

### 六、完整流程图

```
[用户输入] "扫描SQL注入漏洞，后台执行"
    ↓
[主Agent LLM] 返回 Agent(prompt="扫描...", run_in_background=True)
    ↓
[Step 1-2] AgentTool.execute() → 加载AgentDef
    ↓
[Step 3-6] 选择LLM → 过滤工具 → 创建权限检查器
    ↓
[Step 7-9] 创建SubAgent → Fork处理 → 注册追踪节点
    ↓
[Step 10-11] TaskManager.launch() → asyncio.create_task()
    ↓ 主Agent立即返回，继续执行
    ↓
[Step 12-13] SubAgent后台执行 → run_to_completion()
    ↓ LLM循环 → 工具调用 → 返回结果
    ↓
[Step 14] _run_background() finally → notify_queue.put(task_id)
    ↓
[Step 15] 主Agent poll_completed() → 收到通知 → 提取结果
    ↓
[完成] 主Agent将结果展示给用户
```

---

*本附录详细追踪了SubAgent从调用到完成的完整15步执行流程，揭示了MewCode SubAgent系统的核心实现机制。*
