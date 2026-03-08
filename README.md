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

### 2. 任务取消

通过 `/stop` 命令取消当前正在执行的任务。

```
用户：/stop
⏹ Stopped 1 task(s).
```

---

### 3. 工具调用一致性校验

防止历史截断导致工具调用断裂。

---

### 4. 三级执行审批

敏感操作需审批后执行。

#### Level 1: 简单放通
```json
{ "exec_approval": { "enabled": true, "mode": "text" } }
```
回复 `/approve` 或 `/deny` 审批。

#### Level 2: 飞书卡片
```json
{ "exec_approval": { "enabled": true, "mode": "feishu_card", "approvers": ["ou_xxx"] } }
```
审批人收到卡片一键审批。

#### Level 3: SIM 卡认证
```json
{ "exec_approval": { "enabled": true, "mode": "sim_auth" } }
```
需手机号二次确认，支持 CMCC SM2 签名验证。

**触发审批的命令**：`rm`, `git push`, `docker`, `sudo`, `curl` 等。

---

### 5. 操作日志

按会话存储原始消息，支持消息去重和会话恢复。

```
workspace/logs/feishu_oc_xxx.jsonl
```

---

### 6. 强制配置文件

必须指定配置文件启动。

```bash
feibot gateway --config config.json
```

---

### 7. Spawn 创建飞书群聊

子任务自动创建独立飞书群聊。

```
用户：/sp 分析代码并生成报告
🤖 创建群聊：任务-代码分析报告
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

## 🚀 快速开始

### 安装

```bash
# 本地开发
uv sync

# 运行
uv run feibot --config ./config.json gateway
```

### 最小配置

```json
{
  "model": "gpt-4o",
  "channels": {
    "feishu": {
      "enabled": true,
      "app_id": "cli_xxx",
      "app_secret": "xxx",
      "allow_from": ["ou_xxx"]
    }
  },
  "exec_approval": {
    "enabled": true,
    "mode": "feishu_card",
    "approvers": ["ou_xxx"]
  }
}
```

---

## 📊 与 nanobot 的差异

| 特性 | nanobot | feibot |
|------|---------|--------|
| 定位 | 多平台通用 | 飞书专属 |
| 工具提示 | ❌ | ✅ |
| 任务取消 | ❌ | ✅ `/stop` 命令 |
| 工具一致性校验 | ❌ | ✅ |
| 执行审批 | ❌ | 三级 |
| 操作日志 | ❌ | ✅ Channel Log |
| 强制配置 | ❌ | ✅ |
| 子任务群聊 | ❌ | ✅ |
| 飞书 Wiki | ❌ | ✅ |
| 飞书 Bitable | ❌ | ✅ |
| 飞书云盘 | ❌ | ✅ |
| 支持渠道 | 9+ | 飞书为主 |

---

## 运维技能

安装 `feibot-ops` skill 实现 gateway 生命周期管理：

```bash
ln -s /path/to/feibot-ops ./skills/
```

然后在对话中使用：
- `@agent 查看 gateway 状态`
- `@agent 重启 gateway`

---

## 开发版本控制

版本号格式：`{major}.{minor}.{patch}-dev+{git_hash}`

### 版本更新规范

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
