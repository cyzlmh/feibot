# feibot

> 基于 nanobot 深度定制的飞书专属 AI 助手。

---

## ✨ 核心特性

### 1. 工具调用提示

显示正在调用的工具名称。

```
🔧 调用：list_dir(path=".")
```

---

### 2. 任务控制

| 命令 | 说明 |
|------|------|
| `/stop` | 取消当前任务 |
| `/go` | 继续因中断暂停的任务 |
| `/new` | 开启新会话 |

---

### 3. 子任务命令

| 命令 | 说明 |
|------|------|
| `/fork <任务>` | 带完整上下文的子任务，创建独立飞书群聊 |
| `/spawn <任务>` | 空上下文的子任务，创建独立飞书群聊 |

示例：
```
用户：/fork 分析代码并生成报告
🤖 创建群聊：任务-代码分析报告

用户：/spawn 用 Python 实现快速排序
🤖 创建群聊：任务-快速排序实现
```

---

### 4. 工具调用一致性校验

防止历史截断导致工具调用断裂。

---

### 5. 工具安全模型

执行工具只保留两条基础约束：
- `tools.writableDirs`：允许修改的本地目录
- `tools.allowedHosts`：允许远程连接的主机

文件默认可读；不在 `writableDirs` 里的路径不可写。

Madame agent 可配置显式工具白名单 (`tools.allowedTools`)。

---

### 6. 操作日志与消息去重

按会话存储原始消息 (JSONL)，支持：
- 消息去重（防止重复处理）
- 会话恢复与历史重建

```
workspace/logs/feishu_oc_xxx.jsonl
```

---

### 7. 强制配置文件

必须指定配置文件启动。

```bash
python -m feibot.gateway --config config.json
```

---

### 8. 飞书 Wiki 知识库

支持 Wiki 空间/节点管理。

```python
feishu_wiki spaces
feishu_wiki nodes <space_id>
feishu_wiki create/move/rename
```

---

### 9. 飞书多维表格 (Bitable)

完整 CRUD 操作。

```python
feishu_bitable_list_records
feishu_bitable_create_record
feishu_bitable_update_record
feishu_bitable_create_app
```

---

### 10. 飞书云盘

文件管理能力。

```python
feishu_drive list/create_folder/move/delete
```

---

### 11. Madame 控制平面

Madame 是唯一的 agent 管理入口，统一负责：

- **生命周期管理**：`/agent create|start|stop|restart|archive`
- **运行状态可见**：`/agent list`（Markdown 表格）、`/agent status <id>`
- **动态凭据池**：按 `name -> app_id/app_secret` 维护
- **Cron 定时任务**：`/agent cron list|add|runs|remove|enable|disable|run`
- **技能治理**：`/agent skills list|install|remove`，共享技能池
- **隔离策略**：`chat` 模式最小化提示词、禁用 skills/memory

---

### 12. 多渠道支持

| 渠道 | 状态 |
|------|------|
| 飞书 (Feishu/Lark) | ✅ 主要渠道 |
| WeChat (ilink API) | ✅ 实验性 |
| CLI 交互模式 | ✅ 本地测试 |

---

### 13. 原生 Provider 系统

支持多 LLM Provider，自动按 model 字符串匹配：

- Anthropic Claude
- OpenAI / Azure OpenAI / OpenAI Codex OAuth
- MiniMax
- DashScope (阿里云)
- Groq
- vLLM / Ollama (本地部署)

---

## 🚀 快速开始

### 安装

```bash
# 本地开发
uv sync

# 运行
uv run python -m feibot.gateway --config ./config.json

# CLI 交互模式
uv run python -m feibot
```

### Madame 初始化（推荐）

```bash
# 1) 初始化 Madame
uv run feibot madame init \
  --repo-dir ~/Projects/feibot \
  --madame-dir ~/madame \
  --app-id <MADAME_APP_ID> \
  --app-secret <MADAME_APP_SECRET> \
  --pool-slot "Agent1=<APP_ID>:<APP_SECRET>"

# 2) 启动 Madame gateway（macOS launchd）
~/madame/ops/manage.sh install

# 3) 后续管理统一走聊天内 /agent 命令
```

### 在对话中使用

```
/agent pool list
/agent create --name "Agent1" --mode agent
/agent create --name "ChatBot" --mode chat
/agent start|stop|restart <runtime_id>
/agent archive <runtime_id>
/agent list
/agent status <runtime_id>
/agent cron list|add|runs|remove|enable|disable|run ...
/agent skills list|install|remove ...
```

### 最小配置

```json
{
  "name": "feibot",
  "paths": {
    "workspace": "./workspace",
    "sessions": "./sessions"
  },
  "agents": {
    "defaults": {
      "model": "openai/gpt-4o"
    }
  },
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "allowFrom": ["ou_xxx"]
    }
  },
  "tools": {
    "writableDirs": ["./workspace"],
    "allowedHosts": [],
    "exec": {
      "timeout": 300,
      "pathAppend": ""
    }
  }
}
```

---

## 📊 与 nanobot 的差异

| 特性 | nanobot | feibot |
|------|---------|--------|
| 定位 | 多平台通用 | 飞书为主 |
| 工具提示 | ❌ | ✅ |
| 任务取消 | ❌ | ✅ `/stop` |
| 任务继续 | ❌ | ✅ `/go` |
| 子任务 | ❌ | ✅ `/fork` `/spawn` |
| 工具一致性校验 | ❌ | ✅ |
| 执行审批 | ❌ | ✅ (已简化) |
| 操作日志 | ❌ | ✅ |
| 消息去重 | ❌ | ✅ |
| 强制配置 | ❌ | ✅ |
| 飞书 Wiki | ❌ | ✅ |
| 飞书 Bitable | ❌ | ✅ |
| 飞书云盘 | ❌ | ✅ |
| 多 Agent 管理 | ❌ | ✅ Madame |
| Cron 定时任务 | ❌ | ✅ |
| 技能共享池 | ❌ | ✅ |
| 原生 Provider | ❌ | ✅ |

---

## 开发版本控制

版本号格式：`{major}.{minor}.{patch}-dev+{git_hash}`

| 变更类型 | 操作 | 示例 |
|----------|------|------|
| Bug 修复 | patch +1 | 0.1.4 → 0.1.5 |
| 新功能 | minor +1 | 0.1.4 → 0.2.0 |
| 重大变更 | major +1 | 0.1.4 → 1.0.0 |

### 发布流程

1. 更新 `pyproject.toml` 中的版本号
2. 提交代码：`git commit -m "release: v0.1.5"`
3. 打标签：`git tag v0.1.5`
4. 推送：`git push && git push --tags`

---

## 📄 License

MIT

基于 [nanobot](https://github.com/HKUDS/nanobot) 深度定制