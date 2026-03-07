# FeiBot (Feishu-only) - Personal AI Assistant

轻量级、可扩展的个人 AI 助手框架。

> 对外项目名和 CLI 统一使用 **FeiBot / `feibot`**。

## 快速开始

```bash
# 1. 安装（本地开发）
uv sync

# 2. 配置
cp config.example.json config.json
# 编辑 config.json，填入 API keys

# 3. 运行
uv run feibot --config ./config.json agent      # 交互模式
uv run feibot --config ./config.json gateway    # 服务端模式
```

## 最小配置

```json
{
  "agents": {
    "default": {
      "provider": "anthropic",
      "model": "claude-3-5-sonnet-20241022"
    }
  },
  "providers": {
    "anthropic": { "api_key": "sk-ant-api03-..." }
  }
}
```

## 功能特性

| 功能 | 说明 |
|------|------|
| **通道** | Feishu |
| **工具** | Web 搜索、Shell 执行、文件操作 |
| **定时任务** | Cron 调度、Heartbeat |
| **技能扩展** | 模块化 Skill 系统 |

## 常用命令

```bash
# 单次指令
uv run feibot run "总结这段文字" < article.txt

# 启动 gateway
uv run feibot --config ./config.json gateway
```

## 运维技能

安装 `feibot-ops` skill 实现 gateway 生命周期管理：

```bash
# 复制 skill 到项目
ln -s /path/to/feibot-ops ./skills/
```

然后在 agent 对话中使用：
- `@agent 查看 gateway 状态`
- `@agent 重启 gateway`

## 项目结构

```
feibot/
├── feibot/
│   ├── agent/        # Agent 核心
│   ├── channels/     # 通道适配
│   └── skills/       # 内置技能
├── skills/           # 自定义技能
└── config.example.json
```

## License

MIT

## 开发版本控制

版本号格式：`{major}.{minor}.{patch}-dev+{git_hash}`

例如：`0.1.4-dev+920f5fe`

### 版本号来源

- **基础版本**：定义在 `pyproject.toml` 的 `version` 字段
- **Git hash**：自动从当前 commit 获取 (7 位短 hash)

### 版本更新规范

| 变更类型 | 操作 | 示例 |
|----------|------|------|
| Bug 修复 | patch +1 | 0.1.4 → 0.1.5 |
| 新功能 | minor +1 | 0.1.4 → 0.2.0 |
| 重大变更 | major +1 | 0.1.4 → 1.0.0 |

### 发布流程

1. 更新 `pyproject.toml` 中的版本号
2. 更新 `feibot/__init__.py` 中的 `_base_version`
3. 提交代码：`git commit -m "release: v0.1.5"`
4. 打标签：`git tag v0.1.5`
5. 推送：`git push && git push --tags`

### 开发阶段

开发期间无需手动更新版本号，git hash 会自动附加到版本号后，便于追踪具体代码版本。
