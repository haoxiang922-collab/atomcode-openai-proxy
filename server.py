"""
AtomCode -> OpenAI / Claude API 反向代理

把 AtomCode CLI (atomcode -p 无头模式) 包装成 OpenAI 和 Claude 兼容 API，
供 SillyTavern / Hermes / Cherry Studio 等支持自定义端点的客户端直接调用。

架构:
    客户端 (SillyTavern/Hermes/...)
        │  POST /v1/chat/completions  (OpenAI 格式)
        │  POST /v1/messages          (Claude 格式)
        ▼
    本服务 (FastAPI, 默认 0.0.0.0:8787)
        │  子进程: atomcode -p "<拼好的 prompt>" --max-turns N -y --no-telemetry
        │  + 429/5xx 自动重试 + 流式抗截断续写
        ▼
    AtomCode CLI  →  GLM-5.2  →  stdout
        │
        ▼
    包装成 OpenAI ChatCompletion / Claude Message / SSE 流式响应返回

注意:
- AtomCode 无头模式本身是无状态的（每次 -p 都是新会话），所以多轮对话靠客户端把
  完整 messages 历史发上来，本服务把历史拼成单一 prompt 文本喂给 -p。
- 工具调用 (tool_calls) 不支持 —— AtomCode 无头模式下工具调用对客户端不可见，
  这里只取最终文本输出。
"""

from __future__ import annotations

import os
import re
import json
import time
import uuid
import asyncio
import logging
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

ATOMCODE_BIN = os.environ.get("ATOMCODE_BIN", "atomcode")
DEFAULT_MODEL = os.environ.get("ATOMCODE_MODEL", "glm-5.2")
HOST = os.environ.get("ATOMCODE_PROXY_HOST", "0.0.0.0")
PORT = int(os.environ.get("ATOMCODE_PROXY_PORT", "8787"))
# 可选鉴权：设了才校验（逗号分隔多个有效 key）；不设则放行（仅本地用）
AUTH_KEYS = {k.strip() for k in os.environ.get("ATOMCODE_PROXY_API_KEY", "").split(",") if k.strip()}
# 子进程超时（秒）
REQUEST_TIMEOUT = int(os.environ.get("ATOMCODE_PROXY_TIMEOUT", "600"))
# 默认最大 LLM 回合数（对应 --max-turns）
DEFAULT_MAX_TURNS = int(os.environ.get("ATOMCODE_MAX_TURNS", "3"))

# === gcli2api 借鉴的四项增强 ===
# 兼容性模式：把 system 消息转成 user 消息（某些客户端不认 system role）
COMPATIBILITY_MODE = os.environ.get("COMPATIBILITY_MODE", "false").lower() in ("1", "true", "yes", "on")
# 429/5xx 自动重试：对子进程非零退出码做有限重试
RETRY_ENABLED = os.environ.get("RETRY_ENABLED", "true").lower() in ("1", "true", "yes", "on")
RETRY_MAX = int(os.environ.get("RETRY_MAX", "3"))
RETRY_INTERVAL = float(os.environ.get("RETRY_INTERVAL", "1.0"))
# 流式抗截断：检测答案不完整 → 自动续写补全
ANTI_TRUNCATION_ENABLED = os.environ.get("ANTI_TRUNCATION_ENABLED", "true").lower() in ("1", "true", "yes", "on")
ANTI_TRUNCATION_MAX_ATTEMPTS = int(os.environ.get("ANTI_TRUNCATION_MAX_ATTEMPTS", "3"))
# 答案完整性的粗略启发式：以这些标点结尾视为可能完整
_COMPLETE_ENDINGS = ("。", "！", "？", ".", "!", "?", "```", ")", "）", "\n")

logging.basicConfig(
    level=os.environ.get("ATOMCODE_PROXY_LOG", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("atomcode-proxy")

# --------------------------------------------------------------------------- #
# OpenAI 请求/响应模型
# --------------------------------------------------------------------------- #


class ChatMessage(BaseModel):
    role: str
    content: str | None = None
    name: str | None = None


# --------------------------------------------------------------------------- #
# AtomCode 子进程调用
# --------------------------------------------------------------------------- #

# 去掉 AtomCode 启动时打印的这些非答案噪声行
_NOISE_PREFIXES = (
    "[engine",
    "[headless]",
    "[daemon]",
)


def _filter_noise(line: str) -> str | None:
    """过滤 CLI 启动噪声行，返回 None 表示该行应丢弃。"""
    s = line.rstrip("\r\n")
    if any(s.startswith(p) for p in _NOISE_PREFIXES):
        return None
    return s


def _messages_to_prompt(messages: list[ChatMessage], max_turns: int) -> str:
    """
    把 OpenAI/Claude messages 数组拼成 AtomCode -p 能吃的单段文本。

    约定:
      - system 消息 → 作为最前置的指令块（COMPATIBILITY_MODE 时降级为 user）
      - user/assistant 交替 → 拼成对话历史
      - 当前轮（最后一条 user）作为实际提问
    """
    parts: list[str] = []
    system_parts: list[str] = []
    for m in messages:
        content = (m.content or "").strip()
        if not content:
            continue
        if m.role == "system":
            if COMPATIBILITY_MODE:
                # 兼容性模式：system 降级成 user，避免某些客户端/后端不认 system role
                parts.append(f"用户：\n[系统指令] {content}")
            else:
                system_parts.append(content)
        elif m.role == "user":
            parts.append(f"用户：\n{content}")
        elif m.role == "assistant":
            parts.append(f"助手：\n{content}")
        else:
            parts.append(f"{m.role}：\n{content}")

    header = ""
    if system_parts:
        header = "【系统指令】\n" + "\n\n".join(system_parts) + "\n\n"
    body = "\n\n".join(parts)
    footer = (
        "\n\n请根据上方对话上下文回答最后一条用户消息。"
        "只输出回答正文，不要复述问题，不要输出思考过程。"
    )
    return header + body + footer


def _looks_truncated(text: str) -> bool:
    """粗略判断答案是否被截断：非空且不以完整标点结尾视为可能被截断。"""
    if not text or not text.strip():
        return False
    tail = text.rstrip()
    # 以代码块、列表项、明显未结束的标点结尾 → 视为截断
    if tail.endswith(("```", ")", "）")):
        return False
    return not tail.endswith(_COMPLETE_ENDINGS)


async def _run_atomcode_once(prompt: str, max_turns: int, workdir: str | None = None) -> tuple[int, str, str]:
    """
    单次调用 atomcode -p 子进程，返回 (退出码, stdout 清洗文本, stderr)。
    不含重试逻辑，由上层 _run_atomcode 统一重试。
    """
    cmd = [
        ATOMCODE_BIN,
        "-p", prompt,
        "--max-turns", str(max_turns),
        "-y",                 # 跳过所有权限提示（无头模式必须）
        "--no-telemetry",
    ]
    if workdir:
        cmd += ["-C", workdir]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,   # 避免 "不支持输入重新定向" 报错
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=REQUEST_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(504, f"atomcode 子进程超时 ({REQUEST_TIMEOUT}s)")

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    # 过滤噪声行
    lines = [_filter_noise(l) for l in stdout.splitlines()]
    lines = [l for l in lines if l is not None]
    cleaned = "\n".join(lines).strip()
    return proc.returncode or 0, cleaned, stderr


def _is_retryable(code: int, stderr: str) -> bool:
    """判断子进程退出码/stderr 是否对应可重试的瞬时错误（429/5xx）。"""
    if code == 0:
        return False
    s = stderr.lower()
    # 限流、服务端错误、临时不可用
    return any(k in s for k in ("429", "rate limit", "too many requests",
                                "500", "502", "503", "504", "529",
                                "internal error", "unavailable", "timeout"))


async def _run_atomcode(prompt: str, max_turns: int, workdir: str | None = None) -> str:
    """
    调用 atomcode -p，带 429/5xx 自动重试 + 流式抗截断续写。
    非流式：等子进程跑完一次性返回完整文本。
    """
    last_err = ""
    text = ""
    # 第一阶段：重试拿到至少一次成功输出
    for attempt in range(1, RETRY_MAX + 1 if RETRY_ENABLED else 1):
        t0 = time.time()
        code, text, stderr = await _run_atomcode_once(prompt, max_turns, workdir)
        log.info("atomcode 尝试 %d/%d 退出码=%d 耗时=%.1fs stdout=%dB",
                 attempt, RETRY_MAX if RETRY_ENABLED else 1, code, time.time() - t0, len(text))
        if code == 0 and text:
            break
        last_err = stderr[-500:] or f"exit {code}"
        if not RETRY_ENABLED or not _is_retryable(code, stderr):
            raise HTTPException(502, f"atomcode 失败 (exit {code}): {last_err}")
        if attempt < RETRY_MAX:
            log.warning("可重试错误，%ss 后第 %d 次重试", RETRY_INTERVAL, attempt + 1)
            await asyncio.sleep(RETRY_INTERVAL)
    else:
        if not text:
            raise HTTPException(502, f"atomcode 重试 {RETRY_MAX} 次仍失败: {last_err}")

    # 第二阶段：流式抗截断 —— 答案看起来被截断则续写补全
    if ANTI_TRUNCATION_ENABLED and _looks_truncated(text):
        text = await _anti_truncation_continue(prompt, text, max_turns, workdir)

    return text


async def _anti_truncation_continue(original_prompt: str, partial: str, max_turns: int, workdir: str | None) -> str:
    """
    抗截断：检测到 partial 不完整时，让 AtomCode 在 partial 基础上继续写完。
    最多续写 ANTI_TRUNCATION_MAX_ATTEMPTS 次，每次把已生成内容作为上下文喂回去。
    """
    current = partial
    for attempt in range(1, ANTI_TRUNCATION_MAX_ATTEMPTS + 1):
        if not _looks_truncated(current):
            break
        log.info("[抗截断] 第 %d 次续写，当前长度=%d", attempt, len(current))
        continue_prompt = (
            f"{original_prompt}\n\n"
            f"【已生成内容（可能被截断，请直接从最后位置续写补全，不要重复已有内容）】\n"
            f"{current}\n\n"
            f"【请继续补完整上述内容，只输出续写部分】"
        )
        try:
            code, continuation, stderr = await _run_atomcode_once(continue_prompt, max_turns, workdir)
        except HTTPException:
            break  # 超时等错误不再续写，保留已有内容
        if code != 0 or not continuation:
            break
        # 拼接续写部分（去掉续写可能重复的开头）
        current = _merge_continuation(current, continuation)
        if not _looks_truncated(current):
            break
    return current


def _merge_continuation(prev: str, cont: str) -> str:
    """
    把续写内容 cont 接到 prev 后面，去掉重叠部分。
    简单策略：找 cont 开头与 prev 结尾的最长公共子串重叠，去掉重复。
    """
    if not cont:
        return prev
    # 尝试找重叠：prev 尾部与 cont 头部的最长公共片段
    max_overlap = min(len(prev), len(cont), 64)
    overlap = 0
    for n in range(max_overlap, 0, -1):
        if prev.endswith(cont[:n]):
            overlap = n
            break
    return prev + cont[overlap:]


async def _run_atomcode_stream(prompt: str, max_turns: int, workdir: str | None = None) -> AsyncIterator[str]:
    """
    流式版本：按行读取 atomcode stdout，每凑到一行就 yield 一段文本。
    AtomCode 无头模式本身不增量输出（它是 agent 循环，最后才打印完整答案），
    所以这里的 "流式" 实际是拿到完整答案后按行切片模拟流式，保证客户端 SSE 协议兼容。
    抗截断：收集完整输出后若发现截断，先续写补全再切片输出。
    """
    # 先用非流式方式拿到完整（含抗截断）文本，再按行模拟流式推送
    # 这是 AtomCode 无头模式的现实：它不会增量吐 token，只能整段拿
    text = await _run_atomcode(prompt, max_turns, workdir)
    # 按行推送，每行作为一个 chunk
    lines = text.splitlines()
    for line in lines:
        yield line
    if not text.strip():
        yield ""


# --------------------------------------------------------------------------- #
# OpenAI / Claude 响应构造
# --------------------------------------------------------------------------- #

def _gen_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _now() -> int:
    return int(time.time())


def _build_openai_full(text: str, model: str) -> dict[str, Any]:
    return {
        "id": _gen_id(),
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _build_openai_chunk(text: str, model: str, finish: str | None = None) -> dict[str, Any]:
    return {
        "id": _gen_id(),
        "object": "chat.completion.chunk",
        "created": _now(),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": text} if text else {},
            "finish_reason": finish,
        }],
    }


def _build_claude_full(text: str, model: str, req_id: str | None = None) -> dict[str, Any]:
    return {
        "id": req_id or _gen_id("msg"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _build_claude_stream_events(text: str, model: str, req_id: str | None = None) -> list[dict[str, Any]]:
    """
    Claude 流式协议是事件序列：message_start → content_block_start →
    若干 content_block_delta → content_block_stop → message_delta → message_stop。
    这里把完整答案作为一个大 delta 推出。
    """
    msg_id = req_id or _gen_id("msg")
    block_id = _gen_id("block")
    events = [
        {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
    ]
    if text:
        events.append({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": text}})
    events += [
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 0}},
        {"type": "message_stop"},
    ]
    return events


# --------------------------------------------------------------------------- #
# FastAPI 路由
# --------------------------------------------------------------------------- #

app = FastAPI(title="AtomCode OpenAI/Claude Proxy", version="0.2.0")


def _check_auth(authorization: str | None, x_api_key: str | None = None) -> None:
    """同时支持 Authorization: Bearer 和 x-api-key（Claude 协议用）。"""
    if not AUTH_KEYS:
        return  # 未配置鉴权，放行
    token = None
    if authorization:
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization.strip()
    elif x_api_key:
        token = x_api_key.strip()
    if not token or token not in AUTH_KEYS:
        raise HTTPException(401, "invalid or missing api key")


def _parse_messages(data: dict) -> tuple[list[ChatMessage], str]:
    """
    从请求体解析出 messages 列表和模型名。
    同时兼容 OpenAI 格式 (messages) 和 Claude 格式 (messages + 顶层 system)。
    """
    model = data.get("model", DEFAULT_MODEL)
    raw_msgs = data.get("messages", [])
    if not raw_msgs:
        raise HTTPException(400, "messages 不能为空")
    try:
        messages = [ChatMessage(**m) for m in raw_msgs]
    except Exception as e:
        raise HTTPException(400, f"invalid messages: {e}")
    # Claude 协议：system 是顶层字段，转成第一条 system message
    sys_field = data.get("system")
    if sys_field:
        if isinstance(sys_field, list):  # Claude 支持 system 为 content block 数组
            sys_text = " ".join(
                b.get("text", "") for b in sys_field if isinstance(b, dict)
            )
        else:
            sys_text = str(sys_field)
        if sys_text.strip():
            messages.insert(0, ChatMessage(role="system", content=sys_text))
    return messages, model


@app.get("/health")
async def health():
    return {
        "status": "ok", "backend": "atomcode", "model": DEFAULT_MODEL,
        "compatibility_mode": COMPATIBILITY_MODE,
        "retry": RETRY_ENABLED, "anti_truncation": ANTI_TRUNCATION_ENABLED,
    }


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    return {
        "object": "list",
        "data": [{
            "id": DEFAULT_MODEL, "object": "model",
            "created": _now(), "owned_by": "atomcode",
        }],
    }


# === OpenAI 端点 ===

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_auth(authorization)
    try:
        raw = await request.body()
        data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"invalid JSON body: {e}")
    if not isinstance(data, dict):
        raise HTTPException(400, "body must be a JSON object")

    messages, model = _parse_messages(data)
    stream = bool(data.get("stream", False))
    max_turns = DEFAULT_MAX_TURNS
    prompt = _messages_to_prompt(messages, max_turns)
    workdir = os.environ.get("ATOMCODE_WORKDIR")

    if not stream:
        text = await _run_atomcode(prompt, max_turns, workdir)
        return JSONResponse(_build_openai_full(text, model))

    async def event_gen():
        try:
            yield {"data": json.dumps(_build_openai_chunk("", model))}
            async for piece in _run_atomcode_stream(prompt, max_turns, workdir):
                if piece:
                    yield {"data": json.dumps(_build_openai_chunk(piece + "\n", model))}
            yield {"data": json.dumps(_build_openai_chunk("", model, finish="stop"))}
            yield {"data": "[DONE]"}
        except Exception as e:
            log.exception("流式生成失败")
            yield {"data": json.dumps(_build_openai_chunk(f"\n[proxy error: {e}]\n", model, finish="stop"))}
            yield {"data": "[DONE]"}

    return EventSourceResponse(event_gen())


# === Claude 端点 ===

@app.post("/v1/messages")
async def claude_messages(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    _check_auth(authorization, x_api_key)
    try:
        raw = await request.body()
        data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"invalid JSON body: {e}")
    if not isinstance(data, dict):
        raise HTTPException(400, "body must be a JSON object")

    messages, model = _parse_messages(data)
    stream = bool(data.get("stream", False))
    req_id = data.get("id") or _gen_id("msg")
    max_turns = DEFAULT_MAX_TURNS
    prompt = _messages_to_prompt(messages, max_turns)
    workdir = os.environ.get("ATOMCODE_WORKDIR")

    if not stream:
        text = await _run_atomcode(prompt, max_turns, workdir)
        return JSONResponse(_build_claude_full(text, model, req_id))

    async def event_gen():
        try:
            text = await _run_atomcode(prompt, max_turns, workdir)
            for evt in _build_claude_stream_events(text, model, req_id):
                yield {"data": json.dumps(evt)}
        except Exception as e:
            log.exception("Claude 流式生成失败")
            err_evt = {"type": "error", "error": {"type": "internal_error", "message": str(e)}}
            yield {"data": json.dumps(err_evt)}

    return EventSourceResponse(event_gen())


@app.get("/")
async def root():
    return {
        "service": "atomcode-openai-proxy", "version": "0.2.0",
        "endpoints": ["/v1/chat/completions", "/v1/messages", "/v1/models", "/health"],
        "features": {
            "claude_format": True,
            "compatibility_mode": COMPATIBILITY_MODE,
            "retry": RETRY_ENABLED,
            "anti_truncation": ANTI_TRUNCATION_ENABLED,
        },
    }


if __name__ == "__main__":
    import uvicorn
    log.info("启动 AtomCode OpenAI/Claude 反代 on %s:%d (model=%s, auth=%s, compat=%s, retry=%s, anti_trunc=%s)",
             HOST, PORT, DEFAULT_MODEL, "on" if AUTH_KEYS else "off",
             COMPATIBILITY_MODE, RETRY_ENABLED, ANTI_TRUNCATION_ENABLED)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
