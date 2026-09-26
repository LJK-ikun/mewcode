# CH06: MCP协议与工具适配

## 从一个真实需求说起

你的 AI Agent 已经很强大了：能读写文件、执行命令、搜索代码。但用户突然说：

> "我想让 AI 访问我的 Notion 笔记、查询 PostgreSQL 数据库、控制我的智能家居..."

**问题来了：**
- 每增加一个外部系统，就要写一套新的工具代码？
- 不同系统的认证方式、通信协议都不一样，如何统一管理？
- 第三方开发者想贡献工具，如何让他们不需要修改 Agent 核心代码？

这就是 **MCP（Model Context Protocol）** 要解决的问题：
- **标准化协议**：所有外部工具都遵循统一的接口规范
- **插件化架构**：通过配置文件动态加载外部服务，无需修改代码
- **进程隔离**：MCP Server 运行在独立进程中，崩溃不会影响 Agent 主进程

---

## 核心架构：三层适配体系

```
┌─────────────────────────────────────────────────────┐
│                   Agent 主循环                       │
│            (mewcode/agent/loop.py)                  │
└────────────────┬────────────────────────────────────┘
                 │ 调用 tool.execute()
                 ▼
┌─────────────────────────────────────────────────────┐
│              MCPToolWrapper (适配层)                 │
│          (mewcode/mcp/tool_wrapper.py)              │
│  • 名称适配: mcp_{server}_{tool}                    │
│  • Schema 转换: JSON Schema → Pydantic Model        │
│  • 结果提取: MCP Response → ToolResult              │
└────────────────┬────────────────────────────────────┘
                 │ 通过 client.call_tool()
                 ▼
┌─────────────────────────────────────────────────────┐
│               MCPClient (通信层)                     │
│            (mewcode/mcp/client.py)                  │
│  • Stdio 模式: 启动子进程，stdin/stdout 通信        │
│  • HTTP 模式: 建立 HTTP 长连接，SSE 流式传输        │
│  • 生命周期: connect/reconnect/close                │
└────────────────┬────────────────────────────────────┘
                 │ MCP 协议通信
                 ▼
┌─────────────────────────────────────────────────────┐
│           外部 MCP Server (第三方服务)               │
│  • Notion API                                       │
│  • PostgreSQL 数据库                                 │
│  • Filesystem 操作                                   │
│  • ... (任何实现了 MCP 协议的服务)                   │
└─────────────────────────────────────────────────────┘
```

---

## 第一层：MCPClient — 跨进程通信的桥梁

### 核心职责

`MCPClient` 负责建立和维护与外部 MCP Server 的连接，支持两种通信模式：

**1. Stdio 模式（进程间通信）**
```python
# 配置示例 (config.yaml)
mcp_servers:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/projects"]
    env:
      NODE_ENV: production
```

**工作流程：**
```python
# client.py:57-71
async def _connect_stdio(self) -> tuple[Any, Any]:
    params = StdioServerParameters(
        command=self.config.command,      # "npx"
        args=self.config.args,            # ["-y", "@modelcontextprotocol/..."]
        env=build_child_env(self.config.env),
    )
    devnull = open(os.devnull, "w")
    self._stack.callback(devnull.close)
    read, write = await self._stack.enter_async_context(
        stdio_client(params, errlog=devnull)
    )
    return read, write
```

**关键设计点：**
- **子进程管理**：通过 `stdio_client` 启动外部进程
- **环境变量注入**：`build_child_env()` 合并父进程 PATH 和用户配置
- **错误日志重定向**：stderr 输出到 `/dev/null`，避免污染主进程日志

**2. HTTP 模式（远程服务）**
```python
# 配置示例
mcp_servers:
  - name: remote_api
    url: https://api.example.com/mcp
    headers:
      Authorization: "Bearer ${API_TOKEN}"  # 支持环境变量替换
```

**工作流程：**
```python
# client.py:73-90
async def _connect_http(self) -> tuple[Any, Any]:
    resolved_headers = {
        k: resolve_env_vars(v) for k, v in self.config.headers.items()
    }
    http_client = httpx.AsyncClient(
        headers=resolved_headers,
        follow_redirects=True,
    )
    await self._stack.enter_async_context(http_client)
    
    result = await self._stack.enter_async_context(
        streamable_http_client(self.config.url, http_client=http_client)
    )
    return result[0], result[1]
```

**关键设计点：**
- **环境变量替换**：`${API_TOKEN}` 从环境变量读取敏感信息
- **长连接管理**：使用 `streamable_http_client` 保持连接活跃
- **自动重定向**：`follow_redirects=True` 处理 API 端点迁移

### 生命周期管理

**连接建立（connect）：**
```python
# client.py:32-54
async def connect(self) -> None:
    if self._alive:
        return
    
    self._stack = AsyncExitStack()
    await self._stack.__aenter__()
    
    try:
        if self.config.is_stdio:
            read, write = await self._connect_stdio()
        else:
            read, write = await self._connect_http()
        
        session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        await session.initialize()
        self._session = session
        self._alive = True
        logger.info("MCP server '%s' connected", self.name)
    except Exception:
        await self._cleanup_stack()
        raise
```

**关键技术：**
- **AsyncExitStack**：管理多个异步上下文管理器（子进程、HTTP 连接、Session）
- **懒初始化**：`if self._alive: return` 避免重复连接
- **异常安全**：连接失败时自动清理已分配资源

**健康检查与重连：**
```python
# manager.py:59-66
if not client.is_alive:
    logger.info("Reconnecting MCP server '%s'", name)
    await client.close()
    client = MCPClient(self._configs[name])
    await client.connect()
    self._clients[name] = client
```

**清理资源：**
```python
# client.py:110-121
async def _cleanup_stack(self) -> None:
    if self._stack is not None:
        try:
            await self._stack.__aexit__(None, None, None)
        except RuntimeError as e:
            if "cancel scope" in str(e):
                logger.debug("Cancel scope cleanup (expected during shutdown): %s", e)
            else:
                raise
        except Exception:
            logger.debug("Error closing stack for '%s'", self.name, exc_info=True)
        self._stack = None
```

**为什么捕获 `cancel scope` 错误？**
- AsyncIO 在 shutdown 时可能取消正在运行的任务
- `cancel scope` 错误是预期行为，记录 debug 日志即可
- 其他异常说明资源清理失败，需要抛出

---

## 第二层：MCPToolWrapper — Schema 转换与结果适配

### 核心职责

将外部 MCP Server 的工具定义（JSON Schema）转换为 Agent 可识别的 Pydantic Model。

### JSON Schema → Pydantic Model 动态构建

**问题场景：**

MCP Server 返回的工具定义是运行时动态的：
```json
{
  "name": "read_file",
  "description": "读取文件内容",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "文件路径"},
      "encoding": {"type": "string", "default": "utf-8"}
    },
    "required": ["path"]
  }
}
```

**如何在 Python 中校验参数？**

传统方式需要手写 Pydantic Model：
```python
class ReadFileParams(BaseModel):
    path: str
    encoding: str | None = "utf-8"
```

但 MCP Server 有几十个工具，每个都要手写吗？**不可能！**

**解决方案：动态构建 Pydantic Model**

```python
# tool_wrapper.py:12-26
def _build_params_model(
    tool_name: str, input_schema: dict[str, Any]
) -> type[BaseModel]:
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))
    
    field_definitions: dict[str, Any] = {}
    for name, prop in properties.items():
        py_type = _json_type_to_python(prop.get("type", "string"))
        if name in required:
            field_definitions[name] = (py_type, ...)  # 必填字段
        else:
            field_definitions[name] = (py_type | None, None)  # 可选字段
    
    return create_model(f"{tool_name}Params", **field_definitions)
```

**类型映射表：**
```python
# tool_wrapper.py:29-38
def _json_type_to_python(json_type: str) -> type:
    mapping: dict[str, type] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    return mapping.get(json_type, str)
```

**实际效果：**
```python
# 运行时动态生成
ReadFileParams = create_model(
    "read_fileParams",
    path=(str, ...),              # 必填
    encoding=(str | None, None)   # 可选
)

# Pydantic 自动校验参数
params = ReadFileParams(path="/tmp/test.txt")  # ✅
params = ReadFileParams()  # ❌ ValidationError: path field required
```

### MCP Response 结果提取

**问题：MCP Server 返回的内容格式复杂**

```python
# MCP Response 可能包含多种类型
result.content = [
    TextContent(type="text", text="文件内容..."),
    ImageContent(type="image", data="base64...", mimeType="image/png"),
    EmbeddedResource(resource=Resource(uri="file:///tmp/data.json", text="..."))
]
```

**解决方案：统一提取为文本**

```python
# tool_wrapper.py:41-54
def _extract_text(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        elif isinstance(block, mcp_types.ImageContent):
            parts.append(f"[image: {block.mimeType}]")
        elif isinstance(block, mcp_types.EmbeddedResource):
            resource = block.resource
            if hasattr(resource, "text"):
                parts.append(resource.text)
            else:
                parts.append(f"[binary resource: {resource.uri}]")
    return "\n".join(parts) if parts else "(no output)"
```

**设计要点：**
- **文本优先**：直接提取 `TextContent.text`
- **图片占位符**：`[image: image/png]` 告诉 LLM 有图片存在
- **资源嵌入**：优先提取 `resource.text`，否则显示 URI
- **空结果处理**：返回 `"(no output)"` 避免 LLM 困惑

### 工具名称规范化

**问题：避免命名冲突**

如果两个 MCP Server 都有 `read_file` 工具怎么办？

**解决方案：添加 server 前缀**

```python
# tool_wrapper.py:67
self.name = f"mcp_{server_name}_{tool_def.name}"

# 示例
# Server: filesystem, Tool: read_file  → mcp_filesystem_read_file
# Server: notion, Tool: read_file      → mcp_notion_read_file
```

### 工具执行流程

```python
# tool_wrapper.py:89-111
async def execute(self, params: BaseModel) -> ToolResult:
    # 1. 检查连接健康
    if not self._client.is_alive:
        try:
            await self._client.connect()
        except Exception as e:
            return ToolResult(
                output=f"MCP server '{self._server_name}' reconnect failed: {e}",
                is_error=True,
            )
    
    # 2. 调用 MCP Server
    try:
        result = await self._client.call_tool(
            self._tool_def.name, params.model_dump(exclude_none=True)
        )
    except Exception as e:
        self._client._alive = False  # 标记连接断开
        return ToolResult(
            output=f"MCP tool call failed: {e}",
            is_error=True,
        )
    
    # 3. 提取结果
    text = _extract_text(result.content)
    return ToolResult(output=text, is_error=bool(result.isError))
```

**关键设计：**
- **自动重连**：连接断开时尝试恢复
- **错误传播**：调用失败标记 `_alive = False`，下次触发重连
- **参数清理**：`model_dump(exclude_none=True)` 去除空值，符合 MCP 规范

---

## 第三层：MCPManager — 全局协调者

### 核心职责

MCPManager 负责管理所有 MCP Server 的生命周期，并将它们的工具批量注册到 Agent 的工具注册表。

### 启动流程：批量注册工具

```python
# manager.py:26-45
async def register_all_tools(self, registry: ToolRegistry) -> list[str]:
    errors: list[str] = []
    for name, config in self._configs.items():
        try:
            # 1. 创建并连接 Client
            client = MCPClient(config)
            await client.connect()
            self._clients[name] = client
            
            # 2. 拉取工具列表
            tools = await client.list_tools()
            
            # 3. 为每个工具创建 Wrapper 并注册
            for tool_def in tools:
                wrapper = MCPToolWrapper(name, tool_def, client)
                registry.register(wrapper)
                logger.info("Registered MCP tool: %s", wrapper.name)
        
        except Exception as e:
            msg = f"MCP server '{name}': {e}"
            logger.warning(msg)
            errors.append(msg)
    
    return errors
```

**设计要点：**
- **容错机制**：某个 Server 启动失败不影响其他 Server
- **错误收集**：返回所有失败信息，启动时展示给用户
- **日志记录**：每个工具注册成功都记录日志，便于调试

### 按需获取 Client（懒加载 + 重连）

```python
# manager.py:48-66
async def get_client(self, name: str) -> MCPClient | None:
    client = self._clients.get(name)
    
    # 1. Client 不存在，首次创建
    if client is None:
        config = self._configs.get(name)
        if config is None:
            return None
        client = MCPClient(config)
        await client.connect()
        self._clients[name] = client
        return client
    
    # 2. Client 已死，重新连接
    if not client.is_alive:
        logger.info("Reconnecting MCP server '%s'", name)
        await client.close()
        client = MCPClient(self._configs[name])
        await client.connect()
        self._clients[name] = client
    
    return client
```

**为什么需要 `get_client`？**
- **懒加载**：不是所有 Server 都在启动时连接（可能有几十个）
- **重连逻辑**：集中管理连接恢复，避免在 Wrapper 中重复代码
- **资源复用**：同一个 Server 的多个工具共享一个 Client

### 优雅关闭

```python
# manager.py:69-76
async def shutdown(self) -> None:
    for name, client in self._clients.items():
        try:
            await client.close()
            logger.info("MCP server '%s' closed", name)
        except Exception:
            logger.debug("Error closing MCP server '%s'", name, exc_info=True)
    self._clients.clear()
```

**设计要点：**
- **逐个关闭**：即使某个 Server 关闭失败，也继续关闭其他 Server
- **清理引用**：`_clients.clear()` 避免内存泄漏
- **降级日志**：关闭失败只记录 debug，避免 shutdown 时日志爆炸

---

## 配置加载：YAML 到对象

### 配置文件格式

```yaml
mcp_servers:
  # Stdio 模式
  - name: filesystem
    command: npx
    args:
      - "-y"
      - "@modelcontextprotocol/server-filesystem"
      - "/Users/me/projects"
    env:
      NODE_ENV: production
  
  # HTTP 模式
  - name: remote_api
    url: https://api.example.com/mcp
    headers:
      Authorization: "Bearer ${API_TOKEN}"
```

### 配置对象定义

```python
# config.py:105-117
@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    
    @property
    def is_stdio(self) -> bool:
        return self.command is not None
```

**判断连接模式：**
- `command is not None` → Stdio 模式
- `url is not None` → HTTP 模式

### 环境变量替换

**问题：不想在配置文件中明文写 API Key**

```yaml
headers:
  Authorization: "Bearer ${API_TOKEN}"  # 从环境变量读取
```

**实现：正则替换**

```python
# config.py:27, 91-92
_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")

def resolve_env_vars(value: str) -> str:
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
```

**匹配逻辑：**
```python
# 正则：\$\{([^}]+)\}
# ${API_TOKEN}  → 提取 "API_TOKEN"
# ${PATH}       → 提取 "PATH"
# 如果环境变量不存在，保留原字符串 "${API_TOKEN}"
```

### 子进程环境变量构建

**问题：Node.js MCP Server 需要继承 PATH**

```python
# config.py:95-102
def build_child_env(declared_env: dict[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    
    # 1. 继承父进程 PATH
    path = os.environ.get("PATH", "")
    if path:
        env["PATH"] = path
    
    # 2. 合并用户配置（支持环境变量替换）
    for key, value in (declared_env or {}).items():
        env[key] = resolve_env_vars(value)
    
    return env
```

**为什么需要继承 PATH？**
- MCP Server 通常用 `npx`、`python`、`node` 启动
- 不继承 PATH 会导致 `command not found`

---

## 实战案例：接入 Filesystem MCP Server

### 第一步：安装 MCP Server

```bash
npm install -g @modelcontextprotocol/server-filesystem
```

### 第二步：配置 config.yaml

```yaml
mcp_servers:
  - name: fs
    command: npx
    args:
      - "-y"
      - "@modelcontextprotocol/server-filesystem"
      - "/Users/me/projects"  # 允许访问的根目录
```

### 第三步：启动 Agent

```python
# Agent 启动时自动加载
async def main():
    config = load_config()
    manager = MCPManager()
    manager.load_configs(config.mcp_servers)
    
    registry = ToolRegistry()
    errors = await manager.register_all_tools(registry)
    
    if errors:
        for err in errors:
            print(f"Warning: {err}")
    
    print("Available tools:")
    for tool_name in registry.list_tools():
        print(f"  - {tool_name}")
```

**输出：**
```
Registered MCP tool: mcp_fs_read_file
Registered MCP tool: mcp_fs_write_file
Registered MCP tool: mcp_fs_list_directory
Available tools:
  - mcp_fs_read_file
  - mcp_fs_write_file
  - mcp_fs_list_directory
```

### 第四步：LLM 调用工具

**LLM 返回：**
```json
{
  "name": "mcp_fs_read_file",
  "arguments": {
    "path": "/Users/me/projects/README.md"
  }
}
```

**执行流程：**
```
1. ToolRegistry 找到 MCPToolWrapper("mcp_fs_read_file")
2. Pydantic 校验参数 → ReadFileParams(path="/Users/me/projects/README.md")
3. MCPClient.call_tool("read_file", {"path": "..."})
4. 子进程通过 stdin 发送 JSON-RPC 请求
5. MCP Server 读取文件，通过 stdout 返回结果
6. MCPClient 解析响应
7. MCPToolWrapper 提取文本内容
8. 返回 ToolResult(output="# My Project\n...")
```

---

## 设计亮点总结

### 1. 插件化架构

**传统方式：**
```python
# 每增加一个系统都要改代码
class NotionTool(Tool):
    async def execute(self, params): ...

class PostgreSQLTool(Tool):
    async def execute(self, params): ...

registry.register(NotionTool())
registry.register(PostgreSQLTool())
```

**MCP 方式：**
```yaml
# 只需修改配置文件
mcp_servers:
  - name: notion
    command: notion-mcp-server
  - name: postgres
    command: postgres-mcp-server
```

### 2. 进程隔离

**好处：**
- **崩溃隔离**：MCP Server 崩溃不影响 Agent 主进程
- **语言无关**：Server 可以用 Node.js/Python/Rust 任意实现
- **权限隔离**：子进程只能访问配置的目录

### 3. 动态 Schema 构建

**避免手写几十个 Pydantic Model：**
```python
# 一行代码搞定所有工具的参数校验
params_model = create_model(f"{tool_name}Params", **field_definitions)
```

### 4. 自动重连机制

**网络不稳定时自动恢复：**
```python
if not client.is_alive:
    await client.connect()  # 透明重连
```

### 5. 优雅降级

**某个 Server 启动失败不影响其他：**
```python
try:
    client = MCPClient(config)
    await client.connect()
except Exception as e:
    errors.append(f"MCP server '{name}': {e}")
    # 继续加载其他 Server
```

---

## 延伸思考

### 问题 1：如何防止 MCP Server 恶意操作？

**当前设计：**
- Path Sandbox 只检查内置工具（ReadFile/WriteFile）
- MCP Server 可以绕过沙箱直接操作文件系统

**解决方案：**
```python
# 在 MCPToolWrapper 中添加沙箱检查
async def execute(self, params: BaseModel) -> ToolResult:
    # 提取文件路径参数
    if "path" in params.model_dump():
        path = params.model_dump()["path"]
        allowed, msg = self._sandbox.check(path)
        if not allowed:
            return ToolResult(output=msg, is_error=True)
    
    # 调用 MCP Server
    result = await self._client.call_tool(...)
```

### 问题 2：如何支持流式响应？

**当前设计：**
- `call_tool()` 等待完整结果后返回
- 无法实时展示 MCP Server 的进度

**解决方案：**
```python
# MCP 协议支持 Server-Sent Events (SSE)
async for chunk in client.call_tool_stream(tool_name, arguments):
    if chunk.type == "progress":
        print(f"Progress: {chunk.progress}%")
    elif chunk.type == "result":
        return chunk.data
```

### 问题 3：如何实现工具权限分级？

**场景：**
- 某些 MCP Server 的工具很危险（如 `execute_sql`）
- 希望标记为高风险，强制 HITL 确认

**解决方案：**
```python
# tool_wrapper.py
class MCPToolWrapper(Tool):
    def __init__(self, server_name: str, tool_def: mcp_types.Tool, client: MCPClient):
        self.category = self._infer_category(tool_def)
    
    def _infer_category(self, tool_def: mcp_types.Tool) -> str:
        dangerous_keywords = ["execute", "delete", "drop", "truncate"]
        if any(kw in tool_def.name.lower() for kw in dangerous_keywords):
            return "command"  # 高风险
        return "read"  # 低风险
```

---

## 关键代码位置速查

| 文件 | 核心功能 | 关键代码行 |
|------|---------|-----------|
| `mcp/client.py` | 建立连接 | `connect()` (32-54) |
| `mcp/client.py` | Stdio 模式 | `_connect_stdio()` (57-71) |
| `mcp/client.py` | HTTP 模式 | `_connect_http()` (73-90) |
| `mcp/client.py` | 资源清理 | `_cleanup_stack()` (110-121) |
| `mcp/tool_wrapper.py` | Schema 构建 | `_build_params_model()` (12-26) |
| `mcp/tool_wrapper.py` | 结果提取 | `_extract_text()` (41-54) |
| `mcp/tool_wrapper.py` | 工具执行 | `execute()` (89-111) |
| `mcp/manager.py` | 批量注册 | `register_all_tools()` (26-45) |
| `mcp/manager.py` | 按需获取 | `get_client()` (48-66) |
| `config.py` | 配置定义 | `MCPServerConfig` (105-117) |
| `config.py` | 环境变量替换 | `resolve_env_vars()` (91-92) |

---

**下一章预告：CH07 对话管理与上下文压缩**

当对话越来越长，Token 成本如何控制？如何在保留关键信息的同时压缩上下文？我们将拆解 MewCode 的对话管理策略。
