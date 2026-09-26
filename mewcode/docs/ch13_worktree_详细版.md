# CH13: Worktree - Git工作树并行开发

## 从一个真实场景说起

你正在开发一个功能，突然产品经理说："紧急Bug！用户登录失败了，赶紧修！"

**传统做法**：

```bash
# 1. 保存当前工作
git stash

# 2. 切换分支修Bug
git checkout main
git checkout -b hotfix/login-bug

# 3. 修复完成后
git add .
git commit -m "Fix login bug"
git checkout feature/new-feature

# 4. 恢复之前的工作
git stash pop

# 问题：
# - stash可能冲突
# - 切换分支耗时
# - 工作区混乱
# - 容易出错
```

**使用Worktree**：

```bash
# 1. 创建新的worktree修Bug（不影响当前工作）
git worktree add ../hotfix-worktree -b hotfix/login-bug

# 2. 在新worktree中修复
cd ../hotfix-worktree
# 修复Bug...
git commit -m "Fix login bug"

# 3. 回到原工作区，继续开发
cd ../main-project
# 你的feature分支完全没有被打断！

# 4. Bug修完后删除worktree
git worktree remove ../hotfix-worktree
```

**Worktree的优势**：
- **并行开发**：同一个仓库，多个工作目录，互不干扰
- **零切换成本**：不需要stash、不需要切换分支
- **物理隔离**：每个worktree有独立的文件系统
- **共享仓库**：共享.git目录，节省空间

这就是**Worktree系统**要解决的问题：**在同一个仓库中并行开发多个任务，互不干扰**。

---

## MewCode的Worktree系统

### 什么是Git Worktree？

Git Worktree是Git的原生功能（Git 2.5+），允许一个仓库有多个工作目录：

```
main-project/
├── .git/              # 主仓库（shared）
├── src/               # 主工作区（main分支）
├── .claude/
│   └── worktrees/
│       ├── experiment-1/   # Worktree 1（branch: wt/experiment-1）
│       │   ├── src/        # 独立的文件副本
│       │   └── .git        # 指向主仓库的链接
│       └── experiment-2/   # Worktree 2（branch: wt/experiment-2）
│           ├── src/
│           └── .git
```

**关键特性**：
1. **共享仓库**：所有worktree共享同一个.git目录
2. **独立分支**：每个worktree可以checkout不同的分支
3. **物理隔离**：文件修改互不影响
4. **高效**：不需要复制整个仓库

---

## MewCode Worktree的核心组件

### 1. 数据模型

**文件**: `mewcode/worktree/models.py`

```python
from dataclasses import dataclass

@dataclass
class Worktree:
    """
    Worktree实例
    """
    name: str              # "experiment-redis"
    path: str              # ".claude/worktrees/experiment-redis"
    branch: str            # "wt/experiment-redis"
    head_commit: str       # 创建时的commit SHA
    base_ref: str          # 基于哪个分支创建（通常是"HEAD"）


@dataclass
class WorktreeSession:
    """
    Worktree会话状态（用于EnterWorktree/ExitWorktree）
    """
    original_cwd: str           # 原始工作目录
    worktree_path: str          # worktree路径
    worktree_name: str          # worktree名称
    original_branch: str        # 原始分支
    original_head_commit: str   # 原始commit
    session_id: str             # 会话ID
    hook_based: bool = False    # 是否通过hook创建
```

**关键点**：
- `Worktree`：描述一个worktree实例
- `WorktreeSession`：记录进入worktree前的状态，用于退出时恢复

---

### 2. WorktreeManager - 核心管理器

**文件**: `mewcode/worktree/manager.py`

```python
class WorktreeManager:
    """
    Worktree生命周期管理
    
    职责：
    1. 创建worktree（create）
    2. 进入worktree（enter）
    3. 退出worktree（exit）
    4. 自动清理（auto_cleanup）
    5. 快速恢复优化（read_worktree_head_sha）
    """
    
    def __init__(self, repo_root: str, mewcode_dir: str):
        self._repo_root = repo_root
        self._mewcode_dir = Path(mewcode_dir)
        self._worktrees: dict[str, Worktree] = {}
        self._worktree_base = Path(repo_root) / ".claude" / "worktrees"
```

**核心方法**：

#### create() - 创建worktree

```python
async def create(self, name: str, base_ref: str = "HEAD") -> Worktree:
    """
    创建一个新的worktree
    
    参数：
        name: worktree名称（如"experiment-redis"）
        base_ref: 基于哪个分支/commit创建（默认"HEAD"）
    
    返回：
        Worktree对象
    
    流程：
        1. 验证name格式
        2. 生成worktree路径和分支名
        3. 读取当前HEAD commit（快速恢复优化）
        4. 执行git worktree add
        5. 执行post-creation设置
        6. 保存worktree信息
    """
    # 1. 验证name
    err = validate_slug(name)
    if err:
        raise ValueError(f"Invalid worktree name: {err}")
    
    # 2. 生成路径
    flattened = flatten_slug(name)  # "a/b" -> "a+b"
    wt_path = self._worktree_base / flattened
    branch_name = f"wt/{name}"
    
    if wt_path.exists():
        raise ValueError(f"Worktree already exists at {wt_path}")
    
    # 3. 读取HEAD commit（快速恢复优化）
    head_commit = self.read_worktree_head_sha(base_ref)
    
    # 4. 执行git worktree add
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "git", "worktree", "add",
                str(wt_path),
                "-b", branch_name,
                base_ref
            ],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr}")
    
    except subprocess.TimeoutExpired:
        raise RuntimeError("git worktree add timed out")
    
    # 5. Post-creation设置
    perform_post_creation_setup(
        repo_root=self._repo_root,
        wt_path=str(wt_path),
        symlink_directories=["node_modules", "vendor"],  # 可配置
    )
    
    # 6. 保存worktree信息
    wt = Worktree(
        name=name,
        path=str(wt_path),
        branch=branch_name,
        head_commit=head_commit,
        base_ref=base_ref,
    )
    self._worktrees[name] = wt
    
    return wt
```

**关键点**：
- **validate_slug()**：验证name只包含字母、数字、点、破折号、下划线、斜杠
- **flatten_slug()**：将斜杠转换为加号（"a/b" → "a+b"），因为文件系统不允许斜杠
- **read_worktree_head_sha()**：快速恢复优化，记录创建时的commit
- **perform_post_creation_setup()**：复制本地配置、设置hooks、创建symlinks

---

#### read_worktree_head_sha() - 快速恢复优化

```python
def read_worktree_head_sha(self, ref: str = "HEAD") -> str:
    """
    读取指定ref的commit SHA
    
    为什么需要？
    ========
    auto_cleanup()需要判断worktree是否有变更。
    如果没有记录原始commit，需要对比HEAD和base_ref：
    
        git rev-list --count HEAD..origin/main
    
    但如果base_ref是"HEAD"，这个命令无意义。
    所以在创建时记录具体的commit SHA。
    
    优化：
    ======
    避免每次都执行git命令，提前读取并缓存。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        
        if result.returncode == 0:
            return result.stdout.strip()
        
        return ""
    
    except (subprocess.SubprocessError, OSError):
        return ""
```

**为什么重要？**

```python
# 场景：用户创建worktree做实验
wt = await manager.create("experiment", "HEAD")
# 记录head_commit = "abc123def456"

# 实验过程中，用户在worktree中提交了2个commit
# HEAD现在是"xyz789"

# auto_cleanup时判断是否有变更
new_commits = git rev-list --count abc123def456..HEAD
# 返回2，说明有新commit，保留worktree

# 如果没有记录abc123def456，无法判断
```

---

#### auto_cleanup() - 自动清理逻辑

```python
async def auto_cleanup(self, name: str, original_head: str) -> CleanupResult:
    """
    决策：保留还是删除worktree？
    
    判断依据：
        1. 有未提交的文件？ → 保留
        2. 有新的commit（相比original_head）？ → 保留
        3. 什么都没改？ → 删除
    
    返回：
        CleanupResult(kept=True/False, path="...", branch="...")
    """
    wt = self._worktrees.get(name)
    if wt is None:
        return CleanupResult(kept=False)
    
    # 检测变更
    has_changes = has_worktree_changes(wt.path, original_head)
    
    if has_changes:
        # 有变更，保留
        log.info("Worktree %s has changes, keeping it", name)
        return CleanupResult(
            kept=True,
            path=wt.path,
            branch=wt.branch,
        )
    else:
        # 无变更，删除
        log.info("Worktree %s has no changes, removing it", name)
        await self.remove(name)
        return CleanupResult(kept=False)
```

**has_worktree_changes()实现**：`worktree/changes.py`

```python
def has_worktree_changes(wt_path: str, head_commit: str) -> bool:
    """
    检测worktree是否有变更
    
    检测两种变更：
        1. 未提交的文件（git status --porcelain）
        2. 新的commit（git rev-list --count HEAD..原始commit）
    """
    c = count_worktree_changes(wt_path, head_commit)
    return c.uncommitted > 0 or c.new_commits > 0


def count_worktree_changes(wt_path: str, head_commit: str) -> Changes:
    """
    统计变更数量
    """
    changes = Changes()
    
    # 1. 检测未提交的文件
    try:
        status = _run_git(["status", "--porcelain"], cwd=wt_path)
        if status.returncode == 0:
            changes.uncommitted = len(
                [line for line in status.stdout.splitlines() if line.strip()]
            )
    except (subprocess.SubprocessError, OSError):
        changes.uncommitted = 1  # 出错视为有变更（保守策略）
    
    # 2. 检测新commit
    try:
        rev_list = _run_git(
            ["rev-list", "--count", f"{head_commit}..HEAD"],
            cwd=wt_path
        )
        if rev_list.returncode == 0:
            changes.new_commits = int(rev_list.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        changes.new_commits = 1  # 出错视为有变更
    
    return changes
```

**关键点**：
- `git status --porcelain`：检测未暂存/未提交的文件
- `git rev-list --count HEAD..原始commit`：检测新commit数量
- **保守策略**：出错时视为有变更，避免误删

---

#### remove() - 删除worktree

```python
async def remove(self, name: str) -> None:
    """
    删除worktree
    
    流程：
        1. 执行git worktree remove
        2. 从_worktrees字典移除
        3. 删除本地目录（如果git没删干净）
    """
    wt = self._worktrees.get(name)
    if wt is None:
        return
    
    try:
        # 1. git worktree remove
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "worktree", "remove", wt.path, "--force"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        
        if result.returncode != 0:
            log.warning("git worktree remove failed: %s", result.stderr)
    
    except subprocess.TimeoutExpired:
        log.warning("git worktree remove timed out")
    
    # 2. 从字典移除
    self._worktrees.pop(name, None)
    
    # 3. 强制删除目录（如果还存在）
    wt_path = Path(wt.path)
    if wt_path.exists():
        try:
            shutil.rmtree(wt_path)
        except OSError as e:
            log.warning("Failed to remove worktree directory: %s", e)
```

---

### 3. Post-Creation设置

**文件**: `mewcode/worktree/setup.py`

```python
def perform_post_creation_setup(
    repo_root: str,
    wt_path: str,
    symlink_directories: list[str] | None = None,
) -> None:
    """
    Worktree创建后的初始化设置
    
    步骤：
        1. 复制本地配置文件（.env, settings.local.json）
        2. 设置Git hooks路径
        3. 创建symlinks（如node_modules）
        4. 复制.gitignore中的特定文件
    """
    root = Path(repo_root)
    wt = Path(wt_path)

    _copy_local_configs(root, wt)
    _setup_git_hooks(root, wt)
    _create_symlinks(root, wt, symlink_directories or [])
    _copy_ignored_files(root, wt)
```

**为什么需要这些设置？**

#### 3.1 复制本地配置文件

```python
LOCAL_CONFIG_FILES = [
    "settings.local.json",
    ".env",
]

def _copy_local_configs(root: Path, wt: Path) -> None:
    """
    复制本地配置文件到worktree
    
    原因：
    ====
    .env和settings.local.json通常在.gitignore中，
    不会被git worktree复制。
    
    但worktree中运行代码需要这些配置，
    所以手动复制。
    """
    for name in LOCAL_CONFIG_FILES:
        src = root / name
        if src.exists():
            dst = wt / name
            try:
                shutil.copy2(str(src), str(dst))
                log.debug("Copied %s to worktree", name)
            except OSError as e:
                log.warning("Failed to copy %s: %s", name, e)
```

#### 3.2 设置Git Hooks路径

```python
def _setup_git_hooks(root: Path, wt: Path) -> None:
    """
    设置worktree的Git hooks路径
    
    原因：
    ====
    每个worktree有独立的.git文件（指向主仓库），
    默认不会执行hooks。
    
    需要手动配置core.hooksPath指向主仓库的hooks。
    
    支持：
    - Husky（.husky/目录）
    - 传统hooks（.git/hooks/）
    """
    hooks_path: str | None = None

    # 1. 检查Husky
    husky_dir = root / ".husky"
    if husky_dir.is_dir():
        hooks_path = str(husky_dir)
    else:
        # 2. 检查传统hooks
        git_hooks = root / ".git" / "hooks"
        if git_hooks.is_dir():
            hooks_path = str(git_hooks)

    if hooks_path is None:
        return

    # 3. 设置core.hooksPath
    try:
        subprocess.run(
            ["git", "config", "core.hooksPath", hooks_path],
            cwd=str(wt),
            capture_output=True,
            timeout=10,
        )
        log.debug("Set core.hooksPath to %s in worktree", hooks_path)
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("Failed to set hooks path: %s", e)
```

**为什么重要？**

```bash
# 主工作区有pre-commit hook（代码格式化）
.husky/pre-commit

# Worktree中提交代码
cd .claude/worktrees/experiment-1
git commit -m "test"

# 如果没有设置core.hooksPath：
# ✗ pre-commit hook不执行，代码未格式化

# 设置core.hooksPath后：
# ✓ pre-commit hook执行，代码被格式化
```

#### 3.3 创建Symlinks

```python
def _create_symlinks(root: Path, wt: Path, directories: list[str]) -> None:
    """
    创建目录symlinks
    
    典型用例：
    ========
    node_modules/：
        - 主工作区：500MB，npm install 5分钟
        - Worktree：symlink指向主工作区，秒级创建
    
    vendor/（PHP）：
        - 主工作区：200MB，composer install 3分钟
        - Worktree：symlink，秒级创建
    """
    for dirname in directories:
        src = root / dirname
        dst = wt / dirname
        
        # 检查源目录存在
        if not src.exists():
            continue
        
        # 检查目标不存在
        if dst.exists() or dst.is_symlink():
            continue
        
        # 创建symlink
        try:
            os.symlink(str(src), str(dst))
            log.debug("Symlinked %s to worktree", dirname)
        except OSError as e:
            log.warning("Failed to symlink %s: %s", dirname, e)
```

**为什么重要？**

```bash
# 不使用symlink：
cd .claude/worktrees/experiment-1
npm install  # 5分钟，500MB磁盘空间
npm install  # 又5分钟，又500MB

cd .claude/worktrees/experiment-2
npm install  # 再5分钟，再500MB
# 总计：15分钟，1.5GB磁盘

# 使用symlink：
cd .claude/worktrees/experiment-1
# node_modules/ -> ../../node_modules/（秒级）

cd .claude/worktrees/experiment-2
# node_modules/ -> ../../node_modules/（秒级）
# 总计：秒级，0额外磁盘空间
```

#### 3.4 复制.gitignore中的特定文件

```python
def _copy_ignored_files(root: Path, wt: Path) -> None:
    """
    复制.gitignore中的特定文件
    
    原因：
    ====
    某些文件在.gitignore中，但worktree需要。
    
    用户可以在.worktreeinclude文件中指定：
    
        # .worktreeinclude
        build/config.json
        dist/*.map
    
    这些文件会被复制到worktree。
    """
    include_file = root / ".worktreeinclude"
    if not include_file.exists():
        return

    # 1. 读取patterns
    try:
        patterns = [
            line.strip()
            for line in include_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except OSError:
        return

    if not patterns:
        return

    # 2. 获取所有ignored文件
    try:
        result = subprocess.run(
            [
                "git", "ls-files",
                "--others", "--ignored", "--exclude-standard", "--directory",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return
        ignored_files = [f.rstrip("/") for f in result.stdout.splitlines() if f.strip()]
    except (subprocess.SubprocessError, OSError):
        return

    # 3. 匹配patterns并复制
    for rel_path in ignored_files:
        if not any(fnmatch.fnmatch(rel_path, pat) for pat in patterns):
            continue
        
        src = root / rel_path
        dst = wt / rel_path
        
        if not src.is_file():
            continue
        
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            log.debug("Copied ignored file %s to worktree", rel_path)
        except OSError as e:
            log.warning("Failed to copy ignored file %s: %s", rel_path, e)
```

---

### 4. 会话管理

**文件**: `mewcode/worktree/session.py`

```python
SESSION_FILENAME = "worktree_session.json"

def save_worktree_session(
    mewcode_dir: Path,
    session: WorktreeSession | None,
) -> None:
    """
    保存worktree会话状态
    
    用于EnterWorktree/ExitWorktree功能：
    - EnterWorktree：记录原始状态
    - ExitWorktree：恢复原始状态
    """
    path = mewcode_dir / SESSION_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    
    if session is None:
        path.write_text("{}", encoding="utf-8")
        return
    
    data = {
        "original_cwd": session.original_cwd,
        "worktree_path": session.worktree_path,
        "worktree_name": session.worktree_name,
        "original_branch": session.original_branch,
        "original_head_commit": session.original_head_commit,
        "session_id": session.session_id,
        "hook_based": session.hook_based,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_worktree_session(mewcode_dir: Path) -> WorktreeSession | None:
    """
    加载worktree会话状态
    """
    path = mewcode_dir / SESSION_FILENAME
    if not path.exists():
        return None
    
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data or "worktree_path" not in data:
            return None
        
        return WorktreeSession(
            original_cwd=data["original_cwd"],
            worktree_path=data["worktree_path"],
            worktree_name=data["worktree_name"],
            original_branch=data["original_branch"],
            original_head_commit=data["original_head_commit"],
            session_id=data.get("session_id", ""),
            hook_based=data.get("hook_based", False),
        )
    except (json.JSONDecodeError, KeyError) as e:
        log.warning("Failed to load worktree session: %s", e)
        return None
```

---

### 5. 名称验证

**文件**: `mewcode/worktree/slug.py`

```python
MAX_SLUG_LENGTH = 64
_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

def validate_slug(name: str) -> str | None:
    """
    验证worktree名称
    
    规则：
        1. 不能为空
        2. 长度 ≤ 64字符
        3. 每个segment（斜杠分隔）只能包含：字母、数字、点、破折号、下划线
        4. 不能包含"."或".."作为segment
    
    返回：
        None：验证通过
        str：错误消息
    """
    if not name:
        return "name cannot be empty"
    
    if len(name) > MAX_SLUG_LENGTH:
        return f"name too long (max {MAX_SLUG_LENGTH} characters)"

    # 检查每个segment
    segments = name.split("/")
    for seg in segments:
        if not seg:
            return "name contains empty segment"
        
        if seg in (".", ".."):
            return "name must not contain '.' or '..' as a segment"
        
        if not _SEGMENT_RE.match(seg):
            return f"invalid segment: {seg!r} (allowed: letters, digits, '.', '-', '_')"

    return None


def flatten_slug(name: str) -> str:
    """
    将斜杠转换为加号
    
    原因：
    ====
    Git worktree路径不能包含斜杠（文件系统限制）。
    
    例子：
        "team-auth/frontend" -> "team-auth+frontend"
    """
    return name.replace("/", "+")
```

**为什么需要flatten？**

```bash
# 用户创建：
WorktreeManager.create("team-auth/frontend")

# 如果直接使用斜杠：
.claude/worktrees/team-auth/frontend/
# 问题：team-auth被当作目录，frontend才是worktree

# 使用flatten后：
.claude/worktrees/team-auth+frontend/
# 正确：整个目录是worktree
```

---

## 完整执行流程

### 场景：SubAgent在Worktree中执行实验

**用户请求**：

```python
Agent(
    prompt="尝试用Redis替换缓存",
    isolation="worktree"
)
```

**执行流程**：

```
[Step 1] AgentTool.execute() 检测到 isolation="worktree"
    ↓
[Step 2] 调用 _execute_with_worktree(p)
    ↓
[Step 3] 生成worktree名称：generate_worktree_name()
    ↓ 返回：worktree-a1b2c3d4（随机）
    ↓
[Step 4] WorktreeManager.create("worktree-a1b2c3d4", "HEAD")
    ↓
    ├─ [4.1] validate_slug("worktree-a1b2c3d4") ✓
    ├─ [4.2] flatten_slug() → "worktree-a1b2c3d4"
    ├─ [4.3] read_worktree_head_sha("HEAD") → "abc123def456"
    ├─ [4.4] git worktree add .claude/worktrees/worktree-a1b2c3d4 -b wt/worktree-a1b2c3d4 HEAD
    ├─ [4.5] perform_post_creation_setup()
    │   ├─ 复制 .env
    │   ├─ 设置 core.hooksPath
    │   ├─ symlink node_modules/
    │   └─ 复制 .worktreeinclude 中的文件
    └─ [4.6] 返回 Worktree对象
    ↓
[Step 5] 构建worktree通知：build_worktree_notice()
    ↓
    通知内容：
    """
    You are working in an isolated Git worktree.
    Original repo: /home/user/project
    Worktree path: /home/user/project/.claude/worktrees/worktree-a1b2c3d4
    Branch: wt/worktree-a1b2c3d4
    
    All file operations must use paths relative to the worktree.
    Changes here do not affect the main working directory.
    """
    ↓
[Step 6] 创建SubAgent（work_dir = worktree路径）
    ↓
    sub_agent = AgentClass(
        client=client,
        registry=filtered_registry,
        work_dir=wt.path,  # .claude/worktrees/worktree-a1b2c3d4
        permission_checker=PermissionChecker(
            sandbox=PathSandbox(wt.path),  # 限制在worktree内
        ),
    )
    ↓
[Step 7] SubAgent执行任务
    ↓
    Sub Agent调用工具：
    - Read(".env") → 读取worktree中的.env
    - Write("src/cache.py", redis_code) → 写入worktree中的文件
    - Bash("npm test") → 在worktree中运行测试
    ↓
    所有操作都在worktree中，主工作区完全不受影响
    ↓
[Step 8] SubAgent完成，返回结果
    ↓
[Step 9] auto_cleanup(name="worktree-a1b2c3d4", original_head="abc123def456")
    ↓
    ├─ [9.1] count_worktree_changes()
    │   ├─ git status --porcelain → 3个未提交文件
    │   └─ git rev-list --count abc123def456..HEAD → 1个新commit
    │   └─ 返回 Changes(uncommitted=3, new_commits=1)
    ├─ [9.2] has_changes = True
    └─ [9.3] 返回 CleanupResult(kept=True, path="...", branch="wt/worktree-a1b2c3d4")
    ↓
[Step 10] 返回结果给主Agent
    ↓
    结果：
    """
    我已经实现了Redis缓存方案：
    - 修改了src/cache.py，使用redis-py客户端
    - 更新了requirements.txt，添加redis==4.5.0
    - 测试通过：npm test ✓
    
    [Worktree preserved at .claude/worktrees/worktree-a1b2c3d4, branch wt/worktree-a1b2c3d4]
    """
```

**用户可以**：

```bash
# 1. 查看worktree中的修改
cd .claude/worktrees/worktree-a1b2c3d4
git diff

# 2. 如果满意，合并到主分支
git checkout main
git merge wt/worktree-a1b2c3d4

# 3. 如果不满意，直接删除worktree
git worktree remove .claude/worktrees/worktree-a1b2c3d4
```

---

## 关键技术点详解

### 1. 为什么需要快速恢复优化？

**问题**：auto_cleanup需要判断worktree是否有变更

```python
# 方案1：对比HEAD和base_ref
git rev-list --count HEAD..origin/main

# 问题：如果base_ref是"HEAD"，这个命令无意义
# 因为"HEAD"在主工作区已经变化了
```

**解决方案**：记录创建时的具体commit SHA

```python
# 创建时
head_commit = read_worktree_head_sha("HEAD")  # "abc123def456"

# auto_cleanup时
git rev-list --count abc123def456..HEAD  # 对比具体commit
```

---

### 2. PathSandbox如何限制访问？

**问题**：SubAgent在worktree中，但尝试访问主工作区文件

```python
# Worktree路径：/home/user/project/.claude/worktrees/exp-1
# 主工作区路径：/home/user/project

# SubAgent尝试：
Read("/home/user/project/src/main.py")  # 主工作区！
```

**解决方案**：PathSandbox检查所有文件操作

```python
class PathSandbox:
    def __init__(self, allowed_root: str):
        self.allowed_root = Path(allowed_root).resolve()
    
    def is_allowed(self, path: str) -> bool:
        target = Path(path).resolve()
        return target.is_relative_to(self.allowed_root)

# 创建SubAgent时
checker = PermissionChecker(
    sandbox=PathSandbox(wt.path),  # 只允许worktree路径
)

# SubAgent尝试访问主工作区
Read("/home/user/project/src/main.py")
# PathSandbox.is_allowed() 返回 False
# 操作被拒绝！
```

---

### 3. Symlink的性能优势

**对比**：

```bash
# 不使用symlink
time git worktree add .claude/worktrees/exp-1
# 5秒（复制所有文件）

cd .claude/worktrees/exp-1
time npm install
# 300秒，500MB磁盘

# 使用symlink
time git worktree add .claude/worktrees/exp-1
# 5秒

cd .claude/worktrees/exp-1
ln -s ../../node_modules node_modules
# 0.1秒，0MB磁盘
npm test
# 直接使用主工作区的node_modules
```

**节省**：
- 时间：300秒 → 0.1秒（3000倍）
- 磁盘：500MB → 0MB

---

### 4. Auto Cleanup的保守策略

**问题**：如果git命令出错，怎么办？

```python
# 出错场景
try:
    status = git status --porcelain
except subprocess.TimeoutExpired:
    # 超时了，不知道是否有变更
    changes.uncommitted = ?
```

**保守策略**：出错时视为有变更

```python
except (subprocess.SubprocessError, OSError):
    changes.uncommitted = 1  # 视为有变更
```

**原因**：

```
策略1：出错时视为无变更 → 删除worktree
    风险：可能误删有变更的worktree，用户丢失工作
    影响：严重

策略2：出错时视为有变更 → 保留worktree
    风险：可能保留无变更的worktree，占用磁盘
    影响：轻微（用户可以手动删除）

选择：策略2（保守策略）
```

---

## 真实案例分析

### 案例1：并行试验3种缓存方案

**需求**：对比Redis、Memcached、In-Memory三种缓存性能

**代码**：

```python
# 启动3个SubAgent，每个在独立的worktree中
Agent(
    prompt="实现Redis缓存方案",
    isolation="worktree",
    name="redis-experiment",
)
Agent(
    prompt="实现Memcached缓存方案",
    isolation="worktree",
    name="memcached-experiment",
)
Agent(
    prompt="实现In-Memory缓存方案",
    isolation="worktree",
    name="inmem-experiment",
)
```

**结果**：

```
主工作区：
    src/cache.py（原始代码）

Worktree 1：
    .claude/worktrees/redis-experiment/
    ├── src/cache.py（使用Redis客户端）
    ├── requirements.txt（+ redis==4.5.0）
    └── branch: wt/redis-experiment

Worktree 2：
    .claude/worktrees/memcached-experiment/
    ├── src/cache.py（使用Memcached客户端）
    ├── requirements.txt（+ python-memcached==1.59）
    └── branch: wt/memcached-experiment

Worktree 3：
    .claude/worktrees/inmem-experiment/
    ├── src/cache.py（使用dict缓存）
    └── branch: wt/inmem-experiment
```

**对比性能**：

```bash
# 在每个worktree中运行benchmark
cd .claude/worktrees/redis-experiment
npm run benchmark
# Redis: 10000 req/s

cd .claude/worktrees/memcached-experiment
npm run benchmark
# Memcached: 12000 req/s

cd .claude/worktrees/inmem-experiment
npm run benchmark
# In-Memory: 50000 req/s
```

**选择方案**：

```bash
# Memcached性能好且可扩展，选择它
git checkout main
git merge wt/memcached-experiment

# 清理其他worktree
git worktree remove .claude/worktrees/redis-experiment
git worktree remove .claude/worktrees/inmem-experiment
```

---

### 案例2：紧急Bug修复不打断开发

**场景**：

```
你正在开发新功能（feature/new-api）
突然产品说：紧急Bug！登录失败！
```

**传统做法**：

```bash
git stash  # 保存当前工作
git checkout main
git checkout -b hotfix/login-bug
# 修复Bug...
git commit
git checkout feature/new-api
git stash pop  # 恢复工作（可能冲突）
```

**使用Worktree**：

```python
# 主Agent继续开发新功能
# 同时启动SubAgent修Bug
Agent(
    prompt="修复登录Bug：用户点击登录后无响应",
    isolation="worktree",
    name="hotfix-login",
)
```

**结果**：

```
主工作区：
    feature/new-api分支（继续开发）

Worktree（hotfix）：
    .claude/worktrees/hotfix-login/
    ├── main分支（修复Bug）
    └── branch: wt/hotfix-login
```

**优势**：
- 主工作区不受影响
- 无需stash/pop
- Bug修完立即部署
- 新功能开发不中断

---

## 性能优化技巧

### 1. Symlink大幅减少创建时间

**对比**：

```bash
# 不使用symlink
time create_worktree_with_npm_install
# 305秒（5秒创建 + 300秒npm install）

# 使用symlink
time create_worktree_with_symlink
# 5秒（0秒npm install）
```

**节省**：300秒（5分钟）

---

### 2. 快速恢复优化避免重复git命令

**对比**：

```bash
# 方案1：每次auto_cleanup都执行git rev-parse
time auto_cleanup_without_cache
# 0.5秒（git rev-parse耗时）

# 方案2：创建时缓存commit SHA
time auto_cleanup_with_cache
# 0.1秒（只执行git rev-list）
```

**节省**：0.4秒/次

---

### 3. 并行创建多个Worktree

**对比**：

```bash
# 串行创建
time create_3_worktrees_serial
# 15秒（5秒 × 3）

# 并行创建
time create_3_worktrees_parallel
# 5秒（asyncio并发）
```

**节省**：10秒（3倍加速）

---

## 常见陷阱与解决方案

### 陷阱1：Worktree路径包含斜杠

**错误**：

```python
WorktreeManager.create("team-auth/frontend")

# 创建的路径：
.claude/worktrees/team-auth/frontend/
#                          ↑ 斜杠被当作目录分隔符
```

**问题**：
- team-auth被当作目录
- frontend才是worktree
- 路径混乱

**解决方案**：flatten_slug()

```python
name = "team-auth/frontend"
flattened = flatten_slug(name)  # "team-auth+frontend"

# 创建的路径：
.claude/worktrees/team-auth+frontend/
# 正确：整个目录是worktree
```

---

### 陷阱2：忘记设置Git Hooks

**错误**：

```bash
cd .claude/worktrees/experiment-1
git commit -m "test"

# pre-commit hook（格式化代码）没有执行
# 代码未格式化就提交了
```

**解决方案**：_setup_git_hooks()

```python
# 自动设置
subprocess.run([
    "git", "config", "core.hooksPath", ".husky"
], cwd=wt_path)

# 现在提交时会执行hook
git commit -m "test"
# ✓ pre-commit执行，代码被格式化
```

---

### 陷阱3：Auto Cleanup误删有变更的Worktree

**错误场景**：

```bash
# SubAgent在worktree中修改了文件
Write("src/cache.py", new_code)

# 但git命令超时
git status --porcelain  # TimeoutExpired

# auto_cleanup检测不到变更，删除worktree
# 用户丢失工作！
```

**解决方案**：保守策略

```python
try:
    status = git status --porcelain
except subprocess.TimeoutExpired:
    changes.uncommitted = 1  # 视为有变更，保留worktree
```

---

## 最佳实践

### 1. 何时使用Worktree隔离？

**使用Worktree**：
```python
# 试验性修改
Agent(prompt="尝试用Redis替换缓存", isolation="worktree")

# 大规模重构
Agent(prompt="重构整个auth模块", isolation="worktree")

# 并行对比
Agent(prompt="对比方案A", isolation="worktree")
Agent(prompt="对比方案B", isolation="worktree")
```

**不使用Worktree**：
```python
# 确定的修改
Agent(prompt="修复Bug")

# 只读任务
Agent(prompt="分析代码结构")
```

---

### 2. 配置Symlink目录

**在配置文件中**：

```yaml
# config.yaml
worktree:
  symlink_directories:
    - node_modules
    - vendor
    - .venv
    - build/cache
```

**效果**：
- 节省磁盘空间
- 加快创建速度
- 共享依赖

---

### 3. 使用.worktreeinclude

**场景**：某些构建产物需要在worktree中

```
# .worktreeinclude
build/config.json
dist/*.map
.cache/
```

**效果**：这些文件会被复制到每个worktree

---

## 总结

### Worktree系统的核心价值

#### 1. 并行开发 → 零切换成本

**原理**：
```
传统：stash → checkout → 修改 → commit → checkout → pop
Worktree：创建worktree → 修改 → commit（主工作区不受影响）
```

**实现**：
- Git worktree原生功能
- WorktreeManager管理生命周期
- PathSandbox限制访问

---

#### 2. 物理隔离 → 安全试验

**原理**：
```
主工作区：稳定代码
Worktree 1：试验方案A
Worktree 2：试验方案B
失败了直接删除，不影响主工作区
```

**实现**：
- 独立的文件系统
- 独立的Git分支
- auto_cleanup自动删除无变更的worktree

---

#### 3. 智能清理 → 避免误删

**原理**：
```
检测未提交文件 + 检测新commit
→ 有变更：保留
→ 无变更：删除
```

**实现**：
- count_worktree_changes()
- 保守策略（出错时保留）

---

#### 4. 性能优化 → 秒级创建

**原理**：
```
Symlink node_modules → 节省300秒
缓存commit SHA → 节省0.4秒
并行创建 → 3倍加速
```

**实现**：
- _create_symlinks()
- read_worktree_head_sha()
- asyncio.to_thread()

---

### 核心组件总结

| 组件 | 职责 | 关键方法 |
|------|------|---------|
| WorktreeManager | 生命周期管理 | create(), remove(), auto_cleanup() |
| setup.py | 初始化设置 | _copy_local_configs(), _setup_git_hooks(), _create_symlinks() |
| changes.py | 变更检测 | count_worktree_changes(), has_worktree_changes() |
| session.py | 会话管理 | save_worktree_session(), load_worktree_session() |
| slug.py | 名称验证 | validate_slug(), flatten_slug() |

---

### 下一步阅读

- **CH12: SubAgent** - Worktree如何与SubAgent集成
- **CH14: Team System** - 团队成员的Worktree隔离
- **CH11: Hook System** - Worktree的Hook配置

---

*本文档基于MewCode源码编写，详细讲解了Git Worktree的实现机制和MewCode的Worktree管理系统。版本：2024-12*
