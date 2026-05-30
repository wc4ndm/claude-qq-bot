# Claude QQ Bot

把 Claude 接入 QQ，让 AI 接管你的 QQ 对话。通过 NapCat + aiocqhttp + function calling 大模型，实现 QQ 端的 AI 自动回复、工具调用、记忆共享。

## 效果

- QQ 私聊/群聊自动回复
- 支持 function calling（文件读写、搜索、HTTP 请求等）
- 记忆文件双向同步，与 Claude Desktop 共享上下文
- 对话持久化，重启不丢失
- 上下文超限自动压缩

## 架构

```
QQ 用户
    ↓
NapCat（QQ 机器人框架，OneBot v11）
    ↓ WebSocket
aiocqhttp（Python 服务，端口 8080）
    ↓ OpenAI 兼容 API
大模型（支持 function calling）
    ↑↓
工具层（文件、搜索、HTTP、时间）
```

## 前置条件

- Python 3.10+
- [NapCat](https://github.com/NapNeko/NapCatQQ) — QQ 机器人框架，需提前部署好并登录 QQ 账号
- 支持 function calling 的大模型 API（见下方"模型推荐"）

## 安装

```bash
git clone https://github.com/yourname/claude-qq-bot.git
cd claude-qq-bot
pip install -r requirements.txt
```

## 配置

复制示例配置并填入你自己的信息：

```bash
cp config.example.json config.json
```

编辑 `config.json`，填入你自己的信息：

### mimo_api（模型配置）

| 字段 | 说明 | 示例 |
|------|------|------|
| `base_url` | API 地址（OpenAI 兼容格式） | 见下方"模型推荐"章节 |
| `api_key` | 你的 API Key | 见对应平台的 API Keys 页面 |
| `model` | 模型名称 | 见对应平台的模型列表 |

### bot（机器人配置）

| 字段 | 说明 | 示例 |
|------|------|------|
| `port` | 服务端口，NapCat 会连到这个端口 | `8080` |
| `allowed_private_ids` | **允许使用 bot 的 QQ 号**，填你自己的号。数组格式，可填多个 | `[123456789, 987654321]` |
| `allowed_group_ids` | **允许 bot 响应的群号**，数组格式，可填多个 | `[111222333]` |
| `max_history_rounds` | 保留多少轮对话历史 | `10` |
| `clear_command` | 清空对话的指令 | `/clear` |
| `memory_dir` | 记忆文件目录，留空则不加载记忆 | `./memory` |
| `system_prompt` | AI 的人格设定，写在这里即可 | `你是一个友善的助手。` |

**关于 `allowed_private_ids` 和 `allowed_group_ids`：**
- 只有列表里的 QQ 号/群号 才能触发 bot 回复，其他人的消息会被忽略
- 如果只需要私聊，把 `allowed_group_ids` 留空 `[]`
- 如果只需要群聊，把 `allowed_private_ids` 留空 `[]`
- 不设置（空数组）则不限制，bot 会回复所有消息（不推荐）

完整配置示例：

```json
{
  "mimo_api": {
    "base_url": "https://your-api-endpoint/v1",
    "api_key": "your-api-key",
    "model": "your-model-name",
    "max_tokens": 2048,
    "temperature": 0.7
  },
  "bot": {
    "port": 8080,
    "allowed_private_ids": [123456789],
    "allowed_group_ids": [111222333],
    "max_history_rounds": 10,
    "clear_command": "/clear",
    "memory_dir": "./memory",
    "system_prompt": "你是一个友善的 AI 助手。"
  }
}
```

### 群聊行为

群聊中 bot 不会回复所有消息，只在以下情况触发：

1. **@bot** — 在群里 @ bot 的 QQ 号
2. **回复 bot** — 回复 bot 之前发的消息

这样可以避免 bot 在群里刷屏，也不用担心消耗大量 token。

### NapCat 配置

在 NapCat 中添加一个 **WebSocket 客户端**，地址填：

```
ws://localhost:8080/ws
```

## 运行

在项目目录下打开终端（PowerShell / CMD），执行：

```bash
python bot.py
```

终端窗口需要保持开启，关闭窗口 bot 就会停止。看到 `等待 NapCat 连接...` 且日志中出现 `NapCat: connect` 即表示连接成功。

> 如果想后台运行（不显示终端窗口），可以用 `pythonw bot.py`。日志会写入 `bot.log` 文件。

## 功能说明

### Function Calling 工具

| 工具 | 说明 |
|------|------|
| `read_file` | 读取文件内容 |
| `write_file` | 写入文件（需 Y/N 确认） |
| `list_directory` | 列出目录内容 |
| `search_web` | Bing 搜索 |
| `http_request` | HTTP 请求 |
| `get_current_time` | 获取当前时间 |

### 记忆共享

`memory_dir` 目录下的所有 `.md` 文件会被加载为 AI 的上下文。如果你同时在 Claude Desktop 端使用，可以让两边读写同一个目录，实现记忆双向同步。

### 对话持久化

对话历史自动保存到 `conversations.json`，每 60 秒存盘一次。重启 bot 后自动加载。

### 上下文压缩

当对话历史超出模型上下文限制时，自动压缩旧对话为摘要，新对话从摘要继续。用户感知不到中断。

### 指令

在 QQ 对话中直接发送：

- `/clear` — 清空当前对话历史（记忆文件不受影响）

## 已知限制

- **不支持图片**：目前只处理文本消息，图片、表情、文件等会被忽略
- **群聊引用回复**：能获取被引用消息的内容，但不支持回复特定某条消息的精确定位
- **模型差异**：不同模型的 function calling 能力有差异，复杂工具编排可能不稳定

## 模型推荐

任何支持 OpenAI function calling 的模型都可以使用。以下是主流平台的配置参考：

| 平台 | base_url | 模型名称 | 备注 |
|------|----------|----------|------|
| [DeepSeek](https://platform.deepseek.com/) | `https://api.deepseek.com` | `deepseek-v4-flash` | 性价比极高，1M 上下文 |
| [MiMo](https://platform.xiaomimimo.com/) | `https://api.xiaomimimo.com/v1` | `mimo-v2.5-pro` | 小米大模型 |
| [通义千问](https://dashscope.aliyun.com/) | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-max` | 阿里系，国内访问稳定 |
| [智谱](https://open.bigmodel.cn/) | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` | 工具调用成熟 |
| [Kimi](https://platform.moonshot.cn/) | `https://api.moonshot.cn/v1` | `moonshot-v1-128k` | 长上下文有优势 |
| [豆包](https://console.volcengine.com/ark/) | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-pro-128k` | 字节跳动 |

以上信息可能会变动，请以各平台官方文档为准。

## 目录结构

```
claude-qq-bot/
├── bot.py                  # 主程序
├── config.example.json     # 配置示例
├── config.json             # 你的配置（gitignore）
├── requirements.txt        # Python 依赖
├── conversations.json      # 对话持久化（自动生成，gitignore）
├── bot.log                 # 运行日志（自动生成，gitignore）
└── memory/                 # 记忆文件目录
    ├── MEMORY.md
    ├── persona.md
    └── ...
```

## License

MIT
