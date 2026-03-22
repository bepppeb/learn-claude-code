#!/usr/bin/env python3
"""
s01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.

s10 (Team) 扩展：
- Lead agent 在每轮循环前自动 drain 收件箱和后台任务通知
- 队友消息通过 <inbox> 标签注入上下文，与普通用户输入区分
- 后台任务完成通知通过 <background-results> 标签注入
- 新增 /team 和 /inbox 调试命令，方便观察团队状态

整体架构：
    +---------+
    |  User   |
    +----+----+
         |
    +----v----+  spawn_teammate   +------------+
    |  Lead   | ───────────────> |  Teammate   |
    |  Agent  | <───────────────  |  (thread)   |
    +---------+  send_message     +------------+
         |           ^
         v           |
    +----+----+      |
    | Message  |-----+
    |   Bus    |  (JSONL-based inboxes)
    +---------+
"""

import json
import os
import readline  # noqa: F401 — enables arrow keys and history in input()

import tools
from settings import client, MODEL

# -- System Prompt --
# s10: 角色从 "coding agent" 升级为 "team lead"
# 关键设计决策：
# 1. 告诉 LLM 它是团队领导，让它知道可以分配工作给队友
# 2. 列出所有可用技能，让 LLM 能选择 load_skill 来获取专业知识
# 3. 通信三件套：send_message（点对点）、read_inbox（收件箱）、broadcast（群发）
SYSTEM = f"""You are a team lead at {os.getcwd()}.
Use task_create/task_update/task_list for multi-step work — tasks persist to disk and survive compression.
Use the todo tool for quick in-memory checklists within a single session.
Prefer tools over prose.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.
Spawn teammates for parallel work. Communicate via send_message/read_inbox/broadcast.

Skills available:
{tools.SKILL_LOADER.get_descriptions()}
"""



def agent_loop(messages: list):
    """Lead agent 的主循环。每轮执行 5 个阶段：
    1. micro_compact  — 替换旧的 tool_result 为占位符，节省 token
    2. auto_compact   — 若 token 估算超阈值，LLM 摘要压缩整个对话
    3. inbox drain    — 读取队友发来的消息，注入上下文（s10 新增）
    4. bg drain       — 读取后台任务完成通知，注入上下文（s08 新增）
    5. LLM call + tool execution — 标准 agent loop
    """
    rounds_since_todo = 0
    while True:
        # ===== 阶段 1: micro_compact =====
        # 替换旧的 tool_result 为占位符，只保留最近 KEEP_RECENT 个
        tools.micro_compact(messages)

        # ===== 阶段 2: auto_compact =====
        # 粗略估算 token 数（4 字符≈1 token），超阈值时用 LLM 摘要替换全部历史
        if tools.estimate_tokens(messages) > tools.THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = tools.auto_compact(messages)

        # ===== 阶段 3: inbox drain (s10) =====
        # 队友通过 MessageBus 发消息给 "lead"，这里每轮开头统一收取
        # 设计要点：
        #   - 用 <inbox> XML 标签包裹，让 LLM 区分这是队友消息而非用户输入
        #   - 追加一条伪造的 assistant 回复 "Noted inbox messages."
        #     原因：Claude API 要求 user/assistant 严格交替，连续两个 user 消息会报错
        #   - drain-on-read：read_inbox 会清空收件箱，避免同一条消息被重复注入
        inbox = tools.BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
            messages.append({
                "role": "assistant",
                "content": "Noted inbox messages.",
            })

        # ===== 阶段 4: background task drain (s08) =====
        # 后台任务（background_run）完成后将通知推入队列
        # 这里每轮开头统一取出，注入上下文让 LLM 知道结果
        notifications = tools.BG.drain_notifications()
        if notifications:
            note_text = "<background-results>\n" + "\n---\n".join(notifications) + "\n</background-results>"
            messages.append({"role": "user", "content": note_text})
            messages.append({"role": "assistant", "content": "Noted. Background tasks completed."})
            print(f"[background: {len(notifications)} notification(s) injected]")
        # ===== 阶段 5: LLM 调用 + 工具执行 =====
        # Lead 使用 PARENT_AGENT_TOOLS（完整工具集），包含团队管理工具
        # 队友只有 CHILD_AGENT_TOOLS 的子集（见 _teammate_tools）
        response = client.messages.create(messages=messages, model=MODEL, system=SYSTEM, tools=tools.PARENT_AGENT_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})

        # stop_reason != "tool_use" 表示模型选择了纯文本回复，本轮结束
        if response.stop_reason != "tool_use":
            return

        # -- 工具执行阶段 --
        results = []
        used_todo = False
        manual_compact = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    # compact 工具不走 handler，直接标记，在本轮结束后统一处理
                    manual_compact = True
                    output = "Compressing..."
                else:
                    # bash 命令特殊处理：打印黄色的命令行（方便用户观察）
                    if "command" in block.input:
                        print(f"\033[33m$ {block.input['command']}\033[0m")
                    # 从 TOOL_HANDLERS 字典查找对应的处理函数
                    # s10 新增的 spawn_teammate/send_message/broadcast 等都在此字典中
                    handler = tools.TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"
                # 控制台打印工具执行结果摘要（截断到 200 字符）
                print(f"> {block.name}: {str(output)[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                if block.name == "todo":
                    used_todo = True

        # -- todo 提醒机制 --
        # 如果连续 3 轮没有使用 todo 工具，注入 <reminder> 提醒 LLM 更新进度
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3:
            results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})

        # 将所有工具结果作为 user 消息追加（Claude API 要求 tool_result 放在 user 消息中）
        messages.append({"role": "user", "content": results})

        # ===== compact 后处理 =====
        # compact 工具被调用后，在工具结果全部追加完毕后才执行压缩
        # 这样 LLM 能看到 "Compressing..." 的 tool_result，然后整个对话被摘要替换
        if manual_compact:
            print("[manual compact triggered]")
            messages[:] = tools.auto_compact(messages)

# ===== REPL 入口 =====
# 交互式命令行界面，支持以下特殊命令：
#   q / exit / 空行  — 退出
#   /compact          — 手动触发对话压缩
#   /team             — 查看当前团队名册和状态（s10 新增）
#   /inbox            — 手动检查 lead 的收件箱（s10 新增）
if __name__ == "__main__":
    history = []  # 对话历史，贯穿整个会话
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")  # 青色提示符
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # /compact：手动压缩对话历史，不经过 LLM 循环
        if query.strip().lower() == "/compact":
            if history:
                history[:] = tools.auto_compact(history)
                print("[manual compact done]")
            else:
                print("Nothing to compact.")
            continue

        # /team：直接打印团队名册（不消耗 LLM 调用，纯本地查询）
        # 输出示例：
        #   Team: default
        #     alice (researcher): working
        #     bob (coder): idle
        if query.strip() == "/team":
            print(tools.TEAM.list_all())
            continue

        # /inbox：手动 drain lead 的收件箱，打印所有待读消息
        # 注意：read_inbox 是 drain-on-read 的，调用后收件箱会被清空
        if query.strip() == "/inbox":
            print(json.dumps(tools.BUS.read_inbox("lead"), indent=2))
            continue

        # 正常用户输入：追加到历史，交给 agent_loop 处理
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # 打印最后一条 assistant 消息中的文本内容
        # response.content 可能是 list[ContentBlock]，需要遍历提取 text
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()    