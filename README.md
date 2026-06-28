# AtomCode → OpenAI / Claude 反向代理

把 **AtomCode CLI**（终端原生的 AI 编程助手，默认跑 GLM-5.2）包装成 **OpenAI 和 Claude 兼容 API**，让 SillyTavern / Hermes / Cherry Studio / NextChat 等支持自定义端点的客户端能直接通过 API 调用它。

参考了 [gcli2api](https://github.com/su-kaka/gcli2api) 的设计，借鉴了其中四项实用增强。

## 原理

```
客户端 (SillyTavern / Hermes / Cherry Studio / ...)
   │  POST /v1/chat/completions   (OpenAI 格式)
   │  POST /v1/messages           (Claude 格式)
   ▼
本服务 (FastAPI, 默认 0.0.0.0:8787)
   │  prompt 写入临时文件 → atomcode --prompt-file <file> --provider <model> --max-turns N -y --no-telemetry
   │  （用 --prompt-file 绕开 Windows 命令行 32767 字符硬限制，支持酒馆长上下文）
   │  + 429/5xx 自动重试 + 流式抗截断续写
   ▼
AtomCode CLI  →  GLM-5.2 / deepseek-v4-flash / qwen3-vl-8b-instruct (走你已登录的 AtomGit CodingPlan 额度)
   │  stdout 纯文本答案
   ▼
包装成 OpenAI ChatCompletion / Claude Message / SSE 流式响应 → 返回客户端
```

AtomCode 的 `daemon` 模式（端口 13456）只给 IDE 插件用，不暴露 LLM 端点，所以这里走 **`atomcode --prompt-file` 无头子进程**模式——这是官方确认的非交互入口。用临时文件传 prompt 而非 `-p` 命令行参数，是为了绕开 Windows `CreateProcessW` 的 32767 字符命令行硬限制（酒馆角色卡+世界书+长对话很容易超这个限制报 `WinError 206`）。

## 前置要求

1. 已安装 AtomCode 并登录：
   ```cmd
   atomcode login
   atomcode status   :: 确认显示 Logged in as: ...
   ```
2. Python 3.10+，已装依赖：
   ```cmd
   pip install -r requirements.txt
   ```

## 启动

```cmd
python server.py
```

默认监听 `0.0.0.0:8787`。

## 端点

| 端点 | 协议 | 说明 |
|------|------|------|
| `POST /v1/chat/completions` | OpenAI | 标准 ChatCompletion，支持流式/非流式 |
| `POST /v1/messages` | Claude | Anthropic Message 协议，支持顶层 system、x-api-key 头、流式事件 |
| `GET /v1/models` | OpenAI | 模型列表（含三个模型 + 上下文窗口） |
| `GET /health` | — | 健康检查 + 功能开关状态 |

## 支持的模型

客户端在 `model` 字段填对应名字即可切换，反代会通过 `--provider` 调对应后端：

| model 名 | 实际模型 | 上下文窗口 | 用途 |
|---|---|---|---|
| `glm-5.2` | GLM-5.2 | 200K | 默认，编程强 |
| `deepseek-v4-flash` | deepseek-v4-flash | **1M** | 长上下文（百万 token） |
| `qwen3-vl-8b-instruct` | Qwen3-VL-8B-Instruct | 64K | 视觉模型 |

三个模型都走 AtomGit CodingPlan 额度，无需额外配置。`deepseek-v4-flash` 的 1M 上下文是 CodingPlan Pro 原生支持，不用任何特殊操作。

## 客户端配置示例

### SillyTavern（OpenAI 格式）
- API 类型：**Chat Completion (OpenAI)**
- 自定义端点：`http://127.0.0.1:8787/v1`
- API Key：随便填（未启用鉴权时）；启用鉴权则填你设的 key
- 模型：`glm-5.2` / `deepseek-v4-flash` / `qwen3-vl-8b-instruct`（三选一，见上表）

### Hermes / Cherry Studio / NextChat（OpenAI 格式）
- API Base URL：`http://127.0.0.1:8787/v1`
- API Key：同上
- 模型名：同上

### Claude 协议客户端
- API Base URL：`http://127.0.0.1:8787`
- 端点：`/v1/messages`
- 认证头：`x-api-key: <你的key>` 或 `Authorization: Bearer <你的key>`
- 模型名：同上

## 四项增强功能（借鉴自 gcli2api）

### 1. Claude 格式端点 `/v1/messages`
完整支持 Anthropic Message API 规范：
- 顶层 `system` 字段自动转成第一条 system message
- `x-api-key` 头和 `Authorization: Bearer` 双认证方式
- 流式响应按 Claude SSE 事件序列输出：`message_start → content_block_start → content_block_delta → content_block_stop → message_delta → message_stop`

### 2. 兼容性模式
某些客户端/后端不认 `system` role。开启后所有 system 消息降级为 user 消息：
```cmd
set COMPATIBILITY_MODE=true
```

### 3. 429/5xx 自动重试
子进程遇到限流（429）或服务端错误（500/502/503/504/529）时自动重试：
```cmd
set RETRY_ENABLED=true         :: 默认开
set RETRY_MAX=3                :: 最大重试次数
set RETRY_INTERVAL=1.0         :: 重试间隔秒数
```

### 4. 流式抗截断
检测答案不完整（不以句号/问号等完整标点结尾）时，自动让 AtomCode 在已有内容基础上续写补全，最多续写 3 次：
```cmd
set ANTI_TRUNCATION_ENABLED=true       :: 默认开
set ANTI_TRUNCATION_MAX_ATTEMPTS=3     :: 最大续写次数
```

## 环境变量一览

| 变量 | 默认 | 说明 |
|------|------|------|
| `ATOMCODE_BIN` | `atomcode` | atomcode 可执行文件路径（不在 PATH 时填全路径） |
| `ATOMCODE_MODEL` | `glm-5.2` | 暴露给客户端的模型名（仅展示用） |
| `ATOMCODE_PROXY_HOST` | `0.0.0.0` | 监听地址 |
| `ATOMCODE_PROXY_PORT` | `8787` | 监听端口 |
| `ATOMCODE_PROXY_API_KEY` | 空 | 鉴权 key，逗号分隔多个；**不设则不鉴权** |
| `ATOMCODE_PROXY_TIMEOUT` | `600` | 子进程超时秒数 |
| `ATOMCODE_MAX_TURNS` | `3` | atomcode agent 循环最大回合数 |
| `ATOMCODE_WORKDIR` | 空 | atomcode 工作目录（不设则用当前目录） |
| `COMPATIBILITY_MODE` | `false` | system 消息降级为 user |
| `RETRY_ENABLED` | `true` | 启用 429/5xx 自动重试 |
| `RETRY_MAX` | `3` | 最大重试次数 |
| `RETRY_INTERVAL` | `1.0` | 重试间隔秒数 |
| `ANTI_TRUNCATION_ENABLED` | `true` | 启用流式抗截断续写 |
| `ANTI_TRUNCATION_MAX_ATTEMPTS` | `3` | 最大续写次数 |
| `ATOMCODE_PROXY_LOG` | `INFO` | 日志级别 |

## 测试

OpenAI 非流式：
```cmd
curl -X POST http://127.0.0.1:8787/v1/chat/completions ^
  -H "content-type: application/json" ^
  -d "{\"model\":\"glm-5.2\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

Claude 非流式：
```cmd
curl -X POST http://127.0.0.1:8787/v1/messages ^
  -H "content-type: application/json" -H "x-api-key: test" ^
  -d "{\"model\":\"glm-5.2\",\"max_tokens\":100,\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

流式加 `"stream":true` 即可。

## 已知限制

- **无状态会话**：AtomCode `-p` 每次都是新会话，多轮对话靠客户端把完整 messages 历史发上来，本服务拼成单段 prompt 喂进去。
- **不支持 tool_calls**：无头模式下工具调用过程对客户端不可见，只取最终文本答案。
- **"流式" 是模拟的**：AtomCode agent 循环结束后才打印完整答案，本服务拿到完整答案后按行切片推送，首字延迟等于 agent 完整执行时间。抗截断续写会在幕后静默补全。
- **额度走你的 AtomGit CodingPlan**：所有请求消耗你已登录账号的额度，不是免费的。

## 安全提示

- 默认不鉴权且监听 `0.0.0.0`，**仅在本地使用**。要暴露到公网务必设 `ATOMCODE_PROXY_API_KEY` 并改监听 `127.0.0.1` + 套反代。
- `-y` 会自动批准所有工具调用（无头模式必须）。建议设 `ATOMCODE_WORKDIR` 指向一个空目录，限制 agent 的活动范围。
