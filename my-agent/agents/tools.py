import os
import subprocess
from pathlib import Path

TOOL_HANDLERS = {
    "bash": lambda command, **_:run_bash(command),
    "read_file": lambda path, limit=None, **_:read_file(path, limit),
    "write_file": lambda path, content, **_:write_file(path, content),
    "edit_file": lambda path, old_text, new_text, **_:edit_file(path, old_text, new_text),
}

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


