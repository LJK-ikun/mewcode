# CH01: LLM客户端与流式响应

## 从一次对话说起

想象你正在和 AI 对话：

1. 你输入："帮我读取 README.md 文件"
2. 屏幕上字符一个个蹦出来："好的，让我读取这个文件..."
3. 突然出现 `[调用工具: Read]`
4. 然后显示文件内容
5. 最后显示：`用量: 1200 tokens 输入 / 350 tokens 输出`

这个看似简单的交互背后，发生了什么？数据是怎么流动的？这就是本章要讲的内容。

---

## 第一步：你的消息如何发送给 AI

### 准备阶段

当你按下回车，系统首先要做三件事：

```python
# 1. 构建对话历史
messages = build_anthropic_messages(conversation.get_messages())
# 把你之前的所有对话整理成 AI 能理解的格式

# 2. 标记缓存点（这是省钱的关键！）
_mark_last_user_tail_for_cache(messages)
# 告诉 AI："这部分内容下次直接用缓存，别重新处理"

# 3. 组装请求参数
kwargs = {
    "model": "claude-sonnet-4",
    "max_tokens": 4096,
    "messages": messages,
    "system": "你是一个代码助手...",
    "tools": [ReadTool, WriteTool, ...]  # 可用的工具列表
}
```

### 为什么要缓存？

假设你和 AI 聊了 10 轮，每次都要把前面 9 轮的内容重新发给 AI。这太浪费了！

Anthropic 的缓存机制：

- **第 1 次请求**：发送 10000 tokens，全价计费
- **第 2 次请求**：前 9000 tokens 命中缓存，只需支付 10% 费用！

系统在三个地方打"缓存点"：

1. System prompt（系统提示词）—— 永远不变
2. Tools 定义（工具列表）—— 基本不变
3. 最后一条用户消息 —— 对话历史在这里断开

```python
# 代码细节：client.py:179
_mark_last_user_tail_for_cache(messages)

# 它做的事情：找到最后一条 user 消息，加上标记
msg["content"][-1]["cache_control"] = {"type": "ephemeral"}
```

---

## 第二步：AI 开始回复 —— 流式响应的奥秘

### 为什么不等 AI 说完再显示？

想象两种体验：

**方式 A（阻塞式）**：

```
[等待中...] 
[等待中...] 
[等待中...]
[5秒后] "好的，让我读取这个文件，调用 Read 工具..."
```

**方式 B（流式）**：

```
好 的 ， 让 我 读 取 这 个 文 件 ， 调 用  Read  工 具 ...
```

显然方式 B 体验更好！这就是**流式响应**的意义。

### 数据流的真实样子

当 AI 开始回复，服务器不是一次性返回完整响应，而是像水流一样持续发送"事件"：

```python
async with self._client.messages.stream(**kwargs) as stream:
    async for event in stream:
        # event 可能是：
        # - "content_block_start": 开始一个新的内容块
        # - "content_block_delta": 内容增量（文字片段）
        # - "content_block_stop": 内容块结束
        # - "message_stop": 整个回复结束
```

### 七种事件类型

系统定义了 7 种事件，对应 AI 回复的不同部分：

| 事件                 | 含义         | 携带的数据                                      |
| -------------------- | ------------ | ----------------------------------------------- |
| `TextDelta`        | 文本片段     | `text: "好的，"`                              |
| `ToolCallStart`    | 开始调用工具 | `tool_name: "Read", tool_id: "call_123"`      |
| `ToolCallDelta`    | 工具参数片段 | `text: '{"file_path": "/home/'`               |
| `ToolCallComplete` | 工具调用完成 | `arguments: {"file_path": "/home/README.md"}` |
| `ThinkingDelta`    | 思考过程片段 | `text: "用户想要..."`                         |
| `ThinkingComplete` | 思考完成     | `thinking: "用户想要读取文件..."`             |
| `StreamEnd`        | 整个回复结束 | `input_tokens: 1200, output_tokens: 350`      |

```python
# 定义在 tools/base.py
@dataclass
class TextDelta:
    text: str

@dataclass
class ToolCallComplete:
    tool_id: str
    tool_name: str
    arguments: dict[str, Any]  # 已经解析好的 JSON
```

---

## 第三步：如何把碎片拼成完整信息

### 问题：工具参数是怎么来的？

AI 说要调用 `Read` 工具，参数是 `{"file_path": "/home/README.md"}`。

但服务器发来的数据是这样的：

```
事件1: ToolCallStart(tool_name="Read", tool_id="call_123")
事件2: ToolCallDelta(text='{"fi')
事件3: ToolCallDelta(text='le_pa')
事件4: ToolCallDelta(text='th": "/')
事件5: ToolCallDelta(text='home/RE')
事件6: ToolCallDelta(text='ADME.md"}')
事件7: ToolCallComplete(...)
```

系统需要把这些碎片拼起来！

### 状态累积机制

```python
# 在 stream() 函数内部维护状态变量
current_tool_name = ""
current_tool_id = ""
json_accum = ""  # 累积 JSON 片段

async for event in stream:
    if event.type == "content_block_start":
        # 开始一个新工具调用
        if block.type == "tool_use":
            current_tool_name = block.name
            current_tool_id = block.id
            json_accum = ""
            yield ToolCallStart(...)
  
    elif event.type == "content_block_delta":
        # 收到 JSON 片段，累积起来
        if delta.type == "input_json_delta":
            json_accum += delta.partial_json  # '{"fi' + 'le_pa' + ...
            yield ToolCallDelta(text=delta.partial_json)
  
    elif event.type == "content_block_stop":
        # 工具调用结束，解析完整 JSON
        if current_tool_name:
            args = json.loads(json_accum)  # 解析成字典
            yield ToolCallComplete(
                tool_name=current_tool_name,
                arguments=args,  # {"file_path": "/home/README.md"}
            )
            # 清空状态，准备下一个工具调用
            current_tool_name = ""
            json_accum = ""
```

**关键点**：

- 用局部变量 `json_accum` 累积片段
- 在 `content_block_stop` 时一次性解析
- 如果 JSON 格式错误，降级为空字典 `{}`

---

## 第四步：Thinking 模式 —— AI 的"草稿纸"

### 什么是 Thinking？

有些复杂问题，AI 需要"思考一下"。启用 thinking 模式后：

```
[思考] 用户想读取 README.md，我应该调用 Read 工具，参数是...
[输出] 好的，让我读取这个文件
[调用工具] Read(file_path="/home/README.md")
```

思考过程对用户可见，但不影响最终输出。

### 代码实现

```python
# 请求时启用 thinking
if self.thinking:
    if _supports_adaptive_thinking(self.model):
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 0}
        # 新模型自动分配 thinking token
    else:
        kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": max(self.max_output_tokens - 1, 1024),
        }
        # 旧模型需要手动设置预算

# 处理 thinking 事件
in_thinking = False
thinking_accum = ""

if event.type == "content_block_start":
    if block.type == "thinking":
        in_thinking = True
        thinking_accum = ""

elif event.type == "content_block_delta":
    if delta.type == "thinking_delta":
        thinking_accum += delta.thinking
        yield ThinkingDelta(text=delta.thinking)  # 实时显示

elif event.type == "content_block_stop":
    if in_thinking:
        yield ThinkingComplete(thinking=thinking_accum)
        in_thinking = False
```

---

## 第五步：最终统计 —— 花了多少钱

### Token 用量的三个维度

当 AI 回复完成，`StreamEnd` 事件包含完整的用量统计：

```python
yield StreamEnd(
    stop_reason="end_turn",
    input_tokens=1200,       # 实际处理的输入 token
    output_tokens=350,       # 生成的输出 token
    cache_read=8500,         # 从缓存读取的 token（省钱！）
    cache_creation=0,        # 写入缓存的 token
)
```

**计费公式**：

```
总 prompt 大小 = input_tokens + cache_read + cache_creation
实际费用 = input_tokens × 100% + cache_read × 10% + cache_creation × 100%
```

示例：

```
第 1 次对话：
  input_tokens: 10000 (全价)
  cache_creation: 10000 (写入缓存，全价)
  费用: 20000 tokens

第 2 次对话：
  input_tokens: 100 (只有新消息)
  cache_read: 10000 (从缓存读取，10% 价格)
  费用: 100 + 10000×0.1 = 1100 tokens

节省: (20000 - 1100) / 20000 = 94.5%！
```

---

## OpenAI 的差异 —— 为什么需要三个客户端

### 协议不同，处理方式不同

| 协议                      | API 端点                 | 适用场景                   |
| ------------------------- | ------------------------ | -------------------------- |
| Anthropic                 | `/v1/messages`         | Claude 模型                |
| OpenAI (Responses)        | `/v1/responses`        | OpenAI 新版 API            |
| OpenAI (Chat Completions) | `/v1/chat/completions` | vLLM, Ollama, 各种兼容服务 |

### 核心差异示例

**Anthropic**：

```python
# 工具调用是单个事件流
event.type == "content_block_start"  # 开始
event.delta.type == "input_json_delta"  # JSON 片段
event.type == "content_block_stop"  # 结束
```

**OpenAI Chat Completions**：

```python
# 工具调用按索引分组
delta.tool_calls[0].index = 0  # 第一个工具
delta.tool_calls[0].function.arguments = '{"arg'
delta.tool_calls[0].function.arguments = '1": "v'
...
delta.tool_calls[1].index = 1  # 第二个工具
```

系统需要维护 `active_calls` 字典，按索引跟踪：

```python
active_calls = {}  # {索引: {id, name, args}}

for tc in delta.tool_calls:
    idx = tc.index
    if idx not in active_calls:
        active_calls[idx] = {"id": "", "name": "", "args": ""}
  
    call = active_calls[idx]
    if tc.function.arguments:
        call["args"] += tc.function.arguments
```

---

## 错误处理 —— 出问题了怎么办

### 四种常见错误

```python
try:
    async with self._client.messages.stream(**kwargs) as stream:
        ...
except _anthropic.AuthenticationError as e:
    # API key 无效 → 用户需要修复配置
    raise AuthenticationError("Invalid API key")

except _anthropic.RateLimitError as e:
    # 速率限制 → 可以重试，可能有 retry-after 头
    retry = e.response.headers.get("retry-after")
    raise RateLimitError("Rate limited", retry_after=float(retry))

except _anthropic.APIConnectionError as e:
    # 网络问题 → 提示用户检查网络
    raise NetworkError("Network error")

except _anthropic.APIStatusError as e:
    # 其他 API 错误 → 记录日志
    raise LLMError(f"API error ({e.status_code})")
```

### 优雅降级

关键原则：**非致命错误不应该让系统崩溃**

```python
# 示例：获取模型的 context window 大小
async def fetch_model_context_window(self) -> int | None:
    try:
        info = await self._client.models.retrieve(self.model, timeout=3.0)
        return info.max_input_tokens
    except Exception:
        return None  # 失败了也没关系，用默认值
```

---

## 完整流程图

```
用户输入
   ↓
准备请求
   ├─ 构建对话历史 (build_anthropic_messages)
   ├─ 标记缓存点 (_mark_last_user_tail_for_cache)
   └─ 组装参数 (model, messages, system, tools)
   ↓
发送请求 (client.messages.stream)
   ↓
接收事件流
   ├─ content_block_start
   │    ├─ thinking → 初始化 thinking 状态
   │    └─ tool_use → 初始化 tool call 状态 → yield ToolCallStart
   │
   ├─ content_block_delta
   │    ├─ text_delta → yield TextDelta (实时显示文字)
   │    ├─ thinking_delta → 累积 thinking → yield ThinkingDelta
   │    └─ input_json_delta → 累积 JSON → yield ToolCallDelta
   │
   ├─ content_block_stop
   │    ├─ thinking 结束 → yield ThinkingComplete
   │    └─ tool call 结束 → 解析 JSON → yield ToolCallComplete
   │
   └─ message_stop
        ↓
获取最终统计 (stream.get_final_message)
   ↓
yield StreamEnd (token 用量)
```

---

## 核心设计思想

### 1. 统一接口，隔离差异

```python
class LLMClient(ABC):
    @abstractmethod
    async def stream(...) -> AsyncIterator[StreamEvent]:
        ...

# 上层代码只需要：
async for event in client.stream(...):
    if isinstance(event, TextDelta):
        print(event.text)
```

无论是 Anthropic 还是 OpenAI，上层代码不变。

### 2. 流式优先，体验至上

每个事件立即 `yield`，不等待完整响应。

### 3. 状态累积，容错处理

```python
# JSON 解析失败不崩溃
try:
    args = json.loads(json_accum)
except json.JSONDecodeError:
    args = {}  # 降级为空字典
```

### 4. 成本优化内置

缓存机制默认启用，不需要用户手动配置。

---

## 实际应用场景

### 场景 1：实时显示 AI 回复

```python
async for event in client.stream(conversation, system, tools):
    if isinstance(event, TextDelta):
        # 每收到一个字符就显示
        print(event.text, end="", flush=True)
```

### 场景 2：显示工具调用状态

```python
async for event in client.stream(...):
    if isinstance(event, ToolCallStart):
        print(f"\n[🔧 调用 {event.tool_name}]")
    elif isinstance(event, ToolCallComplete):
        print(f"[✓ 完成，参数: {event.arguments}]")
```

### 场景 3：监控成本

```python
async for event in client.stream(...):
    if isinstance(event, StreamEnd):
        total_cost = (
            event.input_tokens +
            event.cache_read * 0.1 +
            event.cache_creation
        )
        print(f"本次花费: {total_cost} tokens")
    
        cache_hit_rate = event.cache_read / (
            event.input_tokens + event.cache_read + event.cache_creation
        )
        print(f"缓存命中率: {cache_hit_rate:.1%}")
```

---

## 代码亮点：工厂函数的精妙设计

### 亮点 1：`create_client()` - 简单但关键的工厂

```python
def create_client(config: ProviderConfig) -> LLMClient:
    if config.protocol == "anthropic":
        return AnthropicClient(config)
    elif config.protocol == "openai":
        return OpenAIClient(config)
    elif config.protocol == "openai-compat":
        return OpenAICompatClient(config)
    raise ValueError(f"Unknown protocol: {config.protocol}")
```

**看起来简单，但解决了大问题**：

#### 问题：如果没有这个工厂函数

```python
# 上层代码会变成这样：
if config.protocol == "anthropic":
    client = AnthropicClient(config)
    async for event in client.stream(...):
        ...
elif config.protocol == "openai":
    client = OpenAIClient(config)
    async for event in client.stream(...):
        ...
elif config.protocol == "openai-compat":
    client = OpenAICompatClient(config)
    async for event in client.stream(...):
        ...
```

每个调用处都要写一遍 if-else，添加新 provider 时要改 N 个地方！

#### 解决方案：工厂函数 + 抽象基类

```python
# 上层代码只需要：
client = create_client(config)  # 自动选择正确的客户端
async for event in client.stream(...):
    if isinstance(event, TextDelta):
        print(event.text)
```

**好处**：

1. **单一职责**：创建逻辑集中在一处
2. **易扩展**：新增 provider 只需改工厂函数
3. **类型安全**：返回类型是 `LLMClient`，保证接口一致

### 亮点 2：`resolve_context_window()` - 三层降级策略

这个函数看起来平淡，实际上体现了**生产级代码的容错思维**。

#### 问题：不同模型的 context window 不一样

- Claude Sonnet 3.5: 200k tokens
- Claude Opus 4: 200k tokens
- GPT-4: 128k tokens
- 自定义模型: ？？？

系统需要知道模型能接受多大的输入，才能正确管理对话历史。

#### 三层解析策略

```python
async def resolve_context_window(config: ProviderConfig) -> None:
    # 第 0 层：用户显式配置（最高优先级）
    if config.context_window > 0:
        return  # 用户说了算，直接用
  
    # 第 1 层：上次已经拉取过，用缓存
    if config._fetched_context_window > 0:
        return
  
    # 第 2 层：自动从 API 拉取（本函数实现）
    if config.protocol != "anthropic":
        return  # 只支持 anthropic 协议
  
    try:
        client = create_client(config)
        window = await client.fetch_model_context_window()
        if window:
            config.set_fetched_context_window(window)
    except Exception:
        pass  # 失败也不要紧
  
    # 第 3 层：内置映射表（在 config.py 中）
    # 如果以上都失败，get_context_window() 会返回默认值
```

**每一层都是保险**：

| 层级 | 数据源       | 优先级 | 失败后果    |
| ---- | ------------ | ------ | ----------- |
| 0    | 用户配置文件 | 最高   | -           |
| 1    | 本地缓存     | 高     | 降到第 2 层 |
| 2    | API 自动拉取 | 中     | 降到第 3 层 |
| 3    | 内置映射表   | 低     | 降到默认值  |

#### 为什么这样设计？

**尽力而为（Best Effort）原则**：

```python
async def fetch_model_context_window(self) -> int | None:
    try:
        info = await self._client.models.retrieve(
            self.model, 
            timeout=3.0  # 只等 3 秒
        )
        return info.max_input_tokens
    except Exception:
        return None  # 任何错误都返回 None，不抛异常
```

**实际场景**：

1. **网络抖动** → 3 秒超时 → 返回 None → 用默认值
2. **API key 未配置** → 客户端创建失败 → 返回 None → 用默认值
3. **自定义 base_url 不支持 /models 端点** → 请求失败 → 返回 None → 用默认值

**绝不因为非核心功能而让系统无法启动！**

### 亮点 3：`_mark_last_tool_for_cache()` - 浅拷贝的智慧

```python
def _mark_last_tool_for_cache(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回一个浅拷贝的 tools 列表，并在最后一个 tool 上标记 cache_control"""
    if not tools:
        return tools
    marked = list(tools)  # 浅拷贝列表
    last = dict(marked[-1])  # 拷贝最后一个 dict
    last["cache_control"] = _EPHEMERAL
    marked[-1] = last
    return marked
```

#### 为什么要拷贝？

**问题场景**：

```python
# 全局注册的工具定义（单例）
TOOL_REGISTRY = [
    {"name": "Read", "description": "...", "input_schema": {...}},
    {"name": "Write", "description": "...", "input_schema": {...}},
]

# 如果直接修改：
tools[-1]["cache_control"] = {"type": "ephemeral"}

# 下次使用时，全局单例已经被污染了！
# 而且多线程环境下会有竞争条件
```

**解决方案**：

```python
marked = list(tools)  # 创建新列表
last = dict(marked[-1])  # 创建新字典
last["cache_control"] = _EPHEMERAL  # 只修改新字典
marked[-1] = last  # 替换列表中的引用
return marked  # 返回新列表
```

这样原始的 `TOOL_REGISTRY` 保持不变，每次调用都创建独立的副本。

**成本分析**：

- 只拷贝列表框架和最后一个元素：O(n) 时间，O(1) 额外空间
- 其他元素共享引用，不浪费内存
- 避免了全局状态污染和并发问题

### 亮点 4：抽象基类 `LLMClient` - 接口即契约

```python
class LLMClient(ABC):
    @abstractmethod
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield TextDelta("")
  
    def set_max_output_tokens(self, tokens: int) -> None:
        pass
```

#### 为什么需要抽象基类？

**Python 是动态类型语言**，不强制接口检查：

```python
# 没有抽象基类时：
class MyBrokenClient:
    async def streem(self, ...):  # 拼写错误！
        ...

client = MyBrokenClient()
# 运行时才发现：AttributeError: 'MyBrokenClient' object has no attribute 'stream'
```

**使用抽象基类后**：

```python
class MyBrokenClient(LLMClient):
    pass  # 忘记实现 stream()

# 实例化时立即报错：
# TypeError: Can't instantiate abstract class MyBrokenClient 
# with abstract method stream
```

**契约保证**：

1. 所有 `LLMClient` 子类必须实现 `stream()` 方法
2. 类型系统（mypy/pyright）可以静态检查
3. IDE 可以提供准确的代码补全

---

## 总结

从用户输入到 AI 回复，数据流经历了：

1. **准备阶段**：构建消息、标记缓存点
2. **流式接收**：实时处理 7 种事件类型
3. **状态累积**：把 JSON 片段拼成完整参数
4. **最终统计**：计算 token 用量和费用

**代码亮点**：

- ✨ **工厂函数**：`create_client()` 集中创建逻辑，易扩展
- ✨ **三层降级**：`resolve_context_window()` 尽力而为，永不崩溃
- ✨ **浅拷贝智慧**：`_mark_last_tool_for_cache()` 避免污染全局状态
- ✨ **抽象基类**：`LLMClient` 强制接口契约，类型安全

核心原则：

- **流式优先** → 更好的用户体验
- **容错降级** → 出错不崩溃
- **成本优化** → 缓存机制内置
- **协议隔离** → 统一接口屏蔽差异

下一章我们将学习：当 AI 说"调用 Read 工具"后，系统如何真正去读取文件？
