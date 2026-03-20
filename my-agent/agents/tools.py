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

THRESHOLD = 50000
KEEP_RECENT = 3
TRANSCRIPT_DIR = Path.cwd() / ".transcripts"


def estimate_tokens(messages: list) -> int:
    """Rough token count: ~4 chars per token."""
    return len(str(messages)) // 4


def micro_compact(messages: list):
    """Layer 1: Replace old tool_result content with short placeholders.
    Runs every turn, keeps only the last KEEP_RECENT results intact."""
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
    """Layer 2: Save full transcript to disk, then LLM-summarize and replace all messages."""
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


SUBAGENT_SYSTEM = f"""You are a coding subagent at {os.getcwd()}.
Complete the given task, then summarize your findings."""

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
}

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

PARENT_AGENT_TOOLS = CHILD_AGENT_TOOLS + [
    {"name": "task", "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
    {"name": "background_run", "description": "Run a shell command in the background (non-blocking). Returns a task ID immediately.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command to run in background"}}, "required": ["command"]}},
    {"name": "check_background", "description": "Check status and output of background tasks. Pass task_id for details, or omit for overview.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "description": "Background task ID to check (omit for all)"}}}},
]

TODO = None  # global singleton, initialized below

TASKS_DIR = Path.cwd() / ".tasks"


class TaskManager:
    """Persistent task graph (DAG). Each task is a JSON file in .tasks/."""

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

class SKILL_LOADER:
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


class TodoManager:
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

class BackgroundManager:
    """Non-blocking command execution via daemon threads."""

    def __init__(self, timeout: int = 300):
        self.timeout = timeout
        self._tasks: dict[str, dict] = {}
        self._notifications: queue.Queue = queue.Queue()
        self._lock = threading.Lock()

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


def safe_path(path: str) -> Path:
    path = (Path(os.getcwd()) / path).resolve()
    if not path.is_relative_to(Path(os.getcwd())):
        raise ValueError(f"Path escapes workspace: {path}")
    return path

def run_bash(command: str) -> str:
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

TODO = TodoManager()
SKILLS_DIR = Path(os.getcwd()) / "skills"
SKILL_LOADER = SKILL_LOADER(SKILLS_DIR) 

