# CH03: Agent主循环与事件流

## 从一次完整对话说起

想象你和 AI 的一次完整交互：

```
你: "帮我读取 config.yaml 并修改端口为 8080"

AI: 好的，让我先读取这个文件
[调用工具: ReadFile]
[结果显示...]

AI: 我看到当前端口是 3000，现在修改为 8080
[调用工具: WriteFile]
[结果: 成功写入]

AI: 已完成修改，端口已改为 8080
```

这个看似简单的对话，背后经历了什么？

1. **第 1 轮**：AI 决定读取文件 → 调用 ReadFile → 等待结果
2. **第 2 轮**：AI 看到文件内容 → 决定写入 → 调用 WriteFile → 等待结果
3. **第 3 轮**：AI 看到写入成功 → 不再调用工具 → 对话结束

这就是 **Agent 主循环**的工作方式。

---

## 第一步：主循环的骨架

### 核心问题：如何让 AI 持续工作？

```python
async def run(self, conversation: ConversationManager):
    """Agent 主循环"""
    iteration = 0
  
    while True:  # 无限循环
        iteration += 1
    
        # 步骤 1: 准备本轮对话
        # 步骤 2: 调用 LLM，获取回复
        # 步骤 3: 如果没有工具调用 → 结束
        # 步骤 4: 执行工具
        # 步骤 5: 将工具结果加入对话 → 进入下一轮
```

### 为什么需要循环？

**场景 1：简单问题（1 轮）**

```
用户: "今天星期几？"
AI: "今天是星期三"  ← 不调用工具，直接结束
```

**场景 2：需要工具（2 轮）**

```
轮次 1:
  用户: "config.yaml 的内容是什么？"
  AI: [调用 ReadFile] ← 有工具调用，继续循环

轮次 2:
  系统: [ReadFile 结果]
  AI: "文件内容如下：..." ← 无工具调用，结束
```

**场景 3：多步任务（3+ 轮）**

```
轮次 1:
  用户: "找出所有 TODO 注释并生成报告"
  AI: [调用 Grep 搜索 TODO] ← 继续

轮次 2:
  系统: [Grep 结果]
  AI: [调用 WriteFile 生成报告] ← 继续

轮次 3:
  系统: [WriteFile 成功]
  AI: "已生成报告文件" ← 结束
```

---

## 第二步：事件流 - 主循环的"神经系统"

### 为什么需要事件？

主循环运行时，外部需要知道发生了什么：

- UI 需要实时显示 AI 的回复文本
- 用户需要看到工具调用的进度
- 系统需要记录 token 用量
- 错误发生时需要通知

**解决方案：事件流（Event Stream）**

```python
async def run(self, conversation) -> AsyncIterator[AgentEvent]:
    """主循环通过 yield 发出事件"""
  
    # 发出文本事件
    yield StreamText(text="好的，让我读取文件")
  
    # 发出工具调用事件
    yield ToolUseEvent(
        tool_name="ReadFile",
        tool_id="call_123",
        arguments={"file_path": "config.yaml"}
    )
  
    # 发出工具结果事件
    yield ToolResultEvent(
        tool_name="ReadFile",
        output="port: 3000\nhost: localhost",
        is_error=False,
        elapsed=0.05
    )
```

### 11 种事件类型

```python
AgentEvent = (
    StreamText           # AI 输出的文本片段
    | ThinkingText       # AI 的思考过程
    | ToolUseEvent       # 开始调用工具
    | ToolResultEvent    # 工具执行完成
    | TurnComplete       # 一轮对话完成
    | LoopComplete       # 整个对话完成
    | UsageEvent         # Token 用量统计
    | ErrorEvent         # 错误发生
    | RetryEvent         # 需要重试
    | PermissionRequest  # 请求用户授权
    | CompactNotification # 上下文压缩通知
    | HookEvent          # Hook 执行事件
)
```

### 事件的生命周期

```
[轮次 1 开始]
    ↓
StreamText("好的")
StreamText("，")
StreamText("让我")
StreamText("读取")
StreamText("文件")
    ↓
ToolUseEvent(ReadFile, call_123, {...})
    ↓
ToolResultEvent(ReadFile, "port: 3000...", elapsed=0.05)
    ↓
TurnComplete(turn=1)
    ↓
[轮次 2 开始]
    ↓
StreamText("我看到")
StreamText("端口是")
StreamText("3000")
    ↓
ToolUseEvent(WriteFile, call_124, {...})
    ↓
ToolResultEvent(WriteFile, "成功写入", elapsed=0.02)
    ↓
TurnComplete(turn=2)
    ↓
[轮次 3 开始]
    ↓
StreamText("已完成")
StreamText("修改")
    ↓
UsageEvent(input=2500, output=450)
    ↓
LoopComplete(total_turns=3)
```

---

## 第三步：StreamCollector - 从 LLM 事件到 Agent 事件

### 问题：两种事件系统

**LLM 返回的事件**（来自 client.py）：

```python
TextDelta(text="好")
TextDelta(text="的")
ToolCallStart(tool_name="ReadFile", tool_id="call_123")
ToolCallDelta(text='{"file')
ToolCallDelta(text='_path"')
ToolCallComplete(tool_name="ReadFile", arguments={...})
StreamEnd(input_tokens=1200, output_tokens=50)
```

**Agent 需要的事件**：

```python
StreamText(text="好")        # 合并 TextDelta
StreamText(text="的")
ToolUseEvent(...)           # 只关心完整的工具调用
UsageEvent(...)             # 统计信息
```

### StreamCollector：事件转换器

```python
class StreamCollector:
    def __init__(self):
        self.response = LLMResponse()  # 累积响应
  
    async def consume(
        self, stream: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[AgentEvent]:
        """消费 LLM 流，转换为 Agent 事件"""
    
        async for event in stream:
            if isinstance(event, TextDelta):
                # 累积文本
                self.response.text += event.text
                # 立即转发给 UI
                yield StreamText(text=event.text)
        
            elif isinstance(event, ThinkingDelta):
                # 思考过程也立即转发
                yield ThinkingText(text=event.text)
        
            elif isinstance(event, ThinkingComplete):
                # 思考完成，记录完整内容
                self.response.thinking_blocks.append(
                    ThinkingBlock(thinking=event.thinking)
                )
        
            elif isinstance(event, ToolCallStart):
                # 工具开始，不发事件（等待完成）
                pass
        
            elif isinstance(event, ToolCallDelta):
                # 工具参数片段，不发事件（等待完成）
                pass
        
            elif isinstance(event, ToolCallComplete):
                # 工具调用完成，记录并发事件
                self.response.tool_calls.append(event)
                yield ToolUseEvent(
                    tool_name=event.tool_name,
                    tool_id=event.tool_id,
                    arguments=event.arguments,
                )
        
            elif isinstance(event, StreamEnd):
                # 流结束，记录统计信息
                self.response.stop_reason = event.stop_reason
                self.response.input_tokens = event.input_tokens
                self.response.output_tokens = event.output_tokens
                self.response.cache_read = event.cache_read
                self.response.cache_creation = event.cache_creation
```

**为什么需要 StreamCollector？**

1. **过滤噪音**：ToolCallDelta 对 Agent 层没用，不需要传播
2. **累积状态**：收集完整的 LLMResponse，供后续使用
3. **转换格式**：将 LLM 事件转为 Agent 层的事件

---

## 第四步：主循环的完整流程

### 一轮完整迭代

```

```

```python
async def run(self, conversation: ConversationManager):
    iteration = 0
  
    while True:
        iteration += 1
  
        # ========== 阶段 1: 准备 ==========
  
        # 1.1 检查迭代次数
        if iteration > self.max_iterations:
            yield ErrorEvent(message="达到最大迭代次数")
            break
  
        # 1.2 上下文压缩（如果接近上限）
        compact_result = await auto_compact(
            conversation,
            self.client,
            self.context_window,
            ...
        )
        if isinstance(compact_result, CompactEvent):
            yield CompactNotification(
                before_tokens=compact_result.before_tokens,
                message=f"上下文已压缩（压缩前 {compact_result.before_tokens:,} tokens）"
            )
  
        # 1.3 构建系统提示词
        system = build_system_prompt(...)
  
        # 1.4 获取工具列表
        tools = self.registry.get_all_schemas(self.protocol)
  
        # ========== 阶段 2: 调用 LLM ==========
  
        collector = StreamCollector()
        llm_stream = self.client.stream(conversation, system=system, tools=tools)
  
        # 转发所有流式事件
        async for event in collector.consume(llm_stream):
            yield event
  
        response = collector.response
  
        # ========== 阶段 3: 发出用量统计 ==========
  
        self.total_input_tokens += response.input_tokens
        self.total_output_tokens += response.output_tokens
        yield UsageEvent(
            input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens,
        )
  
        # ========== 阶段 4: 检查是否结束 ==========
  
        if not response.tool_calls:
            # 没有工具调用 → 对话结束
            conversation.add_assistant_message(response.text)
            yield LoopComplete(total_turns=iteration)
            break
  
        # ========== 阶段 5: 执行工具 ==========
  
        # 将 AI 的回复加入历史
        tool_uses = [
            ToolUseBlock(
                tool_use_id=tc.tool_id,
                tool_name=tc.tool_name,
                arguments=tc.arguments,
            )
            for tc in response.tool_calls
        ]
        conversation.add_assistant_message(response.text, tool_uses)
  
        # 批量执行工具
        tool_results = []
        batches = partition_tool_calls(response.tool_calls, self.registry)
  
        for batch in batches:
            if batch.concurrent and len(batch.calls) > 1:
                # 并发执行安全的工具
                batch_results = await self._execute_batch_parallel(batch.calls)
                for br in batch_results:
                    tool_results.append(...)
                    yield ToolResultEvent(
                        tool_name=br.tool_name,
                        output=br.result.output,
                        is_error=br.result.is_error,
                        elapsed=br.elapsed,
                    )
            else:
                # 串行执行不安全的工具
                for tc in batch.calls:
                    async for item in self._execute_tool(tc):
                        if isinstance(item, PermissionRequest):
                            yield item  # 请求用户授权
                        else:
                            result, elapsed, is_unknown = item
                            tool_results.append(...)
                            yield ToolResultEvent(...)
  
        # 将工具结果加入历史
        conversation.add_tool_results_message(tool_results)
  
        yield TurnComplete(turn=iteration)
  
        # 进入下一轮迭代
```

---

## 第五步：工具执行的三种策略

### 策略 1：串行执行（默认）

```python
for tc in tool_calls:
    result = await execute_tool(tc)
    yield ToolResultEvent(...)
```

**适用场景**：

- 工具不是并发安全的（WriteFile、EditFile）
- 工具之间有依赖关系

**示例**：

```
AI: 先读取 config.yaml，再修改它
  ↓
执行 ReadFile("config.yaml")  ← 等待完成
  ↓
执行 WriteFile("config.yaml", new_content)  ← 等待完成
```

### 策略 2：并发执行

```python
if batch.concurrent and len(batch.calls) > 1:
    tasks = [execute_tool(tc) for tc in batch.calls]
    results = await asyncio.gather(*tasks)
```

**适用场景**：

- 工具标记为 `is_concurrency_safe = True`
- 多个独立的工具调用

**示例**：

```
AI: 读取 a.txt, b.txt, c.txt 三个文件
  ↓
并发执行:
  ReadFile("a.txt")  ─┐
  ReadFile("b.txt")  ─┼─ 同时执行
  ReadFile("c.txt")  ─┘
  ↓
3 个结果同时返回
```

### 策略 3：批次混合执行

```python
def partition_tool_calls(tool_calls, registry) -> list[ToolBatch]:
    """将工具调用分组"""
    batches = []
    for tc in tool_calls:
        tool = registry.get(tc.tool_name)
        safe = tool.is_concurrency_safe and registry.is_enabled(tc.tool_name)
    
        if safe and batches and batches[-1].concurrent:
            # 加入当前并发批次
            batches[-1].calls.append(tc)
        else:
            # 创建新批次
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches
```

**示例**：

```
AI 调用顺序: [ReadFile, ReadFile, WriteFile, ReadFile, ReadFile]
  ↓
分组结果:
  批次 1 (并发): [ReadFile, ReadFile]
  批次 2 (串行): [WriteFile]
  批次 3 (并发): [ReadFile, ReadFile]
  ↓
执行:
  [ReadFile, ReadFile] 并发执行 → 完成
  [WriteFile] 串行执行 → 完成
  [ReadFile, ReadFile] 并发执行 → 完成
```

---

## 第六步：错误处理与重试

### 场景 1：Token 超限

```python
if response.stop_reason == "max_tokens":
    if not max_tokens_escalated:
        # 第一次超限：增加 token 限制
        self.client.set_max_output_tokens(MAX_TOKENS_CEILING)
        max_tokens_escalated = True
    
        # 将当前回复加入历史
        conversation.add_assistant_message(response.text)
        # 提示 AI 继续
        conversation.add_user_message(
            "Output token limit hit. Resume directly from where you stopped."
        )
    
        yield RetryEvent(reason="max_tokens escalation")
        continue  # 重新进入循环
  
    elif output_recoveries < MAX_OUTPUT_TOKENS_RECOVERIES:
        # 第 2-4 次超限：提示 AI 分块
        output_recoveries += 1
        conversation.add_assistant_message(response.text)
        conversation.add_user_message(
            "Output token limit hit. Break remaining work into smaller pieces."
        )
    
        yield RetryEvent(
            reason=f"max_tokens recovery {output_recoveries}/{MAX_OUTPUT_TOKENS_RECOVERIES}"
        )
        continue
```

**用户视角**：

```
AI: 正在写一个很长的文件...这是第一部分...这是第二部分...
[Token 超限]
系统: [自动重试，增加 token 限制]
AI: ...这是第三部分...这是第四部分...
[再次超限]
系统: [提示 AI 分块]
AI: 由于内容较长，我将分两次写入...
```

### 场景 2：连续调用未知工具

```python
consecutive_unknown = 0

for tc in tool_calls:
    tool = registry.get(tc.tool_name)
    if tool is None:
        consecutive_unknown += 1
    else:
        consecutive_unknown = 0

if consecutive_unknown >= 3:
    yield ErrorEvent(
        message="Agent terminated: too many consecutive unknown tool calls"
    )
    break
```

**为什么需要这个机制？**

AI 可能陷入循环：

```
轮次 1: AI 调用 "ReadFil" (拼写错误) → 失败
轮次 2: AI 调用 "ReadFil" (没有学到教训) → 失败
轮次 3: AI 调用 "ReadFil" (仍然错误) → 失败
轮次 4: 系统终止对话，避免无限循环
```

### 场景 3：达到最大迭代次数

```python
if iteration > self.max_iterations:
    yield ErrorEvent(
        message=f"Agent reached maximum iterations ({self.max_iterations})"
    )
    break
```

**防止无限循环**：

```
默认: max_iterations = 100

如果 AI 陷入死循环（工具调用一直不成功），100 轮后强制终止
```

---

## 第七步：权限请求与用户交互

### 异步权限请求

```python
async def _execute_tool(self, tc: ToolCallComplete):
    """执行工具，可能请求权限"""
  
    # 检查权限
    decision = self.permission_checker.check(tc.tool_name, tc.arguments)
  
    if decision == Decision.PROMPT:
        # 需要用户授权
        future = asyncio.Future()
        yield PermissionRequest(
            tool_name=tc.tool_name,
            description=self._build_permission_description(tc),
            future=future,
        )
    
        # 等待用户响应
        response = await future
    
        if response == PermissionResponse.DENY:
            # 用户拒绝
            yield ToolResult(
                output="User denied execution",
                is_error=True
            ), 0.0, False
            return
  
    # 执行工具
    result = await tool.execute(params)
    yield result, elapsed, False
```

### 用户视角

```
AI: 调用 Bash("rm -rf temp/")
  ↓
[弹出确认框]
  工具: Bash
  命令: rm -rf temp/
  [允许] [拒绝] [总是允许]
  ↓
用户点击 [允许]
  ↓
系统: 执行命令
AI: 已删除临时目录
```

---

## 第八步：Hook 系统 - 生命周期钩子

### Hook 事件点

```python
# 会话级别
await hook_engine.run_hooks("session_start", ctx)
await hook_engine.run_hooks("session_end", ctx)

# 轮次级别
await hook_engine.run_hooks("turn_start", ctx)
await hook_engine.run_hooks("turn_end", ctx)

# 工具级别
await hook_engine.run_hooks("pre_tool_use", ctx)
await hook_engine.run_hooks("post_tool_use", ctx)

# LLM 级别
await hook_engine.run_hooks("pre_send", ctx)
await hook_engine.run_hooks("post_receive", ctx)
```

### 实际应用

**示例 1：自动保存检查点**

```python
# 配置 Hook
hooks:
  - event: turn_end
    command: git add . && git commit -m "Auto checkpoint"
```

**示例 2：工具使用日志**

```python
# 配置 Hook
hooks:
  - event: post_tool_use
    command: echo "[$(date)] Tool: {{tool_name}}" >> tools.log
```

**主循环中的处理**：

```python
if self.hook_engine:
    ctx = self._build_hook_context("turn_end")
    await self.hook_engine.run_hooks("turn_end", ctx)
  
    # 收集 Hook 输出
    for he in self._drain_hook_events():
        yield he  # 转发 HookEvent
```

---

## 代码亮点：设计模式与最佳实践

### 亮点 1：生成器模式（AsyncIterator）

```python
async def run(self) -> AsyncIterator[AgentEvent]:
    """主循环是一个异步生成器"""
  
    # 不是:
    events = []
    events.append(StreamText("..."))
    return events  # 等所有事件完成才返回
  
    # 而是:
    yield StreamText("...")  # 立即返回，UI 实时更新
    yield ToolUseEvent(...)
    yield ToolResultEvent(...)
```

**好处**：

- ✨ 实时响应：事件发生立即通知 UI
- ✨ 内存高效：不需要存储所有事件
- ✨ 可中断：用户可以随时取消

### 亮点 2：事件分层

```
LLM 层事件 (TextDelta, ToolCallDelta, ...)
    ↓ StreamCollector 转换
Agent 层事件 (StreamText, ToolUseEvent, ...)
    ↓ 主循环转发
UI 层 (实时显示)
```

**为什么分层？**

- LLM 层：关注协议细节（JSON 片段、流式传输）
- Agent 层：关注业务逻辑（工具调用、权限检查）
- UI 层：关注用户体验（进度显示、交互）

每层只关心自己的职责，解耦清晰。

### 亮点 3：状态机式循环

```
[开始] → [准备] → [调用 LLM] → [检查结果]
                                      ↓
                               有工具调用? 
                               ↙        ↘
                            [是]        [否]
                             ↓           ↓
                        [执行工具]    [结束]
                             ↓
                        [加入历史]
                             ↓
                        [返回准备] ← 循环
```

每个状态清晰，转换条件明确。

### 亮点 4：批次优化

```python
def partition_tool_calls(tool_calls, registry):
    """智能分组：安全的并发，不安全的串行"""
    batches = []
    for tc in tool_calls:
        safe = tool.is_concurrency_safe
        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(tc)  # 加入当前批次
        else:
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches
```

**效果**：

```
调用: [R, R, W, R, R]  (R=ReadFile, W=WriteFile)
分组: [[R,R], [W], [R,R]]
执行: 并发 → 串行 → 并发
性能: 比全串行快 2-3 倍
安全: WriteFile 仍然串行执行
```

### 亮点 5：容错与降级

**多层容错**：

```
第 1 层: 工具不存在
    → 返回错误，AI 自行修正

第 2 层: 连续 3 次未知工具
    → 强制终止，避免死循环

第 3 层: Token 超限
    → 自动重试 + 增加限制

第 4 层: 达到最大迭代次数
    → 强制终止，避免无限循环

第 5 层: 未捕获异常
    → try-except 捕获，转为 ErrorEvent
```

每一层都有降级策略，系统永不崩溃。

### 亮点 6：权限系统的异步设计

```python
# 错误的同步设计:
if need_permission:
    response = input("允许吗? [Y/N]")  # 阻塞整个事件循环！
    if response == "N":
        return error

# 正确的异步设计:
if need_permission:
    future = asyncio.Future()
    yield PermissionRequest(..., future=future)  # 立即返回
    response = await future  # 等待用户响应，不阻塞其他任务
```

**好处**：

- UI 线程可以继续响应
- 其他 Agent 可以继续工作
- 用户可以看到完整的上下文

---

## 完整流程图

```
用户输入: "读取 config.yaml 并修改端口"
    ↓
[主循环启动]
    ↓
═══════════════════════════════════════
    轮次 1
═══════════════════════════════════════
    ↓
1. 检查迭代次数 (1 / 100) ✓
    ↓
2. 上下文压缩检查 (未超限，跳过)
    ↓
3. 构建系统提示词
    ↓
4. 获取工具列表: [ReadFile, WriteFile, Bash, ...]
    ↓
5. 调用 LLM
    ↓
   [流式响应开始]
    ├─ yield StreamText("好")
    ├─ yield StreamText("的")
    ├─ yield StreamText("，")
    ├─ yield StreamText("让我")
    ├─ yield StreamText("读取")
    ├─ yield StreamText("文件")
    ├─ yield ToolUseEvent(ReadFile, "config.yaml")
    └─ [流式响应结束]
    ↓
6. yield UsageEvent(input=1200, output=50)
    ↓
7. 检查: 有工具调用 → 继续
    ↓
8. 将 AI 回复加入历史
    ↓
9. 执行工具: ReadFile("config.yaml")
    ├─ 从缓存读取 (未命中)
    ├─ 从磁盘读取
    └─ 返回内容
    ↓
10. yield ToolResultEvent(output="port: 3000\n...", elapsed=0.05)
    ↓
11. 将工具结果加入历史
    ↓
12. yield TurnComplete(turn=1)
    ↓
═══════════════════════════════════════
    轮次 2
═══════════════════════════════════════
    ↓
1. 检查迭代次数 (2 / 100) ✓
    ↓
2-4. [重复准备步骤]
    ↓
5. 调用 LLM (历史包含 ReadFile 结果)
    ↓
   [流式响应开始]
    ├─ yield StreamText("我")
    ├─ yield StreamText("看到")
    ├─ yield StreamText("端口是")
    ├─ yield StreamText("3000")
    ├─ yield ToolUseEvent(WriteFile, "config.yaml", new_content)
    └─ [流式响应结束]
    ↓
6. yield UsageEvent(input=1450, output=80)
    ↓
7. 检查: 有工具调用 → 继续
    ↓
8. 执行工具: WriteFile("config.yaml", new_content)
    ├─ 检查权限 (category="write" → 需要确认)
    ├─ yield PermissionRequest(...)
    ├─ 等待用户响应
    ├─ 用户允许
    ├─ 写入文件
    └─ 返回成功
    ↓
9. yield ToolResultEvent(output="成功写入", elapsed=0.02)
    ↓
10. 将工具结果加入历史
    ↓
11. yield TurnComplete(turn=2)
    ↓
═══════════════════════════════════════
    轮次 3
═══════════════════════════════════════
    ↓
1-4. [重复准备步骤]
    ↓
5. 调用 LLM (历史包含 WriteFile 成功)
    ↓
   [流式响应开始]
    ├─ yield StreamText("已")
    ├─ yield StreamText("完成")
    ├─ yield StreamText("修改")
    ├─ yield StreamText("，")
    ├─ yield StreamText("端口")
    ├─ yield StreamText("已改为")
    ├─ yield StreamText("8080")
    └─ [流式响应结束，无工具调用]
    ↓
6. yield UsageEvent(input=1600, output=120)
    ↓
7. 检查: 无工具调用 → 结束
    ↓
8. 将 AI 回复加入历史
    ↓
9. yield LoopComplete(total_turns=3)
    ↓
[主循环结束]
```

---

## 实际应用场景

### 场景 1：多文件批量处理

```python
用户: "读取 src/ 下所有 .py 文件，统计行数"

轮次 1:
  AI: [调用 Glob("src/**/*.py")]
  系统: [返回 file1.py, file2.py, file3.py]

轮次 2:
  AI: [并发调用 ReadFile(file1), ReadFile(file2), ReadFile(file3)]
  系统: [同时返回 3 个文件内容]  ← 并发执行，快！

轮次 3:
  AI: "共 3 个文件，总计 1250 行"
```

### 场景 2：错误自动恢复

```python
轮次 1:
  AI: [调用 ReadFile("config.yml")]  ← 拼写错误
  系统: Error: file not found

轮次 2:
  AI: [调用 ReadFile("config.yaml")]  ← 自动修正
  系统: [返回文件内容]

轮次 3:
  AI: "文件内容如下..."
```

### 场景 3：长文本分块

```python
轮次 1:
  AI: [写入 5000 行代码]
  系统: [Token 超限，自动重试]

轮次 2:
  AI: "由于内容较长，我分两次写入"
  AI: [写入前 2500 行]
  系统: [成功]

轮次 3:
  AI: [写入后 2500 行]
  系统: [成功]

轮次 4:
  AI: "已完成写入"
```

---

## 总结

Agent 主循环与事件流解决了什么问题：

1. **持续交互**：while True 循环，让 AI 可以多轮对话
2. **实时反馈**：AsyncIterator 事件流，UI 实时更新
3. **智能执行**：批次分组，安全的工具并发执行
4. **错误处理**：多层容错，自动重试和降级
5. **权限控制**：异步权限请求，不阻塞事件循环
6. **生命周期管理**：Hook 系统，在关键节点插入自定义逻辑

**代码亮点**：

- ✨ **生成器模式**：实时事件流，内存高效
- ✨ **事件分层**：LLM / Agent / UI 职责清晰
- ✨ **状态机循环**：清晰的状态转换
- ✨ **批次优化**：智能分组并发执行
- ✨ **多层容错**：每层都有降级策略
- ✨ **异步权限**：不阻塞其他任务

下一章我们将学习：对话管理与上下文压缩如何工作？
