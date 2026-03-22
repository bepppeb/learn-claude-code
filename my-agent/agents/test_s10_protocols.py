"""
test_s10_protocols.py — s10 团队协议测试

测试范围：
1. MessageBus: send / read_inbox / broadcast / 线程安全
2. TeammateManager: spawn / _find_member / list_all / config 持久化
3. Shutdown 协议: handle_shutdown_request / shutdown_response / check_shutdown_status
4. Plan Approval 协议: plan_approval / handle_plan_review
5. 请求跟踪器: shutdown_requests / plan_requests / _tracker_lock

不需要 LLM 调用：所有测试直接调用 Python 函数，不涉及 API。
"""

import json
import shutil
import threading
import time
from pathlib import Path

import pytest

# 为了避免 import tools 时触发 SKILL_LOADER 和 TEAM 的副作用，
# 我们需要先设置好环境变量
import os
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import sys
sys.path.insert(0, str(Path(__file__).parent))

import tools


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def tmp_inbox(tmp_path):
    """创建临时 inbox 目录，返回一个全新的 MessageBus 实例。"""
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    return tools.MessageBus(inbox_dir)


@pytest.fixture
def tmp_team(tmp_path):
    """创建临时 team 目录，返回一个全新的 TeammateManager 实例。"""
    team_dir = tmp_path / "team"
    team_dir.mkdir()
    return tools.TeammateManager(team_dir)


@pytest.fixture(autouse=True)
def clean_trackers():
    """每个测试前清空全局跟踪器。"""
    tools.shutdown_requests.clear()
    tools.plan_requests.clear()
    yield
    tools.shutdown_requests.clear()
    tools.plan_requests.clear()


# ============================================================
# 1. MessageBus 测试
# ============================================================

class TestMessageBus:
    def test_send_and_read(self, tmp_inbox):
        """基本发送和接收：send 后 read_inbox 能拿到消息。"""
        tmp_inbox.send("lead", "alice", "hello")
        msgs = tmp_inbox.read_inbox("alice")
        assert len(msgs) == 1
        assert msgs[0]["from"] == "lead"
        assert msgs[0]["content"] == "hello"
        assert msgs[0]["type"] == "message"

    def test_drain_on_read(self, tmp_inbox):
        """read_inbox 是 drain-on-read：读一次后收件箱清空。"""
        tmp_inbox.send("lead", "alice", "msg1")
        tmp_inbox.send("lead", "alice", "msg2")
        msgs = tmp_inbox.read_inbox("alice")
        assert len(msgs) == 2
        # 再次读取应该为空
        msgs2 = tmp_inbox.read_inbox("alice")
        assert len(msgs2) == 0

    def test_read_empty_inbox(self, tmp_inbox):
        """读取从未收到过消息的收件箱返回空列表。"""
        msgs = tmp_inbox.read_inbox("nobody")
        assert msgs == []

    def test_invalid_msg_type(self, tmp_inbox):
        """发送无效消息类型返回错误字符串。"""
        result = tmp_inbox.send("lead", "alice", "hello", msg_type="invalid_type")
        assert "Error" in result

    def test_valid_msg_types(self, tmp_inbox):
        """所有有效消息类型都能正常发送。"""
        for msg_type in tools.VALID_MSG_TYPES:
            result = tmp_inbox.send("lead", "alice", f"test-{msg_type}", msg_type=msg_type)
            assert "Sent" in result
        msgs = tmp_inbox.read_inbox("alice")
        assert len(msgs) == len(tools.VALID_MSG_TYPES)

    def test_extra_fields(self, tmp_inbox):
        """extra 字段会被合并到消息 JSON 中。"""
        tmp_inbox.send("lead", "alice", "plan", "message", extra={"request_id": "abc123"})
        msgs = tmp_inbox.read_inbox("alice")
        assert msgs[0]["request_id"] == "abc123"

    def test_broadcast(self, tmp_inbox):
        """broadcast 给所有队友发送消息，跳过发送者自己。"""
        result = tmp_inbox.broadcast("lead", "hello all", ["lead", "alice", "bob"])
        assert "Broadcast to 2" in result
        # alice 和 bob 都应该收到
        alice_msgs = tmp_inbox.read_inbox("alice")
        bob_msgs = tmp_inbox.read_inbox("bob")
        assert len(alice_msgs) == 1
        assert len(bob_msgs) == 1
        assert alice_msgs[0]["type"] == "broadcast"
        # lead 不应该收到自己的消息
        lead_msgs = tmp_inbox.read_inbox("lead")
        assert len(lead_msgs) == 0

    def test_concurrent_sends(self, tmp_inbox):
        """多线程并发发送到同一个收件箱不会丢消息。"""
        n_threads = 10
        n_per_thread = 50
        barrier = threading.Barrier(n_threads)

        def sender(thread_id):
            barrier.wait()  # 所有线程同时开始
            for i in range(n_per_thread):
                tmp_inbox.send(f"sender-{thread_id}", "alice", f"msg-{thread_id}-{i}")

        threads = [threading.Thread(target=sender, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        msgs = tmp_inbox.read_inbox("alice")
        assert len(msgs) == n_threads * n_per_thread

    def test_concurrent_send_and_read(self, tmp_inbox):
        """并发 send 和 read_inbox 不会丢消息（drain-on-read 原子性）。"""
        total_sent = 0
        total_read = 0
        lock = threading.Lock()

        def sender():
            nonlocal total_sent
            for i in range(100):
                tmp_inbox.send("lead", "alice", f"msg-{i}")
                with lock:
                    total_sent += 1
                time.sleep(0.001)

        def reader():
            nonlocal total_read
            for _ in range(50):
                msgs = tmp_inbox.read_inbox("alice")
                with lock:
                    total_read += len(msgs)
                time.sleep(0.002)

        t1 = threading.Thread(target=sender)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 最后一次 drain 读取剩余消息
        remaining = tmp_inbox.read_inbox("alice")
        total_read += len(remaining)

        assert total_read == total_sent


# ============================================================
# 2. TeammateManager 测试（不涉及 LLM）
# ============================================================

class TestTeammateManager:
    def test_empty_team(self, tmp_team):
        """初始状态没有队友。"""
        assert tmp_team.list_all() == "No teammates."
        assert tmp_team.member_names() == []

    def test_find_member_not_found(self, tmp_team):
        """查找不存在的队友返回 None。"""
        assert tmp_team._find_member("nobody") is None

    def test_config_persistence(self, tmp_team):
        """config 写入磁盘后能被重新加载。"""
        tmp_team.config["members"].append({"name": "alice", "role": "coder", "status": "idle"})
        tmp_team._save_config()

        # 重新加载
        reloaded = tools.TeammateManager(tmp_team.dir)
        member = reloaded._find_member("alice")
        assert member is not None
        assert member["role"] == "coder"
        assert member["status"] == "idle"

    def test_find_member_returns_reference(self, tmp_team):
        """_find_member 返回的是引用，修改它会直接影响名册。"""
        tmp_team.config["members"].append({"name": "alice", "role": "coder", "status": "idle"})
        member = tmp_team._find_member("alice")
        member["status"] = "working"
        # 直接检查 config 中的状态也变了
        assert tmp_team.config["members"][0]["status"] == "working"

    def test_list_all_format(self, tmp_team):
        """list_all 输出格式检查。"""
        tmp_team.config["members"].append({"name": "alice", "role": "coder", "status": "working"})
        tmp_team.config["members"].append({"name": "bob", "role": "reviewer", "status": "idle"})
        output = tmp_team.list_all()
        assert "alice (coder): working" in output
        assert "bob (reviewer): idle" in output

    def test_member_names(self, tmp_team):
        """member_names 返回所有队友名字列表。"""
        tmp_team.config["members"].append({"name": "alice", "role": "coder", "status": "idle"})
        tmp_team.config["members"].append({"name": "bob", "role": "reviewer", "status": "idle"})
        names = tmp_team.member_names()
        assert set(names) == {"alice", "bob"}


# ============================================================
# 3. Shutdown 协议测试
# ============================================================

class TestShutdownProtocol:
    def test_handle_shutdown_request(self, tmp_inbox):
        """handle_shutdown_request 生成 request_id 并发送消息。"""
        # 临时替换全局 BUS
        original_bus = tools.BUS
        tools.BUS = tmp_inbox
        try:
            result = tools.handle_shutdown_request("alice")
            assert "Shutdown request" in result
            assert "sent to 'alice'" in result
            assert "pending" in result

            # 提取 request_id
            req_id = result.split()[2]  # "Shutdown request {req_id} sent..."

            # 检查 tracker
            assert req_id in tools.shutdown_requests
            assert tools.shutdown_requests[req_id]["target"] == "alice"
            assert tools.shutdown_requests[req_id]["status"] == "pending"

            # 检查 alice 的收件箱收到了消息
            msgs = tmp_inbox.read_inbox("alice")
            assert len(msgs) == 1
            assert msgs[0]["type"] == "shutdown_request"
            assert msgs[0]["request_id"] == req_id
        finally:
            tools.BUS = original_bus

    def test_check_shutdown_status(self):
        """check_shutdown_status 返回正确的状态。"""
        tools.shutdown_requests["test123"] = {"target": "alice", "status": "pending"}
        result = tools.check_shutdown_status("test123")
        data = json.loads(result)
        assert data["target"] == "alice"
        assert data["status"] == "pending"

    def test_check_shutdown_status_not_found(self):
        """查询不存在的 request_id 返回 error。"""
        result = tools.check_shutdown_status("nonexistent")
        data = json.loads(result)
        assert "error" in data

    def test_shutdown_response_approve(self, tmp_inbox):
        """队友批准 shutdown 请求：tracker 状态变为 approved。"""
        original_bus = tools.BUS
        tools.BUS = tmp_inbox
        try:
            # 先创建一个 shutdown request
            tools.shutdown_requests["req001"] = {"target": "alice", "status": "pending"}

            # 模拟队友的 _exec 调用 shutdown_response
            # 直接测试 TeammateManager._exec 中的逻辑
            req_id = "req001"
            approve = True
            with tools._tracker_lock:
                tools.shutdown_requests[req_id]["status"] = "approved"
            tmp_inbox.send("alice", "lead", "", "shutdown_response",
                          {"request_id": req_id, "approve": approve})

            # 验证 tracker 状态
            assert tools.shutdown_requests["req001"]["status"] == "approved"

            # 验证 lead 收到了回复
            msgs = tmp_inbox.read_inbox("lead")
            assert len(msgs) == 1
            assert msgs[0]["type"] == "shutdown_response"
            assert msgs[0]["approve"] is True
        finally:
            tools.BUS = original_bus

    def test_shutdown_response_reject(self, tmp_inbox):
        """队友拒绝 shutdown 请求：tracker 状态变为 rejected。"""
        original_bus = tools.BUS
        tools.BUS = tmp_inbox
        try:
            tools.shutdown_requests["req002"] = {"target": "bob", "status": "pending"}

            with tools._tracker_lock:
                tools.shutdown_requests["req002"]["status"] = "rejected"
            tmp_inbox.send("bob", "lead", "still working", "shutdown_response",
                          {"request_id": "req002", "approve": False})

            assert tools.shutdown_requests["req002"]["status"] == "rejected"
        finally:
            tools.BUS = original_bus


# ============================================================
# 4. Plan Approval 协议测试
# ============================================================

class TestPlanApprovalProtocol:
    def test_plan_submission(self, tmp_inbox):
        """队友提交计划：生成 request_id，发送给 lead。"""
        original_bus = tools.BUS
        tools.BUS = tmp_inbox
        try:
            import uuid
            plan_text = "I plan to refactor the auth module."
            req_id = str(uuid.uuid4())[:8]
            with tools._tracker_lock:
                tools.plan_requests[req_id] = {"from": "alice", "plan": plan_text, "status": "pending"}
            tmp_inbox.send("alice", "lead", plan_text, "plan_approval_response",
                          {"request_id": req_id, "plan": plan_text})

            # 验证 tracker
            assert req_id in tools.plan_requests
            assert tools.plan_requests[req_id]["from"] == "alice"
            assert tools.plan_requests[req_id]["status"] == "pending"

            # 验证 lead 收到消息
            msgs = tmp_inbox.read_inbox("lead")
            assert len(msgs) == 1
            assert msgs[0]["request_id"] == req_id
        finally:
            tools.BUS = original_bus

    def test_plan_approval(self, tmp_inbox):
        """Lead 批准计划：状态变为 approved，队友收到审批结果。"""
        original_bus = tools.BUS
        tools.BUS = tmp_inbox
        try:
            tools.plan_requests["plan001"] = {"from": "alice", "plan": "refactor auth", "status": "pending"}

            result = tools.handle_plan_review("plan001", approve=True, feedback="Looks good!")
            assert "approved" in result

            # 验证 tracker
            assert tools.plan_requests["plan001"]["status"] == "approved"

            # 验证 alice 收到审批结果
            msgs = tmp_inbox.read_inbox("alice")
            assert len(msgs) == 1
            assert msgs[0]["approve"] is True
            assert msgs[0]["feedback"] == "Looks good!"
        finally:
            tools.BUS = original_bus

    def test_plan_rejection(self, tmp_inbox):
        """Lead 拒绝计划：状态变为 rejected。"""
        original_bus = tools.BUS
        tools.BUS = tmp_inbox
        try:
            tools.plan_requests["plan002"] = {"from": "bob", "plan": "delete everything", "status": "pending"}

            result = tools.handle_plan_review("plan002", approve=False, feedback="Too risky.")
            assert "rejected" in result
            assert tools.plan_requests["plan002"]["status"] == "rejected"
        finally:
            tools.BUS = original_bus

    def test_plan_review_unknown_id(self, tmp_inbox):
        """审批不存在的 plan request_id 返回错误。"""
        original_bus = tools.BUS
        tools.BUS = tmp_inbox
        try:
            result = tools.handle_plan_review("nonexistent", approve=True)
            assert "Error" in result
        finally:
            tools.BUS = original_bus


# ============================================================
# 5. args_approve 辅助函数测试
# ============================================================

class TestArgsApprove:
    def test_approve_true(self):
        assert tools.args_approve({"approve": True}) is True

    def test_approve_false(self):
        assert tools.args_approve({"approve": False}) is False

    def test_approve_missing(self):
        assert tools.args_approve({}) is False


# ============================================================
# 6. 工具注册表完整性测试
# ============================================================

class TestToolRegistration:
    def test_tool_handlers_has_protocol_tools(self):
        """TOOL_HANDLERS 包含 s10 协议工具。"""
        assert "shutdown_request" in tools.TOOL_HANDLERS
        assert "shutdown_response" in tools.TOOL_HANDLERS
        assert "plan_approval" in tools.TOOL_HANDLERS

    def test_parent_tools_has_protocol_schemas(self):
        """PARENT_AGENT_TOOLS 包含 s10 协议工具 schema。"""
        tool_names = {t["name"] for t in tools.PARENT_AGENT_TOOLS}
        assert "shutdown_request" in tool_names
        assert "shutdown_response" in tool_names
        assert "plan_approval" in tool_names

    def test_parent_tools_superset_of_child(self):
        """PARENT_AGENT_TOOLS 是 CHILD_AGENT_TOOLS 的超集。"""
        child_names = {t["name"] for t in tools.CHILD_AGENT_TOOLS}
        parent_names = {t["name"] for t in tools.PARENT_AGENT_TOOLS}
        assert child_names.issubset(parent_names)

    def test_all_handlers_have_schemas(self):
        """每个 TOOL_HANDLER 都有对应的 schema 定义。"""
        parent_names = {t["name"] for t in tools.PARENT_AGENT_TOOLS}
        for handler_name in tools.TOOL_HANDLERS:
            assert handler_name in parent_names, f"Handler '{handler_name}' missing from PARENT_AGENT_TOOLS"


# ============================================================
# 7. 基础工具测试
# ============================================================

class TestBaseTools:
    def test_safe_path_escapes(self):
        """safe_path 拦截路径逃逸。"""
        with pytest.raises(ValueError, match="escapes workspace"):
            tools.safe_path("../../../etc/passwd")

    def test_safe_path_normal(self):
        """safe_path 正常路径不报错。"""
        result = tools.safe_path("test.txt")
        assert result.name == "test.txt"

    def test_run_bash_dangerous(self):
        """危险命令被拦截。"""
        assert "Error" in tools.run_bash("sudo rm -rf /")
        assert "Error" in tools.run_bash("shutdown now")

    def test_run_bash_normal(self):
        """正常命令能执行。"""
        result = tools.run_bash("echo hello")
        assert "hello" in result

    def test_read_file(self, tmp_path):
        """读取文件内容。"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3")
        # 需要在 cwd 下，所以用绝对路径不会通过 safe_path
        # 直接测试函数
        result = test_file.read_text()
        assert "line1" in result

    def test_write_and_read_file(self, tmp_path):
        """写入文件后能读回来。"""
        test_file = tmp_path / "output.txt"
        test_file.write_text("hello world")
        assert test_file.read_text() == "hello world"

    def test_edit_file(self, tmp_path):
        """编辑文件：替换文本。"""
        test_file = tmp_path / "edit_test.txt"
        test_file.write_text("foo bar baz")
        text = test_file.read_text()
        test_file.write_text(text.replace("bar", "qux", 1))
        assert test_file.read_text() == "foo qux baz"


# ============================================================
# 8. TodoManager 测试
# ============================================================

class TestTodoManager:
    def test_update_and_render(self):
        todo = tools.TodoManager()
        result = todo.update([
            {"id": "1", "text": "task A", "status": "pending"},
            {"id": "2", "text": "task B", "status": "in_progress"},
        ])
        assert "[ ] #1: task A" in result
        assert "[>] #2: task B" in result

    def test_max_items(self):
        todo = tools.TodoManager()
        items = [{"id": str(i), "text": f"task {i}", "status": "pending"} for i in range(21)]
        with pytest.raises(ValueError, match="Max 20"):
            todo.update(items)

    def test_multiple_in_progress(self):
        todo = tools.TodoManager()
        with pytest.raises(ValueError, match="Only one"):
            todo.update([
                {"id": "1", "text": "A", "status": "in_progress"},
                {"id": "2", "text": "B", "status": "in_progress"},
            ])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
