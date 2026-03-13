# Docker Guide

在 Docker 容器中运行 learn-claude-code，Agent 执行的所有命令都在沙箱内，不会影响宿主机。

项目提供两个 service：

- **`ref`** — 参考实现（`agents/` 目录，s01-s12 + s_full）
- **`my`** — 你的实现（`my-agent/agents/` 目录）

## 前置条件

- Docker & Docker Compose
- 一个可用的 API Key（Anthropic 或兼容提供商）

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 API Key 和模型：

```dotenv
ANTHROPIC_API_KEY=sk-ant-xxx
MODEL_ID=claude-sonnet-4-6
```

如果使用其他兼容提供商（MiniMax / GLM / DeepSeek 等），取消注释对应的 `ANTHROPIC_BASE_URL` 和 `MODEL_ID`。详见 `.env.example`。

### 2. 构建镜像

```bash
docker compose build
```

### 3. 运行

```bash
# ============ 参考实现 (ref) ============

# 默认运行 s01
docker compose run --rm ref

# 运行指定课程
docker compose run --rm ref agents/s02_tool_use.py
docker compose run --rm ref agents/s12_worktree_task_isolation.py
docker compose run --rm ref agents/s_full.py

# ============ 你的实现 (my) ============

# 默认运行 s01
docker compose run --rm my

# 运行你写的某一步
docker compose run --rm my agents/s02_tool_use.py
```

## 数据持久化

两个 service 的数据隔离存放：

```
宿主机                              容器
./agents/          <──bindmount──>  /workspace/agents   (ref, 只读)
./skills/          <──bindmount──>  /workspace/skills   (ref & my, 只读)
./my-agent/agents/ <──bindmount──>  /workspace/agents   (my, 只读)
./data/ref/        <──bindmount──>  /workspace/data     (ref, 可读写)
./data/my/         <──bindmount──>  /workspace/data     (my, 可读写)
```

代码和 skills 以只读方式挂载，Agent 运行时产生的数据写入 `/workspace/data`，会持久化到对应的 `data/` 子目录，容器销毁后数据不丢失。

`data/` 已加入 `.gitignore`，不会被提交到仓库。

## 参考实现课程一览

| 命令 | 课程 |
|------|------|
| `docker compose run --rm ref agents/s01_agent_loop.py` | The Agent Loop |
| `docker compose run --rm ref agents/s02_tool_use.py` | Tool Use |
| `docker compose run --rm ref agents/s03_todo_write.py` | TodoWrite |
| `docker compose run --rm ref agents/s04_subagent.py` | Subagents |
| `docker compose run --rm ref agents/s05_skill_loading.py` | Skills |
| `docker compose run --rm ref agents/s06_context_compact.py` | Context Compact |
| `docker compose run --rm ref agents/s07_task_system.py` | Tasks |
| `docker compose run --rm ref agents/s08_background_tasks.py` | Background Tasks |
| `docker compose run --rm ref agents/s09_agent_teams.py` | Agent Teams |
| `docker compose run --rm ref agents/s10_team_protocols.py` | Team Protocols |
| `docker compose run --rm ref agents/s11_autonomous_agents.py` | Autonomous Agents |
| `docker compose run --rm ref agents/s12_worktree_task_isolation.py` | Worktree Isolation |
| `docker compose run --rm ref agents/s_full.py` | Full（综合版） |

## 容器内预装工具

镜像基于 `python:3.12-slim`，额外安装了 Agent 常用的命令行工具：

- `git` — 版本控制
- `curl` — HTTP 请求
- `jq` — JSON 处理
- `tree` — 目录结构查看

## 常见问题

**Q: 如何清空 Agent 产生的数据？**

```bash
# 清空全部
rm -rf ./data/*

# 只清空你的实现数据
rm -rf ./data/my/*
```

**Q: 如何进入容器调试？**

```bash
# 进入参考实现容器
docker compose run --rm --entrypoint bash ref

# 进入你的实现容器
docker compose run --rm --entrypoint bash my
```

**Q: 重新构建镜像（修改 Dockerfile 或依赖后）？**

```bash
docker compose build --no-cache
```
