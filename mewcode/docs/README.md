# MewCode 技术文档

MewCode是一个基于Claude的AI编程助手框架，支持工具调用、Hook系统、SubAgent并行执行、Worktree隔离等高级特性。

## 文档目录

### 基础架构（CH01-CH05）

- **CH01: LLM客户端与流式响应** (21KB)
  - Claude API客户端封装
  - 流式响应处理
  - Token统计与管理

- **CH02: 工具注册与执行框架** (30KB)
  - ToolRegistry工具注册表
  - 延迟工具加载机制（ToolSearch）
  - 工具参数验证与执行

- **CH03: Agent主循环与事件流** (26KB)
  - Agent.run_to_completion()主循环
  - LLM调用与工具执行流程
  - 事件触发与Hook集成

- **CH04: 系统提示词组装管理** (29KB)
  - SystemPromptBuilder动态组装
  - 模块化提示词管理
  - 上下文注入机制

- **CH05: 五层权限防御链** (34KB)
  - PermissionChecker权限检查
  - PathSandbox路径沙箱
  - DangerousCommandDetector危险命令检测
  - RuleEngine规则引擎
  - 五层防御机制

### 高级特性（CH06-CH10）

- **CH06: MCP协议与工具适配** (24KB)
  - Model Context Protocol集成
  - MCP服务器通信
  - 工具适配器实现

- **CH07: 对话管理与上下文压缩** (50KB)
  - ConversationManager对话管理
  - 上下文窗口管理
  - 消息压缩策略

- **CH08: 会话持久化与记忆提取** (45KB)
  - 会话状态持久化
  - Memory系统实现
  - 长期记忆管理

- **CH09: 命令注册与分发** (39KB)
  - CommandRegistry命令注册
  - Slash命令处理
  - 命令参数解析

- **CH10: Skill系统 - 可复用的技能包** (37KB)
  - Skill定义与加载
  - 技能参数传递
  - 技能组合与复用

### 核心系统（CH11-CH13）

- **CH11: Hook System - 事件驱动架构** (50KB)
  - 15种生命周期事件
  - Condition条件系统（4种操作符）
  - Action执行器（command/prompt/http/agent）
  - Hook配置加载与验证
  - HookEngine执行引擎
  - 真实案例：自动化工作流

- **CH12: SubAgent - 子Agent与任务分发** (103KB + 29KB补充)
  - **四种执行模式**：
    - 同步执行（Sync）- 阻塞等待结果
    - 异步执行（Background）- 后台并行
    - Fork继承（Fork）- 复制父Agent上下文
    - Worktree隔离（Worktree）- 独立工作树
  - **核心代码执行流程**（15步）：
    1. AgentTool.execute() - 工具入口
    2. 加载AgentDef定义
    3. 选择LLM客户端
    4. 判断执行模式（sync/async）
    5. 过滤工具注册表
    6. 创建权限检查器
    7. 创建SubAgent实例
    8. Fork特殊处理（replacement_state）
    9. 注册追踪节点（TraceManager）
    10. 启动后台任务（TaskManager.launch）
    11. 创建异步任务（asyncio.create_task）
    12. 后台执行（_run_background）
    13. SubAgent主循环（run_to_completion）
    14. 完成并通知（notify_queue.put）
    15. 主Agent收到通知（poll_completed）
  - **关键技术**：
    - Fork上下文继承（build_forked_messages）
    - Prompt Cache共享（replacement_state）
    - 工具过滤三层规则（白名单/黑名单/后台禁用）
    - 异步通知零延迟（asyncio.Queue）
  - **性能优化**：
    - 并行执行：10倍速度提升
    - Prompt Cache复用：节省8000+ tokens
    - 异步通知：零延迟
  - **真实案例**：
    - 并行代码审查（10个模块并行，5分钟完成）
    - 多方案对比试验（3个worktree并行试验）
    - 团队协作开发（前后端分离开发）

- **CH13: Worktree - Git工作树并行开发** (36KB)
  - **Git Worktree原理**：
    - 共享仓库：所有worktree共享.git目录
    - 独立分支：每个worktree可checkout不同分支
    - 物理隔离：文件修改互不影响
  - **WorktreeManager生命周期**：
    - create() - 创建worktree
    - auto_cleanup() - 智能清理
    - remove() - 删除worktree
  - **快速恢复优化**：
    - read_worktree_head_sha() - 记录创建时commit
    - 避免每次执行git命令
  - **Post-Creation设置**：
    - 复制本地配置（.env, settings.local.json）
    - 设置Git hooks路径（支持Husky）
    - 创建symlinks（node_modules等）
    - 复制.gitignore中的特定文件
  - **Auto Cleanup逻辑**：
    - 检测未提交文件（git status --porcelain）
    - 检测新commit（git rev-list --count）
    - 保守策略：出错时保留worktree
  - **性能优化**：
    - Symlink：节省300秒npm install时间
    - 并行创建：3倍加速
  - **真实案例**：
    - 并行试验3种缓存方案
    - 紧急Bug修复不打断开发

## 文档统计

| 章节 | 主题 | 大小 | 状态 |
|------|------|------|------|
| CH01 | LLM客户端 | 21KB | ✅ 已完成 |
| CH02 | 工具框架 | 30KB | ✅ 已完成 |
| CH03 | Agent主循环 | 26KB | ✅ 已完成 |
| CH04 | 系统提示词 | 29KB | ✅ 已完成 |
| CH05 | 权限防御 | 34KB | ✅ 已完成 |
| CH06 | MCP协议 | 24KB | ✅ 已完成 |
| CH07 | 对话管理 | 50KB | ✅ 已完成 |
| CH08 | 会话持久化 | 45KB | ✅ 已完成 |
| CH09 | 命令分发 | 39KB | ✅ 已完成 |
| CH10 | Skill系统 | 37KB | ✅ 已完成 |
| CH11 | Hook系统 | 50KB | ✅ 已完成 |
| CH12 | SubAgent | 132KB | ✅ 已完成 |
| CH13 | Worktree | 36KB | ✅ 已完成 |
| **总计** | | **553KB** | **13章全部完成** |

## 核心亮点

### 1. 延迟工具加载（CH02）
- AI主动调用ToolSearch发现新工具
- 系统提示提醒可用的延迟工具
- 按需加载，避免初始化开销

### 2. Hook事件驱动（CH11）
- 15种生命周期事件
- 条件表达式（==, !=, =~, ~=）
- 4种执行器（command, prompt, http, agent）
- 灵活的自动化工作流

### 3. SubAgent并行执行（CH12）
- 4种执行模式适配不同场景
- Fork继承上下文，Prompt Cache复用
- 异步通知零延迟
- 10倍性能提升

### 4. Worktree隔离开发（CH13）
- Git原生worktree支持
- 智能清理避免误删
- Symlink优化节省时间和空间
- 物理隔离保证安全试验

## 架构设计模式

### 1. 策略模式（SubAgent执行模式）
```python
if p.team_name:
    return await self._execute_as_teammate(p)
elif isolation == "worktree":
    return await self._execute_with_worktree(p)
else:
    return await self._execute_normal(p)
```

### 2. 观察者模式（任务通知）
```python
# 生产者
await self._notify_queue.put(task_id)

# 消费者
completed_tasks = self._task_manager.poll_completed()
```

### 3. 组合模式（Agent树）
```python
TraceNode(
    agent_id="parent",
    children=[
        TraceNode(agent_id="child1"),
        TraceNode(agent_id="child2"),
    ]
)
```

### 4. 建造者模式（SubAgent构建）
```python
# Step 1: 选择LLM
client = self._select_llm(p, definition)

# Step 2: 过滤工具
registry = resolve_agent_tools(base_registry, definition, is_background)

# Step 3: 创建权限检查器
checker = PermissionChecker(...)

# Step 4: 组装SubAgent
sub_agent = AgentClass(client, registry, checker, ...)
```

## 性能优化技巧

### 1. Prompt Cache复用
- Fork继承父Agent上下文
- 共享replacement_state
- 节省8000+ tokens计算

### 2. 并行执行
- asyncio.create_task()异步并发
- TaskManager统一管理
- 10倍速度提升

### 3. Symlink优化
- node_modules等大目录使用symlink
- 节省300秒npm install时间
- 节省500MB+磁盘空间

### 4. 零延迟通知
- asyncio.Queue推送通知
- poll_completed()批量获取
- 避免轮询浪费CPU

## 常见陷阱与解决方案

### 1. Fork嵌套导致上下文爆炸
**问题**：Fork的Fork导致上下文指数增长
**解决**：反嵌套检查，禁止fork from fork

### 2. 后台任务启动SubAgent
**问题**：后台SubAgent又启动SubAgent，无限递归
**解决**：后台模式禁用Agent工具

### 3. Worktree路径混淆
**问题**：SubAgent在worktree中访问主工作区文件
**解决**：PathSandbox限制访问范围

### 4. Auto Cleanup误删
**问题**：git命令出错导致误删有变更的worktree
**解决**：保守策略，出错时保留

## 最佳实践

### 何时使用SubAgent？
- ✅ 并行研究（多个独立任务）
- ✅ 隔离实验（试验性修改）
- ✅ 专业分工（不同类型的工作）
- ✅ 团队协作（长期运行的成员）

### 何时使用Worktree？
- ✅ 试验性修改（不确定是否采用）
- ✅ 大规模重构（怕搞乱主工作区）
- ✅ 并行对比（多个方案同时试验）
- ❌ 确定的修改（直接在主工作区）
- ❌ 只读任务（不需要隔离）

### 何时使用Hook？
- ✅ 自动化工作流（提交前检查）
- ✅ 条件触发（特定事件执行）
- ✅ 集成外部工具（CI/CD）
- ❌ 简单的一次性任务

## 下一步

如果你想深入了解MewCode的实现细节，建议按以下顺序阅读：

1. **入门**：CH01-CH05（基础架构）
2. **进阶**：CH06-CH10（高级特性）
3. **核心**：CH11-CH13（Hook/SubAgent/Worktree）

每章都包含：
- 真实场景引入
- 完整源码剖析
- 流程图与示例
- 关键技术点详解
- 常见陷阱与解决方案
- 真实案例分析
- 最佳实践建议

---

*文档版本：2024-12*
*总字数：553KB*
*完成度：13/13章 (100%)*
