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
"""

import os
import readline  # noqa: F401 — enables arrow keys and history in input()

import tools
from settings import client, MODEL

SYSTEM = f"""You are a coding agent at {os.getcwd()}.
Use task_create/task_update/task_list for multi-step work — tasks persist to disk and survive compression.
Use the todo tool for quick in-memory checklists within a single session.
Prefer tools over prose.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.

Skills available:
{tools.SKILL_LOADER.get_descriptions()}
"""



def agent_loop(messages: list):
    rounds_since_todo = 0
    while True:
        # Layer 1: micro_compact — replace old tool results with placeholders
        tools.micro_compact(messages)
        # Layer 2: auto_compact — if token estimate exceeds threshold, summarize
        if tools.estimate_tokens(messages) > tools.THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = tools.auto_compact(messages)
        response = client.messages.create(messages=messages, model=MODEL, system=SYSTEM, tools=tools.PARENT_AGENT_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        used_todo = False
        manual_compact = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    manual_compact = True
                    output = "Compressing..."
                else:
                    if "command" in block.input:
                        print(f"\033[33m$ {block.input['command']}\033[0m")
                    handler = tools.TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"
                print(f"> {block.name}: {str(output)[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                if block.name == "todo":
                    used_todo = True
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3:
            results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})
        messages.append({"role": "user", "content": results})
        # Layer 3: manual compact — triggered by the compact tool
        if manual_compact:
            print("[manual compact triggered]")
            messages[:] = tools.auto_compact(messages)

if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip().lower() == "/compact":
            if history:
                history[:] = tools.auto_compact(history)
                print("[manual compact done]")
            else:
                print("Nothing to compact.")
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()    