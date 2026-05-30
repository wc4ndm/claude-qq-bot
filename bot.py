"""
QQ Bot — aiocqhttp + MiMo API (function calling)
通过 NapCat 接入 QQ，支持 function calling、记忆共享、对话持久化。
"""

import json
import asyncio
import logging
import sys
import os
import glob
import re
import time as time_mod
from pathlib import Path
from datetime import datetime

import httpx
from aiocqhttp import CQHttp, Event
from openai import AsyncOpenAI

# ───────────────────── 日志 ─────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("qq-bot")

# ───────────────────── 配置 ─────────────────────

CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error(f"配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()

# ───────────────────── Memory 加载 ─────────────────────

class MemoryLoader:
    """从 memory 目录加载人格卡和记忆文件，构建 system prompt。"""

    def __init__(self, memory_dir: str, extra_persona: str = ""):
        self.memory_dir = Path(memory_dir)
        self.extra_persona = extra_persona
        self.last_load = 0
        self.reload_interval = 300  # 5 分钟
        self._cached_prompt = ""

    def load(self, force: bool = False) -> str:
        now = time_mod.time()
        if not force and self._cached_prompt and (now - self.last_load) < self.reload_interval:
            return self._cached_prompt

        parts = []
        parts.append(self.extra_persona)

        if not self.memory_dir.exists():
            log.warning(f"memory 目录不存在: {self.memory_dir}")
            return self.extra_persona

        # 读 MEMORY.md 索引
        index_path = self.memory_dir / "MEMORY.md"
        if index_path.exists():
            parts.append("\n## 记忆索引\n" + index_path.read_text(encoding="utf-8"))

        # 读所有子记忆文件
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            content = md_file.read_text(encoding="utf-8")
            # 去掉 frontmatter
            content = re.sub(r'^---\n.*?\n---\n', '', content, flags=re.DOTALL)
            if content.strip():
                parts.append(f"\n## {md_file.stem}\n{content.strip()}")

        self._cached_prompt = "\n".join(parts)
        self.last_load = now
        log.info(f"memory 已加载: {len(parts)} 个部分")
        return self._cached_prompt

# ───────────────────── 工具定义 ─────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容。可以读取文本文件、配置文件、代码文件等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的绝对路径"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入文件。创建新文件或覆盖已有文件。写入前会要求用户确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的绝对路径"},
                    "content": {"type": "string", "description": "要写入的内容"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出目录中的文件和子目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "通过 Bing 搜索网页。返回搜索结果摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": "发送 HTTP 请求获取网页或 API 内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "请求 URL"},
                    "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
]

# ───────────────────── 工具执行 ─────────────────────

async def execute_tool(name: str, args: dict) -> str:
    # 写入路径安全检查
    WRITE_PROTECTED = {"MEMORY.md", "config.json", "bot.py", "CLAUDE.md"}
    WRITE_ALLOWED_PREFIXES = [
        str(Path(config["bot"].get("memory_dir", "./memory")).resolve()),
        str(Path(__file__).parent.resolve()),
    ]

    try:
        if name == "read_file":
            p = Path(args["path"])
            if not p.exists():
                return f"文件不存在: {args['path']}"
            return p.read_text(encoding="utf-8", errors="replace")[:8000]

        elif name == "write_file":
            p = Path(args["path"]).resolve()
            # 检查是否写保护文件
            if p.name in WRITE_PROTECTED:
                return f"写入被拒绝: {p.name} 是受保护文件，不允许覆盖。可以写入 bot_memory.md 或新建其他文件。"
            # 检查路径白名单
            if not any(str(p).startswith(prefix) for prefix in WRITE_ALLOWED_PREFIXES):
                return f"写入被拒绝: 路径不在允许范围内。只能写入 memory 目录或项目目录。"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"已写入: {args['path']} ({len(args['content'])} 字符)"

        elif name == "list_directory":
            p = Path(args["path"])
            if not p.exists():
                return f"目录不存在: {args['path']}"
            items = []
            for item in sorted(p.iterdir()):
                prefix = "[dir] " if item.is_dir() else "[file]"
                items.append(f"{prefix} {item.name}")
            return "\n".join(items[:50]) or "空目录"

        elif name == "search_web":
            query = args["query"]
            url = f"https://www.bing.com/search?q={query}"
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                # 提取文本内容（简单提取）
                text = re.sub(r'<[^>]+>', ' ', resp.text)
                text = re.sub(r'\s+', ' ', text).strip()
                return text[:3000]

        elif name == "http_request":
            method = args.get("method", "GET").upper()
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                if method == "POST":
                    resp = await client.post(args["url"])
                else:
                    resp = await client.get(args["url"])
                return resp.text[:5000]

        elif name == "get_current_time":
            now = datetime.now()
            return now.strftime("%Y-%m-%d %H:%M:%S (%A)")

        else:
            return f"未知工具: {name}"

    except Exception as e:
        log.error(f"工具执行错误 [{name}]: {e}")
        return f"工具执行错误: {str(e)}"

# ───────────────────── 需要确认的写操作 ─────────────────────

class PendingConfirmations:
    """管理等待用户确认的操作。"""

    def __init__(self):
        self._pending: dict[int, dict] = {}  # user_id -> {tool, args, event, timer}

    def add(self, user_id: int, tool_name: str, args: dict, event: Event, timer: asyncio.Task):
        self._pending[user_id] = {
            "tool": tool_name,
            "args": args,
            "event": event,
            "timer": timer,
        }

    def get(self, user_id: int) -> dict | None:
        return self._pending.get(user_id)

    def remove(self, user_id: int) -> dict | None:
        entry = self._pending.pop(user_id, None)
        if entry and entry.get("timer"):
            entry["timer"].cancel()
        return entry

# ───────────────────── 对话历史（持久化 + 压缩）─────────────────────

class ConversationManager:
    def __init__(self, max_rounds: int, save_path: str = ""):
        self.max_rounds = max_rounds
        self._histories: dict[str, list[dict]] = {}
        self._summaries: dict[str, str] = {}  # 每个会话的压缩摘要
        self.save_path = Path(save_path) if save_path else None
        self.load()

    def _key(self, event: Event) -> str:
        if event.detail_type == "group":
            return f"group_{event.group_id}_{event.user_id}"
        return f"private_{event.user_id}"

    def get(self, event: Event) -> list[dict]:
        key = self._key(event)
        history = list(self._histories.get(key, []))
        # 如果有压缩摘要，作为第一条插入
        summary = self._summaries.get(key, "")
        if summary:
            return [{"role": "system", "content": f"[之前的对话摘要]\n{summary}"}] + history
        return history

    def get_summary(self, event: Event) -> str:
        return self._summaries.get(self._key(event), "")

    def add(self, event: Event, messages: list[dict]):
        key = self._key(event)
        if key not in self._histories:
            self._histories[key] = []
        self._histories[key].extend(messages)
        # 裁剪：保留最近 N 轮，但始终保留压缩摘要
        max_msgs = self.max_rounds * 2 + 10
        if len(self._histories[key]) > max_msgs:
            self._histories[key] = self._histories[key][-max_msgs:]

    def clear(self, event: Event):
        key = self._key(event)
        self._histories.pop(key, None)
        self._summaries.pop(key, None)

    def compress(self, event: Event, old_messages: list[dict]) -> str:
        """把旧对话压缩成摘要，存入 _summaries，清空对应历史。"""
        key = self._key(event)
        # 组装压缩请求
        compress_messages = [
            {"role": "system", "content": "你是一个对话摘要生成器。请将以下对话压缩为简洁的摘要（中文，300字以内），保留关键信息、决策、待办事项。只输出摘要，不要加前缀。"},
        ]
        for m in old_messages:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                compress_messages.append({
                    "role": m["role"],
                    "content": m["content"][:500],  # 截断长消息
                })
        compress_messages.append({"role": "user", "content": "请压缩以上对话。"})
        return compress_messages, key

    def finish_compress(self, key: str, summary: str, new_history: list[dict]):
        """压缩完成后：保存摘要，重置历史。"""
        old = self._summaries.get(key, "")
        if old:
            summary = f"{old}\n{summary}"
        self._summaries[key] = summary[-2000:]  # 摘要最多 2000 字符
        self._histories[key] = new_history
        self.save()
        log.info(f"[压缩] {key}: 摘要 {len(summary)} 字符，历史已重置")

    def save(self):
        """持久化到磁盘。"""
        if not self.save_path:
            return
        try:
            data = {
                "histories": self._histories,
                "summaries": self._summaries,
            }
            self.save_path.parent.mkdir(parents=True, exist_ok=True)
            self.save_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log.error(f"保存对话失败: {e}")

    def load(self):
        """从磁盘加载。"""
        if not self.save_path or not self.save_path.exists():
            return
        try:
            data = json.loads(self.save_path.read_text(encoding="utf-8"))
            self._histories = data.get("histories", {})
            self._summaries = data.get("summaries", {})
            total = sum(len(v) for v in self._histories.values())
            log.info(f"已加载对话历史: {total} 条消息, {len(self._summaries)} 个摘要")
        except Exception as e:
            log.warning(f"加载对话历史失败: {e}")

# ───────────────────── 消息解析 ─────────────────────

def extract_text(message) -> str:
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        parts = []
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(seg.get("data", {}).get("text", ""))
        return "".join(parts).strip()
    return str(message).strip()

# ───────────────────── 消息发送 ─────────────────────

async def send_reply(bot: CQHttp, event: Event, text: str):
    MAX_LEN = 4000
    if len(text) <= MAX_LEN:
        await bot.send(event, text)
        return
    parts, remaining = [], text
    while remaining:
        if len(remaining) <= MAX_LEN:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, MAX_LEN)
        if cut < MAX_LEN * 0.3:
            cut = MAX_LEN
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    for i, part in enumerate(parts):
        if i > 0:
            await asyncio.sleep(0.5)
        await bot.send(event, part)

# ───────────────────── 权限检查 ─────────────────────

def is_allowed(event: Event) -> bool:
    allowed_private = set(config["bot"]["allowed_private_ids"])
    allowed_groups = set(config["bot"]["allowed_group_ids"])
    if event.detail_type == "private":
        return event.user_id in allowed_private
    elif event.detail_type == "group":
        return event.group_id in allowed_groups
    return False


def should_respond_group(event: Event) -> bool:
    """群聊中是否应该回复：@bot 或回复 bot 的消息。"""
    if event.detail_type != "group":
        return True

    message = event.message
    if not isinstance(message, list):
        return True  # string 格式，直接放行

    self_id = str(event.self_id)

    # 检查是否 @ 了 bot
    for seg in message:
        if seg.get("type") == "at" and str(seg.get("data", {}).get("qq", "")) == self_id:
            return True

    # 检查是否回复了 bot 的消息
    for seg in message:
        if seg.get("type") == "reply":
            return True

    return False

# ───────────────────── 核心：带 function calling 的对话 ─────────────────────

async def chat_with_tools(
    client: AsyncOpenAI,
    system_prompt: str,
    history: list[dict],
    user_message: str,
    conv: ConversationManager = None,
    event: Event = None,
    max_rounds: int = 5,
) -> tuple[str, list[dict]]:
    """
    带 function calling 的对话循环。
    返回 (回复文本, 本轮新增的 messages)。
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    new_messages = [{"role": "user", "content": user_message}]

    for _ in range(max_rounds):
        try:
            resp = await client.chat.completions.create(
                model=config["mimo_api"]["model"],
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=config["mimo_api"].get("max_tokens", 2048),
                temperature=config["mimo_api"].get("temperature", 0.7),
            )
        except Exception as e:
            err = str(e)
            # 上下文超限 → 自动压缩
            if ("context" in err.lower() or "too long" in err.lower()
                    or "400" in err or "maximum" in err.lower()):
                log.warning(f"[上下文超限] 触发自动压缩: {err[:100]}")
                if conv and event:
                    return await _compress_and_retry(
                        client, system_prompt, history, user_message,
                        conv, event, max_rounds, new_messages
                    )
                return "上下文超限，请发送 /clear 清空对话后重试。", new_messages
            raise

        choice = resp.choices[0]
        msg = choice.message

        # 有工具调用
        if choice.finish_reason == "tool_calls" and msg.tool_calls:
            assistant_msg = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    }
                    for tc in msg.tool_calls
                ]
            }
            messages.append(assistant_msg)
            new_messages.append(assistant_msg)

            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                log.info(f"[工具调用] {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:100]})")

                WRITE_TOOLS = {"write_file"}
                if tool_name in WRITE_TOOLS:
                    tool_result = json.dumps({
                        "need_confirm": True,
                        "tool": tool_name,
                        "args": tool_args,
                        "tool_call_id": tc.id,
                    }, ensure_ascii=False)
                else:
                    tool_result = await execute_tool(tool_name, tool_args)
                    log.info(f"[工具结果] {tool_result[:200]}")

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                }
                messages.append(tool_msg)
                new_messages.append(tool_msg)

            if any(
                '"need_confirm": true' in m.get("content", "")
                for m in new_messages if m.get("role") == "tool"
            ):
                return "__NEED_CONFIRM__", new_messages

            continue

        # 普通文本回复
        reply = msg.content or ""
        if reply:
            usage = resp.usage
            if usage:
                log.info(f"API: in={usage.prompt_tokens} out={usage.completion_tokens}")
            new_messages.append({"role": "assistant", "content": reply})
            return reply, new_messages

    return "处理轮次过多，请简化问题。", new_messages


async def _compress_and_retry(
    client: AsyncOpenAI,
    system_prompt: str,
    history: list[dict],
    user_message: str,
    conv: ConversationManager,
    event: Event,
    max_rounds: int,
    current_new: list[dict],
) -> tuple[str, list[dict]]:
    """上下文超限时：压缩旧历史 → 重置 → 重新对话。"""
    key = conv._key(event)

    # 1. 取出要压缩的历史（全部旧历史 + 当前消息）
    old = conv._histories.get(key, [])

    # 2. 生成压缩请求
    compress_msgs, _ = conv.compress(event, old)

    # 3. 调用模型压缩
    try:
        resp = await client.chat.completions.create(
            model=config["mimo_api"]["model"],
            messages=compress_msgs,
            max_tokens=512,
            temperature=0.3,
        )
        summary = resp.choices[0].message.content or "对话已压缩。"
        log.info(f"[压缩] 生成摘要: {summary[:200]}")
    except Exception as e:
        log.error(f"[压缩] 生成摘要失败: {e}")
        summary = "之前的对话因上下文超限被压缩。"

    # 4. 保存摘要，清空历史
    conv.finish_compress(key, summary, [])

    # 5. 用新对话重新调用
    new_history = conv.get(event)
    return await chat_with_tools(
        client, system_prompt, new_history, user_message,
        conv=conv, event=event, max_rounds=max_rounds,
    )

# ───────────────────── 主程序 ─────────────────────

def main():
    # 初始化组件
    memory = MemoryLoader(
        memory_dir=config["bot"]["memory_dir"],
        extra_persona=config["bot"]["system_prompt"],
    )
    conv = ConversationManager(
        max_rounds=config["bot"]["max_history_rounds"],
        save_path=str(Path(__file__).parent / "conversations.json"),
    )
    confirms = PendingConfirmations()

    client = AsyncOpenAI(
        base_url=config["mimo_api"]["base_url"],
        api_key=config["mimo_api"]["api_key"],
    )

    bot = CQHttp(enable_socket_adapter=True)

    async def get_reply_context(event: Event) -> str:
        """如果消息是回复某条消息，获取被回复消息的内容。"""
        if not isinstance(event.message, list):
            return ""
        for seg in event.message:
            if seg.get("type") == "reply":
                reply_id = seg.get("data", {}).get("id")
                if reply_id:
                    try:
                        msg_info = await bot.call_action(action="get_msg", message_id=reply_id)
                        # 提取被回复消息的文本
                        reply_segments = msg_info.get("message", [])
                        parts = []
                        for rs in reply_segments:
                            if isinstance(rs, dict) and rs.get("type") == "text":
                                parts.append(rs.get("data", {}).get("text", ""))
                        if parts:
                            return f"[被引用的消息]: {''.join(parts)}\n"
                    except Exception as e:
                        log.warning(f"获取引用消息失败: {e}")
        return ""

    async def handle_message(event: Event, text: str):
        uid = event.user_id

        # ── Y/N 确认处理 ──
        if text.upper() in ("Y", "N", "YES", "NO"):
            pending = confirms.get(uid)
            if pending:
                confirms.remove(uid)
                if text.upper() in ("Y", "YES"):
                    await bot.send(event, "确认执行...")
                    result = await execute_tool(pending["tool"], pending["args"])
                    log.info(f"[确认执行] {pending['tool']}: {result[:200]}")
                    await bot.send(event, f"已完成: {result[:500]}")

                    # 把确认结果加入对话历史
                    tool_result_msg = {
                        "role": "tool",
                        "tool_call_id": "confirmed",
                        "content": result,
                    }
                    # 让模型生成最终回复
                    system_prompt = memory.load()
                    history = conv.get(event)
                    history.append(tool_result_msg)
                    reply, new_msgs = await chat_with_tools(client, system_prompt, history, "工具已执行完成，请根据结果回复用户。", conv=conv, event=event)
                    if reply and reply != "__NEED_CONFIRM__":
                        conv.add(event, new_msgs)
                        await send_reply(bot, event, reply)
                    return
                else:
                    await bot.send(event, "已取消。")
                    return

        # ── /clear 命令 ──
        if text == config["bot"]["clear_command"]:
            conv.clear(event)
            conv.save()  # 立即保存清空后的状态
            await bot.send(event, "对话历史已清空。")
            return

        # ── 正常对话 ──
        log.info(f"[{'群聊' if event.detail_type == 'group' else '私聊'}] QQ:{event.user_id} → {text[:80]}")

        # 如果是引用回复，获取被引用消息的内容
        reply_context = await get_reply_context(event)
        if reply_context:
            text = reply_context + text

        system_prompt = memory.load()
        history = conv.get(event)

        reply, new_msgs = await chat_with_tools(client, system_prompt, history, text, conv=conv, event=event)

        if reply == "__NEED_CONFIRM__":
            # 找到需要确认的工具
            for m in reversed(new_msgs):
                if m.get("role") == "tool" and '"need_confirm"' in m.get("content", ""):
                    confirm_info = json.loads(m["content"])
                    tool_name = confirm_info["tool"]
                    tool_args = confirm_info["args"]

                    # 设置超时
                    async def timeout():
                        await asyncio.sleep(300)
                        confirms.remove(uid)
                        await send_reply(bot, event, "操作确认已超时（5分钟），自动取消。")

                    timer = asyncio.ensure_future(timeout())
                    confirms.add(uid, tool_name, tool_args, event, timer)

                    # 发确认消息
                    preview = json.dumps(tool_args, ensure_ascii=False, indent=2)
                    if len(preview) > 500:
                        preview = preview[:500] + "..."
                    await send_reply(bot, event,
                        f"需要确认操作：\n"
                        f"工具: {tool_name}\n"
                        f"参数:\n{preview}\n\n"
                        f"回复 Y 确认 / N 取消（5分钟内有效）"
                    )
                    return

        if reply:
            conv.add(event, new_msgs)
            await send_reply(bot, event, reply)

    @bot.on_message("private")
    async def handle_private(event: Event):
        if not is_allowed(event):
            return
        text = extract_text(event.message)
        if not text:
            return
        await handle_message(event, text)

    @bot.on_message("group")
    async def handle_group(event: Event):
        if not is_allowed(event):
            return
        if not should_respond_group(event):
            return
        text = extract_text(event.message)
        if not text:
            return
        await handle_message(event, text)

    @bot.on_meta_event("lifecycle")
    async def handle_lifecycle(event: Event):
        log.info(f"NapCat: {event.get('sub_type', 'unknown')}")

    # 自动保存：每 60 秒存盘一次
    async def auto_save():
        while True:
            await asyncio.sleep(60)
            conv.save()

    @bot.on_startup
    async def start_tasks():
        asyncio.ensure_future(auto_save())

    log.info("QQ Bot 启动中，等待 NapCat 连接...")
    bot.run(host="127.0.0.1", port=config["bot"]["port"])


if __name__ == "__main__":
    main()
