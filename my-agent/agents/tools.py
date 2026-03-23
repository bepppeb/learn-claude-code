"""
tools.py — Agent 工具集

本文件包含 agent 可用的所有工具实现，按功能分为几大模块：

1. 团队协作（s10 新增，s11 增强）
   - MessageBus      — 基于 JSONL 文件的异步消息系统
   - TeammateManager — 队友生命周期管理（spawn/work/idle/shutdown）
   核心设计：队友在独立线程中运行完整的 agent loop，通过邮箱与 lead 通信
   s11 增强：队友完成工作后进入空闲循环，自动轮询任务板认领新任务
            身份重注入确保压缩后 LLM 不会忘记自己是谁

2. 上下文管理
   - micro_compact   — 细粒度压缩：替换旧 tool_result 为占位符
   - auto_compact    — 粗粒度压缩：LLM 摘要替换全部对话历史
   - estimate_tokens — 粗略 token 计数

3. 任务管理
   - TaskManager     — 持久化任务 DAG（.tasks/ 目录）
   - TodoManager     — 内存中的轻量待办列表
   - BackgroundManager — 非阻塞后台命令执行

4. 基础工具
   - bash/read_file/write_file/edit_file — 文件系统和 shell 操作
   - run_subagent    — 一次性子智能体（与队友不同：同步阻塞，用完即弃）

5. 技能系统
   - SKILL_LOADER    — 从 skills/ 目录加载 SKILL.md 文件

工具注册：
   - TOOL_HANDLERS   — name → handler 映射，lead 的工具分发表
   - CHILD_AGENT_TOOLS — 子智能体/队友的工具 schema 列表
   - PARENT_AGENT_TOOLS — lead 的完整工具 schema 列表
"""

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from settings import client, MODEL
import re

# 上下文压缩阈值：粗略估算 token 数超过此值时触发 auto_compact
# 计算方式：len(str(messages)) // 4（约 4 字符 = 1 token）
# 50000 token ≈ 200K 字符，为 200K 上下文窗口留出足够的回复空间
THRESHOLD = 50000

# ============================================================
# 团队协作系统 (s10)
# ============================================================
#
# 整体架构：
#   Lead agent 是"团队领导"，可以 spawn 多个 teammate（队友）
#   每个队友在独立 daemon 线程中运行自己的 agent loop（有完整 LLM 推理能力）
#   lead 和队友之间通过基于 JSONL 文件的"邮箱"系统异步通信
#
# 文件系统布局：
#   .team/
#     config.json          — 团队名册（所有队友的 name/role/status）
#     inbox/
#       lead.jsonl         — lead 的收件箱
#       alice.jsonl        — 队友 alice 的收件箱
#       bob.jsonl          — 队友 bob 的收件箱
#
# 与 s04 子智能体（subagent）的对比：
#   子智能体：run_subagent() 同步阻塞，共享 TOOL_HANDLERS，用完即弃
#   队友：    threading.Thread 异步执行，有独立工具集，有持久身份和生命周期

# 团队数据根目录，存 config.json（团队名册）
TEAM_DIR = Path.cwd() / ".team"
# 收件箱目录，每个队友一个 .jsonl 文件（如 alice.jsonl, bob.jsonl）
INBOX_DIR = TEAM_DIR / "inbox"

# 消息类型白名单
VALID_MSG_TYPES = {
    "message",                  # 普通点对点消息（lead ↔ teammate）
    "broadcast",                # 群发消息（lead → all teammates）
    "shutdown_request",         # lead 请求队友主动关机（s10 协议）
    "shutdown_response",        # 队友确认/拒绝关机（s10 协议）
    "plan_approval_response",   # lead 对队友计划的审批回复（s10 协议）
}

# ============================================================
# 请求跟踪器 (s10 Team Protocols)
# ============================================================
#
# 两种协议共用同一个 request_id 关联模式：
#
# 1. Shutdown 协议 (Lead → Teammate)：
#    Lead 发 shutdown_request{request_id} → Teammate 回 shutdown_response{request_id, approve}
#    FSM: pending → approved | rejected
#
# 2. Plan Approval 协议 (Teammate → Lead)：
#    Teammate 发 plan_approval{request_id, plan} → Lead 回 plan_approval_response{request_id, approve}
#    FSM: pending → approved | rejected

shutdown_requests: dict[str, dict] = {}  # {request_id: {"target": name, "status": "pending|approved|rejected"}}
plan_requests: dict[str, dict] = {}      # {request_id: {"from": name, "plan": text, "status": "pending|approved|rejected"}}
_tracker_lock = threading.Lock()          # 保护上述两个字典的并发访问（lead 线程和队友线程都可能读写）
_claim_lock = threading.Lock()            # s11: 保护 claim_task 的原子性（防止两个队友同时认领同一个任务）

# ============================================================
# s11 自治空闲循环参数
# ============================================================
# 队友完成当前工作后，进入 IDLE 阶段，每隔 POLL_INTERVAL 秒检查：
#   1. 收件箱是否有新消息（lead 或其他队友发来的）
#   2. 任务板（.tasks/）是否有未认领的任务
# 如果超过 IDLE_TIMEOUT 秒仍无新工作，队友自动关机
POLL_INTERVAL = 5   # 空闲轮询间隔（秒）
IDLE_TIMEOUT = 60   # 空闲超时（秒）


class MessageBus:
    """基于 JSONL 文件的异步消息总线。

    设计选择：为什么用文件而不用内存队列？
    1. 持久化：进程崩溃后消息不丢失
    2. 可观测：直接 cat inbox/alice.jsonl 就能看到未读消息
    3. 简单：不需要额外的消息中间件

    并发安全模型：
    - 每个收件箱（即每个 .jsonl 文件）有独立的 threading.Lock
    - send() 和 read_inbox() 在操作同一个收件箱时互斥
    - 不同收件箱的操作完全并行（alice 的锁不影响 bob 的锁）

    消息格式（每行一个 JSON）：
    {"type": "message", "from": "lead", "content": "请开始工作", "timestamp": 1711123456.789}
    """

    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)  # 启动时确保 inbox 目录存在
        self._locks: dict[str, threading.Lock] = {}   # name → Lock，每个收件箱一把锁
        self._meta_lock = threading.Lock()             # 保护 _locks 字典本身的并发访问（创建新锁时）

    def _get_lock(self, name: str) -> threading.Lock:
        """获取指定收件箱的锁，不存在则惰性创建。

        使用双重检查锁定（Double-Checked Locking）模式：
        1. 先无锁检查 _locks 字典（快速路径，绝大多数调用走这里）
        2. 不存在时才加 _meta_lock 创建新锁
        3. 加锁后再检查一次，防止两个线程同时通过第一步检查
        """
        if name not in self._locks:
            with self._meta_lock:
                if name not in self._locks:
                    self._locks[name] = threading.Lock()
        return self._locks[name]

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        """向收件人的 JSONL 文件追加一条消息。

        Args:
            sender:   发送者名字（lead 或队友名）
            to:       收件人名字（会映射到 inbox/{to}.jsonl 文件）
            content:  消息正文
            msg_type: 消息类型，必须在 VALID_MSG_TYPES 中
            extra:    可选的扩展字段字典（如 plan_id），直接合并到消息 JSON 中

        并发安全：按收件人加锁（_get_lock(to)），确保：
        - 多个线程同时给同一个人发消息时，追加操作不会交错
        - send 和 read_inbox 操作同一个收件箱时互斥
        """
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),  # Unix 时间戳，方便排序和调试
        }
        if extra:              # 扩展字段（如 plan_id），直接合并到消息对象中
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"  # 收件人的邮箱文件
        with self._get_lock(to):               # 按收件人加锁，不同收件箱互不阻塞
            with open(inbox_path, "a") as f:    # 追加模式写入
                f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        """读取并清空收件箱（drain-on-read 语义）。

        关键设计：读取和清空是在同一把锁内完成的原子操作。
        如果不加锁，可能出现：
          1. read_inbox 读取文件内容
          2. 另一个线程的 send() 追加了新消息
          3. read_inbox 清空文件 → 第 2 步的消息丢失了

        返回值：消息列表（可能为空），每条消息是一个 dict
        副作用：收件箱文件被清空（write_text("")）
        """
        inbox_path = self.dir / f"{name}.jsonl"
        with self._get_lock(name):             # 与 send(to=name) 互斥
            if not inbox_path.exists():        # 从未收到过消息
                return []
            messages = []
            for line in inbox_path.read_text().strip().splitlines():  # 逐行解析 JSONL
                if line:
                    messages.append(json.loads(line))
            inbox_path.write_text("")  # drain：清空文件，防止重复消费
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        """群发消息给所有队友（跳过发送者自己）。

        实现方式：遍历队友列表，逐个调用 send()。
        每次 send() 都会独立加锁，所以不同队友的收件箱可以并行写入。
        消息类型固定为 "broadcast"，让队友能区分这是群发还是定向消息。
        """
        count = 0
        for name in teammates:
            if name != sender:  # 不给自己发（lead broadcast 时跳过 lead 自己）
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# 全局消息总线单例
# 所有地方（lead 的 TOOL_HANDLERS、队友的 _exec、agent_loop 的 inbox drain）都使用同一个实例
BUS = MessageBus(INBOX_DIR)


# ============================================================
# s10 协议处理函数（Lead 端）
# ============================================================

def handle_shutdown_request(teammate: str) -> str:
    """Lead 发起关机请求。生成 request_id，记录到 shutdown_requests 跟踪器，
    并通过 MessageBus 发送 shutdown_request 消息给目标队友。
    队友收到后可以通过 shutdown_response 工具回复 approve/reject。"""
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    """Lead 审批队友提交的计划。通过 request_id 关联到原始请求，
    更新 plan_requests 跟踪器状态，并发送审批结果给队友。"""
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"


def check_shutdown_status(request_id: str) -> str:
    """Lead 查看关机请求的当前状态（pending/approved/rejected）。"""
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))


def args_approve(input_dict: dict) -> bool:
    """从工具参数中提取 approve 字段。用于 _teammate_loop 中检测 shutdown_response。"""
    return input_dict.get("approve", False)


# ============================================================
# s11 自治任务板扫描与认领
# ============================================================
#
# 核心理念："The agent finds work itself."（队友自己找活干）
#
# 传统模式：lead 分配任务 → 队友执行 → 完成后等待新指令
# s11 模式：lead 分配任务 → 队友执行 → 完成后主动扫描任务板 → 找到新任务自动认领
#
# 认领条件（三个都满足才算"可认领"）：
#   1. status == "pending"    — 任务尚未开始
#   2. owner 为空             — 没有人认领
#   3. blockedBy 为空         — 没有前置依赖阻塞

def scan_unclaimed_tasks() -> list:
    """扫描任务板，返回所有可自动认领的任务列表。
    按文件名排序确保多个队友看到的顺序一致，配合 _claim_lock 实现先到先得。"""
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and not task.get("blockedBy")):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id: int, owner: str) -> str:
    """原子性地认领一个任务：设置 owner 和 status=in_progress。

    使用 _claim_lock 确保并发安全：
    - 队友 A 和队友 B 同时看到 task_1 未认领
    - A 先拿到锁，认领成功
    - B 拿到锁后发现 owner 已设置，认领失败
    """
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"Error: Task {task_id} not found"
        task = json.loads(path.read_text())
        if task.get("owner"):
            return f"Error: Task {task_id} already claimed by {task['owner']}"
        task["owner"] = owner
        task["status"] = "in_progress"
        path.write_text(json.dumps(task, indent=2))
    return f"Claimed task #{task_id} for {owner}"


def make_identity_block(name: str, role: str, team_name: str) -> dict:
    """生成身份重注入消息块（s11）。

    使用场景：队友在 IDLE 阶段自动认领新任务时，如果对话历史很短
    （len(messages) <= 3），说明之前的对话可能经历了压缩，LLM 可能忘记了
    自己的身份。此时在对话开头插入 identity_block，确保 LLM 记住：
      - 自己叫什么名字（name）
      - 自己的角色是什么（role）
      - 属于哪个团队（team_name）

    格式：用 <identity> XML 标签包裹，与普通用户消息区分。
    配合 messages 中紧随其后的 assistant 伪回复 "I am {name}. Continuing."
    形成完整的 user-assistant 对，满足 Claude API 交替要求。
    """
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


class TeammateManager:
    """持久化队友管理器。维护团队名册（config.json）并在线程中运行队友 agent loop。

    队友生命周期：
      spawn()     → status="working"  → 线程启动，进入 _teammate_loop
      正常结束    → status="idle"     → 可被再次 spawn（允许换角色）
      主动关机    → status="shutdown" → 不会被自动改为 idle

    与 s04 子智能体（subagent）的本质区别：
      子智能体：run_subagent() 同步调用，阻塞 lead，用完即弃，共享 TOOL_HANDLERS
      队友：    threading.Thread 异步执行，不阻塞 lead，有独立身份和持久状态
              通过 MessageBus 通信，有自己的工具集（_teammate_tools）

    线程模型：
      每个队友在一个 daemon=True 的线程中运行
      daemon=True 意味着主进程退出时线程自动终止（不会卡住）
      线程之间通过文件系统（MessageBus）通信，无内存共享
    """

    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"  # 团队名册文件路径
        self.config = self._load_config()             # 从磁盘恢复上次的名册（支持跨会话）
        self.threads = {}                             # name → Thread，仅用于本次会话的线程跟踪

    def _load_config(self) -> dict:
        """从磁盘加载团队名册。首次运行时返回空名册模板。
        名册格式：{"team_name": "default", "members": [{"name": ..., "role": ..., "status": ...}, ...]}
        """
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        """将当前名册写入磁盘（config.json），确保跨会话持久化。"""
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        """在名册中按名字查找队友。
        返回值是 list 中元素的引用（不是拷贝），修改返回值会直接修改名册。
        这是有意为之的设计，方便 spawn() 和 _teammate_loop() 直接更新状态。
        """
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        """更新队友状态并持久化到 config.json（s11 新增）。
        在空闲循环中频繁切换状态（working ↔ idle），
        抽成方法避免重复代码。"""
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """创建队友并在独立线程中启动 agent loop。

        Args:
            name:   队友名字（唯一标识，也是收件箱文件名：inbox/{name}.jsonl）
            role:   角色描述（如 "researcher"、"code reviewer"），会写入 system prompt
            prompt: 初始任务描述，作为队友 agent loop 的第一条 user 消息

        状态转换：
            (不存在) → working  — 全新队友，加入名册并启动
            idle     → working  — 空闲队友被重新激活（可换角色）
            shutdown → working  — 已关机的队友被重新激活
            working  → Error    — 正在工作的队友不能重复 spawn

        返回值：成功返回确认消息，失败返回 Error 字符串
        """
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            # idle 或 shutdown 的队友可以重新激活，允许换角色
            member["status"] = "working"
            member["role"] = role
        else:
            # 全新队友，加入名册
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()  # 持久化到 config.json

        # 在独立 daemon 线程中启动队友的 agent loop
        # daemon=True 确保主进程退出时线程不会阻塞
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()  # 非阻塞：立即返回，lead 可以继续工作
        return f"Spawned '{name}' (role: {role})"

    def _teammate_loop(self, name: str, role: str, prompt: str):
        """队友的完整 agent loop，带自治空闲循环（s11 增强）。

        s11 之前的生命周期（s10）：
          spawn → WORK（最多 50 轮）→ idle/shutdown → 结束

        s11 的生命周期（自治循环）：
          spawn → WORK → IDLE → (resume WORK | shutdown)
                    ↑______|

        WORK 阶段（内循环，最多 50 轮 LLM 调用）：
          - 每轮开头 drain 收件箱，接收消息
          - 遇到 shutdown_request → 立即关机（快速路径）
          - 队友调用 idle 工具 → 跳出到 IDLE 阶段
          - 队友批准 shutdown_response → 标记退出
          - LLM 返回纯文本（无工具调用）→ 跳出到 IDLE 阶段

        IDLE 阶段（s11 核心机制）：
          每隔 POLL_INTERVAL 秒检查：
            1. 收件箱 → 有消息则恢复 WORK
            2. 任务板 → 有未认领任务则自动认领，恢复 WORK
          超过 IDLE_TIMEOUT 秒无工作 → 自动关机

        身份重注入（s11 新增）：
          自动认领任务后，如果对话历史很短（≤3 条），说明经历了压缩。
          此时在对话开头插入 identity_block，确保 LLM 记住自己是谁。

        关键设计决策：
          - idle 工具不走 _exec，直接标记 idle_requested（避免副作用）
          - shutdown_request 在 inbox drain 时直接处理（快速路径，不等 LLM 决定）
          - should_exit 标志延迟一轮生效（让 LLM 看到 shutdown_response 的结果）
        """
        team_name = self.config["team_name"]
        # s11: system prompt 增加 "Use idle tool" 和 "auto-claim" 提示
        # 告诉队友它有自治能力：没活干时用 idle 工具进入空闲，系统会自动分配新任务
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {Path.cwd()}. "
            f"Use idle tool when you have no more work. You will auto-claim new tasks. "
            f"Submit plans via plan_approval before major work. "
            f"Respond to shutdown_request with shutdown_response."
        )
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()

        # ===== 外循环：WORK → IDLE → WORK → ... =====
        # s11 核心改造：从单次 for 循环变为 while True 自治循环
        while True:
            # ===== WORK 阶段：标准 agent loop =====
            should_exit = False    # s10 协议：队友批准 shutdown 后设置
            idle_requested = False  # s11 新增：队友调用 idle 工具后设置

            for _ in range(50):  # 硬性上限：每次 WORK 阶段最多 50 轮 LLM 调用
                # -- 收件箱检查 --
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    # s11: shutdown_request 快速路径
                    # 不等 LLM 决定是否 approve，直接关机
                    # 与 s10 的 shutdown_response 协议互补：
                    #   快速路径：inbox drain 时发现 shutdown_request → 立即退出
                    #   协议路径：LLM 收到 shutdown_request → 调用 shutdown_response → 延迟退出
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})

                # s10: 如果上一轮已经批准了 shutdown，在处理完收件箱后退出
                if should_exit:
                    break

                # -- LLM 调用 --
                try:
                    response = client.messages.create(
                        model=MODEL,
                        system=sys_prompt,
                        messages=messages,
                        tools=tools,
                        max_tokens=8000,
                    )
                except Exception:
                    # API 异常（网络错误、限流等）→ 安全退出整个循环
                    self._set_status(name, "idle")
                    return

                messages.append({"role": "assistant", "content": response.content})

                # 模型返回纯文本（不再调用工具）→ 当前工作完成，跳出到 IDLE 阶段
                if response.stop_reason != "tool_use":
                    break

                # -- 工具执行 --
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            # s11: idle 工具不走 _exec，直接标记进入空闲阶段
                            # 设计原因：idle 是状态转换信号，不是真正的"工具"
                            idle_requested = True
                            output = "Entering idle phase. Will poll for new tasks."
                        else:
                            output = self._exec(name, block.name, block.input)
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output),
                        })
                        # s10: 队友批准 shutdown 后标记退出
                        if block.name == "shutdown_response" and args_approve(block.input):
                            should_exit = True
                messages.append({"role": "user", "content": results})

                # s11: idle 工具被调用 → 跳出 WORK 阶段，进入 IDLE 阶段
                if idle_requested:
                    break

            # -- WORK 阶段结束判定 --
            # should_exit=True → 队友主动批准了关机，不进入 IDLE 阶段
            if should_exit:
                self._set_status(name, "shutdown")
                return

            # ===== IDLE 阶段：轮询任务板和收件箱 =====
            # s11 核心："The agent finds work itself."
            # 队友不是被动等待 lead 分配，而是主动寻找工作
            self._set_status(name, "idle")
            resume = False
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)  # 总轮询次数（60/5=12 次）

            for _ in range(polls):
                time.sleep(POLL_INTERVAL)

                # -- 检查 1: 收件箱 --
                # 有人给我发消息了吗？（可能是新任务指令、shutdown 请求等）
                inbox = BUS.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break

                # -- 检查 2: 任务板 --
                # .tasks/ 目录中有没有未认领的任务？
                unclaimed = scan_unclaimed_tasks()
                if unclaimed:
                    task = unclaimed[0]  # 取第一个（按文件名排序）
                    claim_task(task["id"], name)
                    task_prompt = (
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    # -- s11 身份重注入 --
                    # 如果对话历史很短（≤3 条消息），可能经历了压缩
                    # LLM 可能已经忘记自己的名字和角色
                    # 在对话开头插入 identity_block + 伪 assistant 回复
                    # 这样 LLM 重新"记住"自己是谁
                    if len(messages) <= 3:
                        messages.insert(0, make_identity_block(name, role, team_name))
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                    messages.append({"role": "user", "content": task_prompt})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break

            # -- IDLE 阶段结束判定 --
            if not resume:
                # 轮询超时：连续 IDLE_TIMEOUT 秒没找到任何工作 → 自动关机
                self._set_status(name, "shutdown")
                return

            # 有新工作 → 回到 WORK 阶段
            self._set_status(name, "working")

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        """队友的工具分发器。

        为什么不直接复用 lead 的 TOOL_HANDLERS？
        因为 send_message 和 read_inbox 需要知道"谁在调用"：
        - lead 的 TOOL_HANDLERS 中 sender 硬编码为 "lead"
        - 队友的 _exec 中 sender 是队友自己的名字
        这样每个队友发消息时，收件人能看到是谁发的（msg["from"] 字段）

        Args:
            sender:    调用者名字（自动绑定，队友自己不需要指定）
            tool_name: 工具名称
            args:      工具参数（来自 LLM 的 tool_use block）
        """
        if tool_name == "bash":
            return run_bash(args["command"])
        if tool_name == "read_file":
            return read_file(args["path"], args.get("limit"))
        if tool_name == "write_file":
            return write_file(args["path"], args["content"])
        if tool_name == "edit_file":
            return edit_file(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            # sender 自动绑定为当前队友名字，队友不能伪装成别人发消息
            return BUS.send(sender, args["to"], args["content"],
                            args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            # 只能读自己的收件箱（sender），不能窥探别人的邮箱
            return json.dumps(BUS.read_inbox(sender), indent=2)
        # -- s10 协议工具 --
        if tool_name == "shutdown_response":
            # 队友回复 lead 的 shutdown 请求
            req_id = args["request_id"]
            approve = args["approve"]
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"
            # 通过邮箱通知 lead 审批结果
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": approve},
            )
            return f"Shutdown {'approved' if approve else 'rejected'}"
        if tool_name == "plan_approval":
            # 队友提交计划给 lead 审批
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            # 通过邮箱发送计划给 lead
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            return f"Plan submitted (request_id={req_id}). Waiting for lead approval."
        # -- s11 新增工具 --
        if tool_name == "claim_task":
            # 队友手动认领任务（补充自动认领：LLM 主动选择要做哪个任务）
            return claim_task(args["task_id"], sender)
        # idle 工具在 _teammate_loop 中特殊处理，不会走到 _exec
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        """队友的工具 schema 定义（10 个工具，s11 从 8 个增加到 10 个）。

        工具集对比：
          Lead（PARENT_AGENT_TOOLS）: 基础 4 + todo + load_skill + compact + task 4
                                      + subagent + background 2 + 团队 8 + s11 2 = 23 个
          队友（_teammate_tools）:     基础 4 + 通信 2 + 协议 2 + s11 2 = 10 个

        s11 新增的 2 个工具：
          - idle:       队友主动宣告"我没活了"，触发 IDLE 轮询阶段
          - claim_task: 队友手动认领任务板上的任务（补充自动认领）

        队友没有的工具（及原因）：
          - spawn_teammate:   只有 lead 能创建队友（防止队友无限繁殖）
          - broadcast:        只有 lead 能群发（层级通信模型）
          - list_teammates:   队友不需要知道全局团队结构
          - shutdown_request: 只有 lead 能发起关机请求
          - todo/task_*:      队友任务简单，不需要复杂的任务管理
          - compact:          队友生命周期短（最多 50 轮），不需要压缩
          - background_run:   队友本身就是后台运行的
        """
        return [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message to a teammate.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},
            # -- s10 协议工具 --
            {"name": "shutdown_response", "description": "Respond to a shutdown request. Approve to shut down, reject to keep working.",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            {"name": "plan_approval", "description": "Submit a plan for lead approval. Provide plan text.",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
            # -- s11 新增工具 --
            {"name": "idle", "description": "Signal that you have no more work. Enters idle polling phase where you will auto-claim new tasks.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "Claim a task from the task board by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]

    def list_all(self) -> str:
        """列出所有队友及状态。被两个地方调用：
        1. /team 调试命令（直接打印，不经过 LLM）
        2. list_teammates 工具（通过 TOOL_HANDLERS，LLM 可见）
        """
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        """返回所有队友名字列表，供 broadcast() 遍历使用。"""
        return [m["name"] for m in self.config["members"]]


# 全局队友管理器单例
TEAM = TeammateManager(TEAM_DIR)
# ============================================================
# 上下文管理（三层压缩策略）
# ============================================================
# Layer 1: micro_compact — 每轮执行，替换旧 tool_result 为占位符
# Layer 2: auto_compact  — token 超阈值时，LLM 摘要压缩整个对话
# Layer 3: manual compact — LLM 主动调用 compact 工具触发压缩

KEEP_RECENT = 3  # micro_compact 保留最近 N 个 tool_result 不压缩
TRANSCRIPT_DIR = Path.cwd() / ".transcripts"  # auto_compact 前保存完整对话的目录


def estimate_tokens(messages: list) -> int:
    """粗略估算 token 数：将整个 messages 序列化为字符串，除以 4。
    精确度不高但够用，避免引入 tokenizer 依赖。"""
    return len(str(messages)) // 4


def micro_compact(messages: list):
    """Layer 1 上下文压缩：替换旧的 tool_result 为短占位符。

    策略：每轮都运行，只保留最近 KEEP_RECENT 个 tool_result 完整，
    更早的 tool_result 如果超过 100 字符就替换为 "[Previous: used {tool_name}]"。
    这样 LLM 仍然知道之前用了什么工具，但不会被大量输出填满上下文。
    """
    # Collect all tool_result entries with their positions
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))
    if len(tool_results) <= KEEP_RECENT:
        return
    # Build tool_use_id -> tool_name map from assistant messages
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name
    # Replace old results with placeholders
    for _, _, result in tool_results[:-KEEP_RECENT]:
        if isinstance(result.get("content"), str) and len(result["content"]) > 100:
            tool_id = result.get("tool_use_id", "")
            tool_name = tool_name_map.get(tool_id, "unknown")
            result["content"] = f"[Previous: used {tool_name}]"

def auto_compact(messages: list) -> list:
    """Layer 2 上下文压缩：先保存完整对话到磁盘，再用 LLM 生成摘要替换全部历史。

    流程：
    1. 将完整 messages 以 JSONL 格式保存到 .transcripts/ 目录（保留现场）
    2. 将对话文本截断到 80K 字符，发给 LLM 请求摘要
    3. 用 [压缩摘要 + 虚拟 assistant 回复] 替换原来的所有 messages

    返回值：压缩后的 messages 列表（只有 2 条消息）
    """
    # Save full transcript
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[transcript saved: {transcript_path}]")
    # Ask LLM to summarize
    conversation_text = json.dumps(messages, default=str)[:80000]
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content":
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details.\n\n" + conversation_text}],
        max_tokens=2000,
    )
    summary = next((b.text for b in response.content if hasattr(b, "text")), "No summary generated.")
    # Replace all messages with compressed summary
    return [
        {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
        {"role": "assistant", "content": "Understood. I have the context from the summary. Continuing."},
    ]


# ============================================================
# 子智能体（s04，与 s10 队友不同的并行模型）
# ============================================================
# 子智能体：同步阻塞调用，共享 TOOL_HANDLERS，用完即弃，不能通信
# 队友：    异步线程，独立工具集，有持久身份，通过 MessageBus 通信
SUBAGENT_SYSTEM = f"""You are a coding subagent at {os.getcwd()}.
Complete the given task, then summarize your findings."""

# ============================================================
# 工具注册表 — Lead 的工具分发
# ============================================================
# 每个 key 是工具名，value 是对应的 handler 函数
# LLM 返回 tool_use block 后，agent_loop 通过 TOOL_HANDLERS[block.name] 找到 handler 并调用
# **_ 用于忽略 LLM 传入的额外参数（容错处理）
TOOL_HANDLERS = {
    "bash": lambda command, **_:run_bash(command),
    "read_file": lambda path, limit=None, **_:read_file(path, limit),
    "write_file": lambda path, content, **_:write_file(path, content),
    "edit_file": lambda path, old_text, new_text, **_:edit_file(path, old_text, new_text),
    "todo": lambda items, **_:TODO.update(items),
    "task": lambda prompt, **_:run_subagent(prompt),
    "load_skill": lambda name, **_:SKILL_LOADER.get_content(name),
    "compact": lambda **_:"Manual compression requested.",
    "task_create": lambda subject, description="", **_: TASKS.create(subject, description),
    "task_update": lambda task_id, status=None, addBlockedBy=None, addBlocks=None, **_: TASKS.update(task_id, status, addBlockedBy, addBlocks),
    "task_list": lambda **_: TASKS.list_all(),
    "task_get": lambda task_id, **_: TASKS.get(task_id),
    "background_run": lambda command, **_: BG.run(command),
    "check_background": lambda task_id=None, **_: BG.check(task_id),
    # -- 团队工具（s10 新增，lead 视角）--
    # 注意：这里所有 sender 都硬编码为 "lead"
    # 队友的 sender 绑定在 TeammateManager._exec() 中处理
    "spawn_teammate": lambda name, role, prompt, **_: TEAM.spawn(name, role, prompt),  # 创建队友线程
    "list_teammates": lambda **_: TEAM.list_all(),                                       # 查看团队名册
    "send_message": lambda to, content, msg_type="message", **_: BUS.send("lead", to, content, msg_type),  # lead → 队友
    "read_inbox": lambda **_: json.dumps(BUS.read_inbox("lead"), indent=2),              # 读 lead 自己的收件箱
    "broadcast": lambda content, **_: BUS.broadcast("lead", content, TEAM.member_names()),  # lead → 全体队友
    # -- s10 协议工具（lead 端）--
    "shutdown_request": lambda teammate, **_: handle_shutdown_request(teammate),      # lead 发起关机请求
    "shutdown_response": lambda request_id="", **_: check_shutdown_status(request_id),  # lead 查看关机状态
    "plan_approval": lambda request_id, approve, feedback="", **_: handle_plan_review(request_id, approve, feedback),  # lead 审批计划
    # -- s11 新增工具 --
    "idle": lambda **_: "Lead does not idle.",                   # lead 不需要空闲循环（由用户驱动）
    "claim_task": lambda task_id, **_: claim_task(task_id, "lead"),  # lead 也可以手动认领任务
}

# ============================================================
# 工具 Schema 定义 — 告诉 LLM 有哪些工具可用
# ============================================================
# Claude API 要求用 JSON Schema 格式描述每个工具的 name、description、input_schema
# LLM 看到这些定义后会选择合适的工具并生成参数

# 子智能体和队友的工具集（基础工具 + todo + skill + compact + task 管理）
CHILD_AGENT_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "todo", "description": "Update task list. Track progress on multi-step tasks.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "text", "status"]}}}, "required": ["items"]}},
    {"name": "load_skill", "description": "Load specialized knowledge by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "Skill name to load"}}, "required": ["name"]}},
    {"name": "compact", "description": "Trigger manual conversation compression to free up context space.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "What to preserve in the summary"}}}},
    {"name": "task_create", "description": "Create a new persistent task (survives compression/restart).",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    {"name": "task_update", "description": "Update a task's status or dependencies. Completing a task auto-unblocks dependents.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "addBlocks": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    {"name": "task_list", "description": "List all tasks with status and dependency info.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "task_get", "description": "Get full details of a task by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]

# Lead 的完整工具集 = 子智能体工具 + subagent + background + 团队管理 5 个
# 使用列表拼接（+）而非重新定义，确保基础工具定义只维护一份
PARENT_AGENT_TOOLS = CHILD_AGENT_TOOLS + [
    {"name": "task", "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
    {"name": "background_run", "description": "Run a shell command in the background (non-blocking). Returns a task ID immediately.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command to run in background"}}, "required": ["command"]}},
    {"name": "check_background", "description": "Check status and output of background tasks. Pass task_id for details, or omit for overview.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "description": "Background task ID to check (omit for all)"}}}},
    # -- 团队工具 schema 定义（s10 新增）--
    # 这 5 个工具只出现在 PARENT_AGENT_TOOLS 中（仅 lead 可用）
    # 队友的工具集在 TeammateManager._teammate_tools() 中单独定义
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate that runs its own agent loop in a thread.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates with name, role, status.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate's inbox.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    # -- s10 协议工具 schema（仅 lead 可用）--
    {"name": "shutdown_request", "description": "Request a teammate to shut down gracefully. Returns a request_id for tracking.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "Check the status of a shutdown request by request_id.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan. Provide request_id + approve + optional feedback.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    # -- s11 新增工具 schema --
    {"name": "idle", "description": "Enter idle state (for lead -- rarely used).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "Claim a task from the board by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]

TODO = None  # global singleton, initialized below

TASKS_DIR = Path.cwd() / ".tasks"


# ============================================================
# 持久化任务管理 (s07)
# ============================================================

class TaskManager:
    """持久化任务 DAG（有向无环图）。每个任务是 .tasks/ 目录下的一个 JSON 文件。

    与 TodoManager 的区别：
    - TaskManager：持久化到磁盘，支持依赖关系，能跨会话和压缩存活
    - TodoManager：纯内存，无依赖，会话结束即丢失
    """

    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _load(self, task_id: int) -> dict:
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())

    def _save(self, task: dict):
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id, "subject": subject, "description": description,
            "status": "pending", "blockedBy": [], "blocks": [], "owner": "",
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2)

    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2)

    def update(self, task_id: int, status: str = None,
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        task = self._load(task_id)
        if status:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            if status == "completed":
                self._clear_dependency(task_id)
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
            for blocked_id in add_blocks:
                try:
                    blocked = self._load(blocked_id)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
                except ValueError:
                    pass
        self._save(task)
        return json.dumps(task, indent=2)

    def _clear_dependency(self, completed_id: int):
        """Remove completed_id from all other tasks' blockedBy lists."""
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def list_all(self) -> str:
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text()))
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
        return "\n".join(lines)


TASKS = TaskManager(TASKS_DIR)

# ============================================================
# 技能系统 (s05)
# ============================================================

class SKILL_LOADER:
    """从 skills/ 目录加载 SKILL.md 文件，提供技能目录和按需加载功能。

    技能文件格式（SKILL.md）：
      ---
      name: git-expert
      description: Git 高级操作指南
      tags: git, version-control
      ---
      （技能正文，Markdown 格式）

    使用方式：
    1. 启动时 _load_all() 扫描所有 SKILL.md，解析 frontmatter
    2. get_descriptions() 返回技能目录（注入 system prompt，让 LLM 知道有哪些技能）
    3. LLM 调用 load_skill 工具时，get_content() 返回技能正文
    """

    def __init__(self, skills_dir: Path):
        print(f"Loading skills from {skills_dir}")
        self.skills = {}
        self.skills_dir = skills_dir
        self._load_all()
    
    def _load_all(self):
        if not self.skills_dir.exists():
            print(f"Skills directory {self.skills_dir} does not exist")
            return
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text()
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}

    
    def _parse_frontmatter(self, text: str) -> tuple:
        """Parse YAML frontmatter between --- delimiters."""
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        meta = {}
        for line in match.group(1).strip().splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                meta[key.strip()] = val.strip()
        return meta, match.group(2).strip()
    
    def get_descriptions(self) -> str:
        if not self.skills:
            print("No skills loaded")
            return "(no skills available)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)
    
    def get_content(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"

def run_subagent(prompt: str) -> str:
    """同步运行一次性子智能体（s04）。阻塞 lead 直到完成。最多 30 轮 LLM 调用。"""
    sub_messages = [{"role": "user", "content": prompt}]
    for _ in range(30):
        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_messages,
            tools=CHILD_AGENT_TOOLS, max_tokens=8000,
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        sub_messages.append({"role": "user", "content": results})
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"


# ============================================================
# 内存待办列表 (s02)
# ============================================================

class TodoManager:
    """轻量级内存待办列表。会话结束即丢失，适合短期任务跟踪。
    限制：最多 20 条，同时只能有 1 条 in_progress。"""

    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        if len(items) > 20:
            raise ValueError("Max 20 todos allowed")
        validated, in_progress_count = [], 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

# ============================================================
# 后台任务管理 (s08)
# ============================================================

class BackgroundManager:
    """非阻塞命令执行器。在 daemon 线程中运行 shell 命令，完成后推送通知。

    与队友线程的区别：
    - BackgroundManager：运行 shell 命令，无 LLM 推理能力
    - TeammateManager：  运行完整 agent loop，有 LLM 推理能力

    通知机制：
    - 命令完成后将结果推入 _notifications 队列
    - agent_loop 每轮开头调用 drain_notifications() 取出通知注入上下文
    """

    def __init__(self, timeout: int = 300):
        self.timeout = timeout                         # 单个命令最大执行时间（秒）
        self._tasks: dict[str, dict] = {}              # task_id → 任务状态字典
        self._notifications: queue.Queue = queue.Queue()  # 完成通知队列
        self._lock = threading.Lock()                  # 保护 _tasks 字典的并发访问

    def run(self, command: str) -> str:
        task_id = uuid.uuid4().hex[:8]
        task = {
            "id": task_id, "command": command,
            "status": "running", "output": "", "start_time": time.time(),
        }
        with self._lock:
            self._tasks[task_id] = task
        t = threading.Thread(target=self._execute, args=(task_id, command), daemon=True)
        t.start()
        return f"Background task {task_id} started: {command}"

    def _execute(self, task_id: str, command: str):
        try:
            r = subprocess.run(
                command, shell=True, cwd=os.getcwd(),
                capture_output=True, text=True, timeout=self.timeout,
            )
            output = (r.stdout + r.stderr).strip() or "(no output)"
            status = "completed"
        except subprocess.TimeoutExpired:
            output = f"Error: Timeout ({self.timeout}s)"
            status = "error"
        except Exception as e:
            output = f"Error: {e}"
            status = "error"
        with self._lock:
            self._tasks[task_id]["status"] = status
            self._tasks[task_id]["output"] = output
        # Push truncated notification to queue
        preview = output[:500] + ("..." if len(output) > 500 else "")
        self._notifications.put(
            f"[bg:{task_id}] `{command}` → {status}\n{preview}"
        )

    def check(self, task_id: str = None) -> str:
        with self._lock:
            if task_id:
                task = self._tasks.get(task_id)
                if not task:
                    return f"Error: No background task with id {task_id}"
                elapsed = time.time() - task["start_time"]
                return (
                    f"id: {task_id}\ncommand: {task['command']}\n"
                    f"status: {task['status']}\nelapsed: {elapsed:.1f}s\n"
                    f"output:\n{task['output'][:50000]}"
                )
            if not self._tasks:
                return "No background tasks."
            lines = []
            for tid, t in self._tasks.items():
                elapsed = time.time() - t["start_time"]
                lines.append(f"  {tid}: [{t['status']}] {t['command']} ({elapsed:.1f}s)")
            return "\n".join(lines)

    def drain_notifications(self) -> list[str]:
        results = []
        while not self._notifications.empty():
            try:
                results.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        return results


BG = BackgroundManager()


# ============================================================
# 基础工具实现
# ============================================================

def safe_path(path: str) -> Path:
    """路径安全检查：确保解析后的路径不会逃逸出工作目录。
    例如 "../../../etc/passwd" 会被拦截。"""
    path = (Path(os.getcwd()) / path).resolve()
    if not path.is_relative_to(Path(os.getcwd())):
        raise ValueError(f"Path escapes workspace: {path}")
    return path

def run_bash(command: str) -> str:
    """执行 shell 命令并返回 stdout+stderr。有基础的危险命令黑名单。"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def read_file(path: str, limit: int = None) -> str:
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def write_file(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def edit_file(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        text = fp.read_text()
        if text.count(old_text) == 0:
            return f"Error: old_text not found in {path}"
        if text.count(old_text) > 1:
            return "Error: old_text matches multiple locations, provide more context to make it unique"
        fp.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

# ============================================================
# 全局单例初始化
# ============================================================
# 这些单例在模块加载时创建，整个进程共享
# 注意初始化顺序：SKILL_LOADER 最后，因为它在 __init__ 中读磁盘并打印日志

TODO = TodoManager()                        # 内存待办列表
SKILLS_DIR = Path(os.getcwd()) / "skills"   # 技能文件目录
SKILL_LOADER = SKILL_LOADER(SKILLS_DIR)     # 技能加载器（启动时扫描 skills/）

