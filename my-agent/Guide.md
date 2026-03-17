# Response 结构参考

```
response (Message)
├── id: "msg_..."
├── model: "MiniMax-M2.5"
├── role: "assistant"
├── stop_reason: "end_turn" | "tool_use"
├── usage: Usage(input_tokens=44, output_tokens=90)
└── content: list
      ├── [0] ThinkingBlock          ← 模型思考过程（开启 extended thinking 时出现）
      │        .type = "thinking"
      │        .thinking = "..."     ← 思考内容
      │        （没有 .text 属性）
      └── [1] TextBlock              ← 实际回复
               .type = "text"
               .text = "..."         ← 回复文本
      └── [?] ToolUseBlock           ← 工具调用（stop_reason == "tool_use" 时出现）
               .type = "tool_use"
               .id = "tu_..."
               .name = "bash"
               .input = {"command": "ls"}
```

> 注意：开启 extended thinking 时，content[0] 是 ThinkingBlock 不是 TextBlock。
> 取文本应遍历查找：`next((b.text for b in response.content if hasattr(b, "text")), None)`

---

# 从零实现类 Claude Code 智能体 -- 学习指南

本指南配合 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 项目使用。每一步给出**目标、思路和测试方案**，不包含具体代码。你需要独立思考实现方式，遇到困难时可参考 `agents/` 目录下的参考实现和 `docs/zh/` 下的详细文档。

---

## 学习路径总览

整个项目分为 **12 步**，从一个最简循环逐步叠加机制，最终构建出支持多智能体协作和隔离执行的完整系统。

### 依赖关系图

```
第一阶段: 单智能体能力 (s01-s08)
=========================================

s01 智能体循环 ──── 基础中的基础，所有后续步骤的地基
 │
 └──> s02 工具使用 ── 扩展工具集，建立分发模式
       │
       ├──> s03 待办写入 ── 让智能体学会做计划
       │
       ├──> s04 子智能体 ── 上下文隔离，任务委派
       │
       ├──> s05 技能加载 ── 按需注入领域知识
       │
       └──> s06 上下文压缩 ── 突破上下文窗口限制
              │
              └──> s07 任务系统 ── 持久化的任务图（后续协作的骨架）
                    │
                    └──> s08 后台任务 ── 非阻塞执行

第二阶段: 多智能体协作 (s09-s12)
=========================================

s09 智能体团队 ── 持久化队友 + 消息通信
 │
 └──> s10 团队协议 ── 结构化的请求-响应协商
       │
       └──> s11 自治智能体 ── 自组织，自动认领任务
             │
             └──> s12 Worktree 隔离 ── 每个任务一个独立目录
```

### 两阶段划分说明

**第一阶段（s01-s08）** 构建一个功能完整的单智能体：它能执行工具、做计划、委派子任务、加载领域知识、管理上下文窗口、追踪任务依赖、以及在后台并行执行命令。完成这一阶段后，你已经拥有了一个可用的编码助手。

**第二阶段（s09-s12）** 将单智能体扩展为多智能体团队：多个智能体各有身份和角色，通过消息邮箱通信，遵循协议协商，能自主从任务板认领工作，并在各自隔离的 git worktree 中互不干扰地执行。

> **核心不变量**：从 s01 到 s12，智能体循环（while + stop_reason 检查）始终不变。每一步只是在循环上叠加一个新机制。

---

## s01: 智能体循环

> *"一个工具 + 一个循环 = 一个智能体"*

### 目标

解决的问题：语言模型能推理代码，但碰不到真实世界——不能读文件、跑测试、看报错。没有循环，每次工具调用你都得手动把结果粘回去。

这一步要实现**最小可用的智能体**：一个 while 循环 + 一个 bash 工具。模型可以自主决定何时调用工具、何时停止，形成闭环。

### 核心思路

- **消息累积**：用一个列表持续收集对话历史（用户消息、助手响应、工具结果），每次调用 LLM 时传入全部历史
- **退出条件**：检查 LLM 响应的 `stop_reason`——如果是 `"tool_use"` 就继续循环执行工具，否则结束
- **工具结果回传**：工具执行结果以 `tool_result` 类型追加到消息列表中，作为 `user` 角色的内容，让 LLM 看到执行结果
- **唯一的工具**：只需一个 `bash` 工具，让模型能执行任意 shell 命令

### 关键设计决策

- **消息列表是可变的还是不可变的？** 直接在原列表上 append 最简单，但要注意引用传递的影响
- **输出截断**：bash 命令的输出可能非常长，需要设置合理的截断长度（比如 50000 字符）
- **错误处理**：子进程执行失败时，应该把错误信息也作为 tool_result 返回给 LLM，而不是让程序崩溃
- **系统提示**：需要一个最基本的 system prompt 告诉模型它是一个编码助手，并说明工作目录
- **REPL 入口**：需要一个简单的输入循环，让用户可以持续输入 prompt 与智能体交互

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Create a file called hello.py that prints "Hello, World!"` | 模型调用 bash 写文件，验证文件存在且内容正确 |
| `List all Python files in this directory` | 模型调用 bash 执行 ls 或 find，返回文件列表 |
| `What is the current git branch?` | 模型调用 bash 执行 git branch，返回分支名 |
| `Create a directory called test_output and write 3 files in it` | 模型多次调用 bash（mkdir + 写文件），验证多轮循环正常工作 |

验证清单：
- [ ] 模型能自主决定调用多少次工具
- [ ] 工具执行结果正确回传给模型
- [ ] 模型完成任务后自动停止循环（不再调用工具）
- [ ] 子进程报错时不会导致程序崩溃

---

## s02: 工具使用

> *"加一个工具，只加一个 handler"*

### 目标

解决的问题：只有 bash 时，所有操作都走 shell。`cat` 截断不可预测，`sed` 遇到特殊字符就崩，每次 bash 调用都是不受约束的安全面。专用工具可以在工具层面做路径沙箱。

这一步要建立**工具分发模式**：一个字典将工具名映射到处理函数，新增工具只需要加一个 handler + 一个 schema，循环本身不用动。

### 核心思路

- **分发字典（dispatch map）**：用 `{工具名: 处理函数}` 的字典替代 if/elif 链，一次查找完成路由
- **路径沙箱**：所有文件操作工具都通过一个 `safe_path()` 函数验证路径不会逃逸出工作区
- **专用工具**：增加 `read_file`、`write_file`、`edit_file` 三个文件操作工具，比通过 bash 操作文件更安全、更可靠
- **循环不变**：循环体里只是把硬编码的 bash 调用改成字典查找，其余完全不变

### 关键设计决策

- **路径解析**：使用 `resolve()` 将相对路径转绝对路径，再用 `is_relative_to()` 检查是否在工作区内
- **edit_file 的设计**：采用 old_text/new_text 的精确替换模式，比行号编辑更可靠——模型更擅长指定要替换的文本内容
- **输出长度限制**：read_file 需要支持行数限制参数，避免读取巨大文件时撑爆上下文
- **未知工具处理**：字典查找失败时，返回错误信息而不是崩溃

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Read the file requirements.txt` | 使用 read_file 而非 bash cat |
| `Create a file called greet.py with a greet(name) function` | 使用 write_file 工具 |
| `Edit greet.py to add a docstring to the function` | 使用 edit_file 的精确替换 |
| `Read greet.py to verify the edit worked` | 使用 read_file 验证修改 |

验证清单：
- [ ] 模型能根据任务自动选择合适的工具（read_file vs bash cat）
- [ ] 路径沙箱能阻止访问工作区外的文件（尝试读取 `/etc/passwd` 应返回错误）
- [ ] edit_file 的精确替换正确工作
- [ ] 新增工具没有修改循环代码

---

## s03: 待办写入（TodoWrite）

> *"没有计划的 agent 走哪算哪"*

### 目标

解决的问题：多步任务中，模型会丢失进度——重复做过的事、跳步、跑偏。对话越长越严重，因为系统提示的影响力被不断增长的工具结果稀释。一个 10 步重构可能做完 1-3 步就开始即兴发挥。

这一步要给智能体加上**自我追踪能力**：一个结构化的待办清单，让模型先列步骤再动手，并通过定时提醒强制更新进度。

### 核心思路

- **TodoManager**：一个内存中的状态管理器，维护带状态的待办列表。每个待办项有 id、text、status（pending / in_progress / completed）
- **单焦点约束**：同一时间只允许一个任务处于 `in_progress` 状态，强制模型按顺序聚焦
- **Nag 提醒机制**：计数器记录距上次调用 todo 工具过了多少轮。超过 3 轮不更新，就在 tool_result 中注入 `<reminder>` 提醒模型更新计划
- **注入位置**：提醒作为文本块插入到最近一条 user 消息的 content 列表开头，这样模型在处理工具结果时首先看到提醒

### 关键设计决策

- **todo 工具的接口**：采用整体替换模式（每次调用传入完整的 items 列表），而非增量操作（add/remove/update），这样模型一次调用就能看到和设定全部状态
- **提醒阈值**：3 轮是经验值——太少会打扰模型，太多则失去提醒效果
- **提醒注入 vs 系统提示**：注入到 tool_result 中比修改系统提示更精准，因为它出现在模型即将处理的上下文最近位置
- **系统提示中的指引**：需要在 system prompt 中告知模型"收到多步任务时，先用 todo 工具列出步骤"

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Refactor the file hello.py: add type hints, docstrings, and a main guard` | 模型先用 todo 列出 3 个步骤，再逐个执行并更新状态 |
| `Create a Python package with __init__.py, utils.py, and tests/test_utils.py` | 模型创建计划，按顺序完成每个文件 |
| `Review all Python files and fix any style issues` | 观察 nag 提醒是否在模型忘记更新时触发 |

验证清单：
- [ ] 模型收到多步任务时主动创建待办列表
- [ ] 同一时间只有一个任务处于 in_progress
- [ ] 连续 3 轮不调用 todo 后出现 reminder 提醒
- [ ] 提醒注入后模型回去更新待办状态

---

## s04: 子智能体

> *"大任务拆小，每个小任务干净的上下文"*

### 目标

解决的问题：智能体工作越久，消息数组越胖。每次读文件、跑命令的输出都永久留在上下文里。一个探索任务（"这个项目用什么测试框架？"）可能需要读 5 个文件，但父智能体只需要一个结论。

这一步要实现**上下文隔离**：子智能体以全新的消息列表启动，运行自己的循环，只返回最终摘要文本给父智能体。

### 核心思路

- **全新消息列表**：子智能体从空的 `messages=[]` 开始，不继承父智能体的上下文
- **独立循环**：子智能体运行自己的 while 循环，可以调用工具、积累结果，直到完成
- **仅返回摘要**：子智能体完成后，只把最后一条文本响应返回给父智能体，中间过程全部丢弃
- **禁止递归**：子智能体拥有除 `task` 外的所有基础工具，防止子智能体再生成子智能体

### 关键设计决策

- **安全上限**：子智能体需要一个最大循环次数限制（比如 30 轮），防止失控
- **工具集区分**：父端工具集 = 基础工具 + task 工具；子端工具集 = 仅基础工具
- **系统提示差异**：子智能体需要自己的系统提示，强调"专注完成给定任务后给出简洁摘要"
- **摘要提取**：从最后一条响应中提取所有文本块并拼接，如果没有文本则返回占位符
- **task 工具的 schema**：只需一个 `prompt` 字段，由父智能体描述要委派的任务

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Use a subtask to find what testing framework this project uses` | 模型调用 task 工具，子智能体读取文件后返回简洁答案 |
| `Delegate: read all .py files and summarize what each one does` | 子智能体读取多个文件，返回摘要，父上下文保持干净 |
| `Use a task to create a new module, then verify it from here` | 子智能体创建文件，父智能体用 read_file 验证 |

验证清单：
- [ ] 子智能体的中间工具调用不出现在父消息列表中
- [ ] 子智能体返回的只是一段摘要文本
- [ ] 连续多次委派任务后，父智能体的上下文仍然紧凑
- [ ] 子智能体不能再生成子智能体

---

## s05: 技能加载

> *"用到什么知识，临时加载什么知识"*

### 目标

解决的问题：你希望智能体遵循特定领域的工作流（git 约定、测试模式、代码审查清单），但全部塞进系统提示太浪费——10 个技能每个 2000 token，就是 20000 token，大部分跟当前任务毫无关系。

这一步要实现**两层技能注入**：系统提示中放技能名称（低成本），需要时通过 tool_result 加载完整内容（按需付费）。

### 核心思路

- **技能文件结构**：每个技能是一个目录，包含 `SKILL.md` 文件，文件开头有 YAML frontmatter（name、description），后面是完整的技能内容
- **SkillLoader**：启动时递归扫描所有 `SKILL.md` 文件，解析 frontmatter 和正文，按名称索引
- **第一层（系统提示）**：在系统提示末尾列出所有技能的名称和简短描述，每个技能只占约 100 token
- **第二层（tool_result）**：`load_skill` 工具被调用时，返回完整的技能正文，包裹在 `<skill>` 标签中

### 关键设计决策

- **YAML frontmatter 解析**：需要处理 `---` 分隔符，提取 name 和 description 字段
- **目录名 vs name 字段**：当 frontmatter 中没有 name 时，回退到使用目录名作为技能标识
- **技能目录位置**：需要确定技能文件的扫描根目录
- **与子智能体的关系**：本步可以独立于 s04 实现，不需要子智能体能力
- **内容包装**：返回时用 `<skill name="xxx">` 标签包裹，帮助模型理解这是按需加载的领域知识

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `What skills are available?` | 模型直接从系统提示中列出技能名称和描述，不需要调用工具 |
| `Load the agent-builder skill and follow its instructions` | 模型调用 load_skill，获取完整内容并遵循指引 |
| `I need to do a code review -- load the relevant skill first` | 模型自行判断该加载哪个技能并加载 |

验证清单：
- [ ] 系统提示中能看到技能列表（名称 + 描述）
- [ ] load_skill 返回完整的技能正文
- [ ] 未加载的技能不占用上下文空间
- [ ] 请求不存在的技能时返回友好的错误信息

---

## s06: 上下文压缩

> *"上下文总会满，要有办法腾地方"*

### 目标

解决的问题：上下文窗口是有限的。读一个 1000 行的文件就吃掉约 4000 token；读 30 个文件、跑 20 条命令，轻松突破 100k token。不压缩，智能体没法在大项目里长时间工作。

这一步要实现**三层压缩策略**，激进程度递增，让智能体能持续工作而不被上下文窗口卡死。

### 核心思路

- **第一层 micro_compact（静默，每轮执行）**：在每次 LLM 调用前，把超过 N 轮之前的旧 tool_result 替换为简短占位符（如 `[Previous: used read_file]`），只保留最近几轮的完整结果
- **第二层 auto_compact（自动，达到阈值触发）**：当估算 token 数超过阈值（如 50000）时，先将完整对话保存到磁盘（`.transcripts/` 目录），然后让 LLM 对整个对话做摘要，用摘要替换所有消息
- **第三层 compact 工具（手动触发）**：暴露一个 `compact` 工具让模型或用户主动触发压缩，执行与 auto_compact 相同的逻辑
- **信息不丢失**：完整历史通过 transcript 文件保存在磁盘上，只是移出了活跃上下文

### 关键设计决策

- **token 估算方法**：精确 token 计数需要 tokenizer，简单方案可以用字符数除以 4 来估算
- **micro_compact 保留数量**：保留最近几轮的完整结果（如最近 3 轮），更早的替换为占位符
- **压缩后的消息格式**：用一条 user 消息放摘要 + 一条 assistant 消息确认（"Understood. Continuing."），保持消息交替规则
- **transcript 保存格式**：JSONL 格式，每行一条消息，便于后续分析和恢复
- **循环整合位置**：micro_compact 在每次 LLM 调用前执行；auto_compact 在 micro_compact 之后、LLM 调用之前检查阈值

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Read every Python file in the agents/ directory one by one` | 观察 micro_compact 替换旧的 read_file 结果为占位符 |
| `Keep reading files until compression triggers automatically` | 持续读文件直到 token 超阈值，观察 auto_compact 触发并生成 transcript 文件 |
| `Use the compact tool to manually compress the conversation` | 手动压缩，验证对话被摘要替换后仍能继续工作 |

验证清单：
- [ ] micro_compact 每轮静默执行，旧结果被替换为占位符
- [ ] auto_compact 在 token 超阈值时自动触发
- [ ] 压缩后智能体仍能继续工作，知道之前做了什么
- [ ] transcript 文件正确保存到磁盘
- [ ] compact 工具可手动触发压缩

---

## s07: 任务系统

> *"大目标要拆成小任务，排好序，记在磁盘上"*

### 目标

解决的问题：s03 的 TodoManager 只是内存中的扁平清单——没有顺序、没有依赖，上下文压缩一跑就没了。真实目标是有结构的：任务 B 依赖任务 A，任务 C 和 D 可以并行，任务 E 要等 C 和 D 都完成。

这一步要把扁平清单升级为**持久化的任务图（DAG）**：每个任务一个 JSON 文件，有状态和依赖关系，压缩和重启后依然存活。这个任务图是后续所有协作机制的骨架。

### 核心思路

- **文件持久化**：每个任务是 `.tasks/` 目录下的一个 JSON 文件（如 `task_1.json`），包含 id、subject、description、status、blockedBy、blocks、owner 等字段
- **依赖图**：通过 `blockedBy`（前置依赖）和 `blocks`（后置依赖）两个列表建立 DAG 关系
- **自动解锁**：任务完成时，自动将其 id 从其他任务的 `blockedBy` 中移除，解锁后续任务
- **三个核心问题**：任务图随时回答——什么可以做（pending 且无阻塞）、什么被卡住（有未完成的前置）、什么做完了
- **TaskManager**：提供 create、update、list_all、get 四个操作，对应四个工具

### 关键设计决策

- **ID 生成策略**：自增整数最简单，需要扫描现有文件找到最大 ID
- **双向依赖维护**：添加 blockedBy 时是否需要自动维护反向的 blocks 关系，还是只做单向
- **状态机**：`pending → in_progress → completed`，是否需要支持回退或取消
- **owner 字段**：预留给后续多智能体使用，单智能体阶段可以留空
- **与 s03 的关系**：任务系统替代 TodoManager 成为主要的计划工具，Todo 可保留用于轻量级场景

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Create 3 tasks: "Setup project", "Write code", "Write tests". Make them depend on each other in order.` | 创建 3 个任务，建立 1→2→3 的依赖链 |
| `List all tasks and show the dependency graph` | task_list 返回所有任务及其依赖状态 |
| `Complete task 1 and then list tasks to see task 2 unblocked` | 完成 task 1 后 task 2 的 blockedBy 自动变空 |
| `Create a task board for refactoring: parse -> transform -> emit -> test, where transform and emit can run in parallel after parse` | 创建菱形依赖图 |

验证清单：
- [ ] 任务以 JSON 文件形式持久化在 `.tasks/` 目录
- [ ] 完成任务后自动解锁被它阻塞的后续任务
- [ ] 重启程序后任务状态依然存在
- [ ] 依赖关系正确阻止被阻塞的任务被执行

---

## s08: 后台任务

> *"慢操作丢后台，agent 继续想下一步"*

### 目标

解决的问题：有些命令要跑好几分钟（`npm install`、`pytest`、`docker build`）。阻塞式循环下模型只能干等。用户说"装依赖，顺便建个配置文件"，智能体却只能一个一个来。

这一步要实现**非阻塞执行**：后台线程跑命令，主循环继续运行，命令完成后通过通知队列注入结果。

### 核心思路

- **BackgroundManager**：维护任务字典和线程安全的通知队列
- **守护线程**：`run()` 方法启动 daemon 线程执行子进程，立即返回任务 ID
- **通知队列**：子进程完成后，结果进入队列。每次 LLM 调用前排空队列，将结果作为 `<background-results>` 注入消息
- **主循环单线程**：循环本身保持单线程，只有子进程 I/O 被并行化

### 关键设计决策

- **任务 ID 格式**：短 UUID（如 8 字符）足够用于标识
- **线程安全**：通知队列的读写需要加锁（threading.Lock）
- **超时控制**：子进程需要设置超时（如 300 秒），防止永远挂起
- **结果截断**：通知中的结果需要截断（如 500 字符），因为只是通知，完整结果可通过 check 工具获取
- **注入格式**：用 user + assistant 消息对注入通知结果，保持消息交替规则
- **两个工具**：`background_run`（启动后台命令）和 `check_background`（查看状态和结果）

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Run "sleep 5 && echo done" in the background, then create a file while it runs` | 后台命令开始后，主循环继续执行其他工具调用 |
| `Start 3 background tasks: "sleep 2", "sleep 4", "sleep 6". Check their status.` | 3 个任务并行执行，check 能看到各自状态 |
| `Run pytest in the background and keep working on other things` | 验证后台结果在完成后自动注入 |

验证清单：
- [ ] 后台命令不阻塞主循环
- [ ] 命令完成后结果自动注入到下一轮 LLM 调用前
- [ ] 多个后台任务可以并行运行
- [ ] 超时的后台任务会返回超时错误
- [ ] check_background 能查看任务状态

---

## s09: 智能体团队

> *"任务太大一个人干不完，要能分给队友"*

### 目标

解决的问题：子智能体（s04）是一次性的——生成、干活、返回摘要、消亡，没有身份，没有跨调用的记忆。后台任务（s08）能跑 shell 命令，但做不了 LLM 引导的决策。

这一步要实现**持久化的智能体团队**：每个队友有名字、角色、独立的 agent loop，通过文件系统的 JSONL 邮箱进行异步通信。

### 核心思路

- **TeammateManager**：通过 `config.json` 维护团队名册（成员列表和状态），每个队友在独立线程中运行自己的 agent loop
- **MessageBus**：基于文件系统的 append-only JSONL 收件箱。每个队友一个文件（如 `alice.jsonl`），发消息就追加一行 JSON，读收件箱就读取全部并清空
- **生命周期**：spawn → working → idle → shutdown
- **收件箱轮询**：每个队友在每次 LLM 调用前检查自己的收件箱，有消息就注入上下文
- **消息类型**：message（点对点）、broadcast（群发）

### 关键设计决策

- **通信模式**：基于文件的 JSONL 比内存队列更可观测（可以直接查看文件内容调试）
- **drain-on-read**：读取收件箱后清空文件，避免重复处理
- **领导角色**：主循环的用户就是"lead"，队友可以给 lead 发消息
- **线程管理**：使用 daemon 线程，主进程退出时自动清理
- **团队目录结构**：`.team/config.json` 存名册，`.team/inbox/` 存收件箱
- **REPL 命令**：增加 `/team`（查看名册）和 `/inbox`（手动查看领导收件箱）

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Spawn alice (coder) and bob (tester). Have alice send bob a message.` | 两个队友线程启动，消息写入 bob.jsonl |
| `Broadcast "status update: phase 1 complete" to all teammates` | 消息出现在所有队友的收件箱中 |
| `Check the lead inbox for any messages` | 读取 lead.jsonl 中的消息 |
| 输入 `/team` | 显示团队名册和各成员状态 |
| 输入 `/inbox` | 显示领导的收件箱内容 |

验证清单：
- [ ] 队友在独立线程中运行自己的 agent loop
- [ ] 消息通过 JSONL 文件正确传递
- [ ] 队友能在 LLM 调用前看到收件箱消息
- [ ] config.json 正确反映团队状态
- [ ] broadcast 能发送给所有队友

---

## s10: 团队协议

> *"队友之间要有统一的沟通规矩"*

### 目标

解决的问题：s09 中队友能干活能通信，但缺少结构化协调。直接杀线程会留下写了一半的文件；队友接到高风险任务立刻开干，没有审查环节。

这一步要实现**请求-响应协议**：两种场景（关机和计划审批）共用同一个模式——一方发带唯一 ID 的请求，另一方引用同一 ID 响应。

### 核心思路

- **统一的 request-response 模式**：发送方生成 `request_id`，接收方用同一 `request_id` 响应
- **共享 FSM**：`pending → approved | rejected`，同一状态机驱动所有协议
- **关机协议**：领导发 shutdown_request → 队友收到后决定 approve（收尾退出）或 reject（继续干）→ 领导收到响应更新状态
- **计划审批协议**：队友提交 plan → 领导收到后 approve 或 reject（带反馈）→ 队友根据结果执行或修改
- **请求追踪器**：内存字典追踪每个 request_id 的状态和元信息

### 关键设计决策

- **消息类型扩展**：在 MessageBus 基础上增加 `shutdown_request`、`shutdown_response`、`plan_approval`、`plan_approval_response` 四种消息类型
- **request_id 关联**：响应消息必须携带原始 request_id，否则无法关联
- **状态追踪位置**：请求状态存在发起方的内存中（不是文件），因为是会话级别的
- **审批后操作**：关机审批通过后队友应设置状态为 shutdown 并退出循环
- **工具数量增长**：增加 shutdown_request、shutdown_response、plan_submit、plan_review 等工具

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Spawn alice as a coder. Then request her shutdown.` | 发送 shutdown_request，alice 收到后响应 |
| `List teammates to see alice's status after shutdown approval` | alice 状态变为 shutdown |
| `Spawn bob with a risky refactoring task. Review and reject his plan.` | bob 提交计划，领导拒绝并给出反馈 |
| `Spawn charlie, have him submit a plan, then approve it.` | 完整的提交→审批→执行流程 |
| 输入 `/team` | 监控各成员状态变化 |

验证清单：
- [ ] 关机协议完成完整的 request → response 握手
- [ ] request_id 在请求和响应之间正确关联
- [ ] 审批通过后队友正确退出
- [ ] 审批拒绝后队友能收到反馈
- [ ] 同一个 FSM 模式同时服务于关机和计划审批

---

## s11: 自治智能体

> *"队友自己看看板，有活就认领"*

### 目标

解决的问题：s09-s10 中，队友只在被明确指派时才动。领导得给每个队友写 prompt 分配任务，任务看板上 10 个未认领的任务得手动分配一一对应。这扩展不了。

这一步要实现**自组织**：队友完成手头任务后进入空闲阶段，自动轮询收件箱和任务看板，发现有活就认领开干，长时间无活就自动关机。

### 核心思路

- **双阶段生命周期**：WORK（执行 LLM 循环）→ IDLE（轮询等待）→ WORK（有新任务）→ ... → SHUTDOWN（超时）
- **空闲轮询**：每 5 秒检查一次收件箱和任务看板。收件箱有消息 → 回到 WORK；看板有未认领任务 → 认领后回到 WORK
- **任务认领**：扫描 `.tasks/` 目录，找 status=pending、无 owner、无 blockedBy 的任务，设置 owner 为自己的名字
- **空闲超时**：连续 60 秒（12 次轮询）没有新任务 → 自动设置状态为 shutdown 并退出
- **身份重注入**：上下文压缩后消息可能只剩摘要，队友忘了自己是谁。检测到消息列表过短时，在开头插入身份块

### 关键设计决策

- **轮询间隔**：5 秒是平衡点——太短浪费 CPU，太长响应慢
- **超时时长**：60 秒适合教学演示，生产环境可能需要更长
- **认领原子性**：简单实现中先到先得（读取 JSON → 写入 owner），并发冲突概率低但存在
- **身份检测条件**：`len(messages) <= 3` 说明发生了上下文压缩，需要重注入身份
- **idle 工具**：增加一个 `idle` 工具，让模型主动声明"我做完了，进入空闲"
- **claim_task 工具**：增加认领任务的工具，自动设置 owner 和 status

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Create 3 tasks on the board, then spawn alice and bob. Watch them auto-claim.` | 队友自动从任务板认领并执行任务 |
| `Spawn a coder teammate and let it find work from the task board itself` | 队友完成 prompt 任务后进入 IDLE，自动发现并认领看板任务 |
| `Create tasks with dependencies. Watch teammates respect the blocked order.` | 被阻塞的任务不会被认领 |
| 输入 `/tasks` | 看到任务的 owner 被自动填充 |
| 输入 `/team` | 观察队友在 working 和 idle 之间切换 |

验证清单：
- [ ] 队友完成任务后自动进入 IDLE 轮询
- [ ] 未认领的可用任务被自动认领
- [ ] 被阻塞的任务不会被提前认领
- [ ] 60 秒无活后队友自动关机
- [ ] 上下文压缩后身份被正确重注入

---

## s12: Worktree 任务隔离

> *"各干各的目录，互不干扰"*

### 目标

解决的问题：到 s11，多个智能体能自主认领和完成任务，但所有任务共享一个目录。两个智能体同时重构不同模块——A 改 config.py，B 也改 config.py——未提交的改动互相污染，谁也没法干净回滚。

这一步要实现**目录级别的隔离**：每个任务绑定一个独立的 git worktree，用任务 ID 把"做什么"（任务板）和"在哪做"（worktree）关联起来。

### 核心思路

- **控制平面 vs 执行平面**：`.tasks/` 管"做什么"，`.worktrees/` 管"在哪做"，通过 task_id 绑定
- **WorktreeManager**：管理 git worktree 的创建、列表、保留、删除。每个 worktree 对应一个独立分支
- **任务绑定**：创建 worktree 时传入 task_id，自动将任务推进到 in_progress 并记录 worktree 名称
- **收尾操作**：两种选择——`keep`（保留目录供后续使用）或 `remove`（删除目录 + 可选完成任务），一个调用搞定拆除 + 完成
- **事件流**：每个生命周期步骤写入 `.worktrees/events.jsonl`，提供完整的审计轨迹

### 关键设计决策

- **Worktree 注册表**：`.worktrees/index.json` 记录所有 worktree 的元信息（名称、路径、分支、绑定的 task_id、状态）
- **分支命名**：自动创建 `wt/<worktree-name>` 格式的分支
- **双向绑定维护**：创建时同时更新 worktree index 和 task JSON；删除时同样
- **事件类型**：`worktree.create.before/after/failed`、`worktree.remove.before/after/failed`、`worktree.keep`、`task.completed`
- **命令执行隔离**：在 worktree 中执行命令时，将 `cwd` 指向 worktree 目录
- **崩溃恢复**：从 `.tasks/` + `.worktrees/index.json` 可以重建现场，因为这些都是磁盘持久化的

### 测试方案

| 测试 prompt | 预期行为 |
|---|---|
| `Create tasks for backend auth and frontend login page, then list tasks.` | 创建两个任务 |
| `Create worktree "auth-refactor" for task 1, then bind task 2 to a new worktree "ui-login".` | 创建两个 worktree，任务自动变为 in_progress |
| `Run "git status --short" in worktree "auth-refactor".` | 命令在隔离目录中执行 |
| `Keep worktree "ui-login", then list worktrees and inspect events.` | worktree 状态变为 kept，events.jsonl 有记录 |
| `Remove worktree "auth-refactor" with complete_task=true, then list tasks/worktrees/events.` | worktree 被删除，task 1 自动完成，事件日志记录全过程 |

验证清单：
- [ ] git worktree 正确创建独立目录和分支
- [ ] 任务与 worktree 双向绑定
- [ ] worktree 中的命令执行不影响主目录
- [ ] remove + complete_task 一步完成拆除和任务完成
- [ ] events.jsonl 记录完整的生命周期事件
- [ ] 重启后可从 index.json 恢复 worktree 状态

---

## 总结

完成 s01-s12 后，你实现了一个从最简循环到多智能体隔离执行的完整系统。核心洞察始终是：

**循环不变，每一步只是叠加一个机制。**

```
s01  循环        -- 智能体的最小闭环
s02  工具分发    -- 扩展能力而不改循环
s03  待办追踪    -- 让模型学会做计划
s04  子智能体    -- 上下文隔离
s05  技能加载    -- 按需注入知识
s06  上下文压缩  -- 突破窗口限制
s07  任务系统    -- 持久化的依赖图
s08  后台执行    -- 非阻塞并行
s09  团队通信    -- 持久化队友 + 邮箱
s10  团队协议    -- 请求-响应协商
s11  自治认领    -- 自组织
s12  目录隔离    -- 互不干扰的执行环境
```

每一步都可以独立运行和验证。祝你构建愉快。
