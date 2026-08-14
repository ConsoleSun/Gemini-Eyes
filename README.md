# gemini-web-mcp

> ## ⚠️ 风险提示
>
> **此仓库仅用于学习交流，请合法使用，如遇任何问题与作者无关。**
>
> 本项目是对 Gemini 网页端内部接口的逆向封装，非官方 API；使用浏览器会话 Cookie
> 自动化访问可能违反相关服务条款，请自行评估风险并遵守当地法律法规。
> 请勿将本项目用于商业用途、批量抓取、绕过付费限制或任何侵犯他人权益的行为。

**让 Agent 借 Gemini 的"眼睛"和"手"** —— 一个 MCP（Model Context Protocol）服务，把
[gemini.google.com](https://gemini.google.com) 网页端的能力暴露给任何 Agent：

- 🗣️ 对话、多轮上下文
- 👁️ **看图 / 看视频**（上传本地文件，Gemini 识别后返回描述）
- 🎨 **生图**（Imagen）与 🎬 **生视频**（Veo），自动下载成品到本地
- 💬 会话管理（列出 / 读取 / 删除历史对话）
- 🔄 **`__Secure-1PSIDTS` 自动续期**：通过 `accounts.google.com/RotateCookies` 每 25 分钟
  后台旋转短效令牌并持久化回 cookie 文件——只要服务常驻，令牌不再过期

**核心特性**：不走官方 API —— 不需要 API Key、不产生 API 计费。它"反编译"本地浏览器
（Chrome / Edge / Chromium）里已登录的 Google 会话 Cookie，原样重放 Gemini 网页端的
内部 RPC 请求，与你在浏览器里使用完全等价（共享账号历史、共享生成额度）。

> ⚠️ 本项目是对 Gemini 网页端内部接口的逆向封装，非官方 API。仅限个人学习与自动化，
> 行为受 Google 服务条款约束，账号风险自负。请勿滥用。

---

## 工作原理

```
┌────────────┐   MCP (stdio / HTTP)   ┌──────────────────┐
│   Agent     │ ◄────────────────────► │  gemini-web-mcp   │
└────────────┘                        └────────┬─────────┘
                                               │ requests（携带解密的 Cookie）
                                               ▼
                                    ┌──────────────────────────┐
                                    │   gemini.google.com 网页端 │
                                    │  StreamGenerate /         │
                                    │  batchexecute / 上传服务   │
                                    └──────────────────────────┘
```

三步：

1. **反编译 Cookie**（`cookie_extractor.py`）
   - 从浏览器配置目录找到 `Local State`（密钥文件）与 `Cookies`（SQLite 数据库）
   - 按平台解密：**Windows** DPAPI → AES-256-GCM；**macOS** 钥匙串 "Chrome Safe
     Storage" → PBKDF2 → AES-128-CBC；**Linux** 硬编码口令 `"peanuts"` → AES-256-GCM
   - 逐条解出 `v10` 前缀的 AES-GCM Cookie；旧版明文/base64 直接读
   - 先复制数据库到临时文件再读，避免浏览器运行时的 SQLite 锁

2. **换令牌**（`gemini_client.py`）：访问 `https://gemini.google.com/app`，从页面提取
   `SNlM0e`（CSRF 令牌）、`cfb2h`（build label）、`FdrFJe`（session id），带 TTL 缓存。

3. **调用网页端 RPC**
   - 对话：POST `/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate`
     流式接口，按 Google 的**长度前缀分帧协议**解析（长度标记含数字后换行 + JSON +
     JSON 后换行，按 UTF-16 单位计数）
   - 文件：**两阶段 resumable 上传**（`push.clients6.google.com`，`start` 拿上传 URL →
     `upload, finalize` 传内容），文件条目携带类型枚举
   - 媒体：从候选结构 `[12][7]`（生成图）、`[12][8]["60"]`（生成视频真实下载 URL）、
     `[12][1]`（引用图）解析，并支持渲染期的 206 重试下载
   - 会话管理：POST `/_/BardChatUi/data/batchexecute`（列/读/删对话 RPC）

4. **令牌自动续期**：`__Secure-1PSIDTS` 是 Google 的短效令牌（几小时~几天），服务通过
   POST `https://accounts.google.com/RotateCookies` 每 25 分钟用当前会话换一组新 cookie
   （含新 1PSIDTS），并**持久化回 cookie 文件**——只要服务常驻，令牌永不过期；
   换令牌失败（SNlM0e 解析失败）时也会先旋转再重试，实现自愈。

---

## 目录结构

```
gemini-web-mcp/
├── pyproject.toml              # uv 项目，依赖 mcp / requests / pycryptodome
├── README.md
├── gemini-web.cordis.yml       # DSH 接入配置示例
├── gemini_mcp/
│   ├── __main__.py             # python -m gemini_mcp 入口
│   ├── server.py               # MCP 服务器（9 个工具）
│   ├── cookie_extractor.py     # ★ cookie 反编译（DPAPI/Keychain/peanuts）
│   └── gemini_client.py        # 网页端 RPC 客户端（令牌/对话/上传/媒体）
└── tests/                      # 38 个单元测试（合成加密数据，无需真实浏览器）
```

---

## 安装

需要 Python ≥ 3.10（推荐 [uv](https://docs.astral.sh/uv/)）：

```bash
cd gemini-web-mcp
uv sync --extra dev        # 安装依赖（mcp / requests / pycryptodome）
```

---

## 使用教程

### 第 1 步：准备 Cookie（二选一）

**方式 A：自动提取（推荐，经典加密的浏览器）**

1. 在 Chrome / Edge / Chromium 中登录 `https://gemini.google.com`
2. **关闭浏览器**（避免数据库锁）
3. 验证能否提取：

```bash
uv run python -m gemini_mcp.cookie_extractor --browser chrome --list --domain google.com
uv run python -m gemini_mcp.cookie_extractor --browser chrome --reveal --domain google.com   # 敏感！仅调试
```

**方式 B：手动导出（新版浏览器必读）**

2025 年后的新版 Chrome/Edge 改用 **portal/app-bound 加密**，无法离线解密（程序会明确
提示）。此时用浏览器扩展 **Cookie-Editor**（或同类工具）把 `gemini.google.com` 的
cookie 全量导出为 JSON：

```json
[
  {"name": "__Secure-1PSID", "value": "xxxx", "domain": ".google.com", "path": "/"},
  {"name": "SID", "value": "xxxx", "domain": ".google.com", "path": "/"}
]
```

或简写 `{"__Secure-1PSID": "xxxx", "SID": "xxxx"}`，保存为 `cookies.json`。

**需要哪些 cookie？**

| 类别 | Cookie |
|---|---|
| **必带** | `__Secure-1PSID`（主会话）；若浏览器里有 `__Secure-1PSIDTS` 则必须一起带（缺它 401） |
| **推荐全带** | `SID`、`HSID`、`SSID`、`APISID`、`SAPISID`、`__Secure-1PAPISID`、`__Secure-3PSID`、`__Secure-3PSIDTS`、`__Secure-3PAPISID`、`NID`、`AEC`、`SIDCC`、`__Secure-1PSIDCC`、`1P_JAR` |

> 最省事的做法：**全部导出，一个不落**。`__Secure-1PSIDTS` 是短效令牌（几小时~几天），
> 失效后重新导出即可。

> 💡 **关于 `__Secure-1PSIDTS` 过期**：服务内置自动续期（每 25 分钟通过
> `RotateCookies` 换新并写回 cookie 文件）。**只要服务常驻，不会再过期**；若服务停机
> 超过令牌寿命导致失效，重新导出一次即可，之后恢复自动续期。

### 第 2 步：启动服务

```bash
# stdio（大多数 MCP 客户端用这个）
uv run gemini-mcp --browser chrome
uv run gemini-mcp --cookie-file cookies.json          # 手动导出的 cookie

# Edge / 指定配置文件
uv run gemini-mcp --browser edge --profile "Profile 1"

# HTTP（streamable-http，供远程 Agent 或调试）
uv run gemini-mcp --cookie-file cookies.json --transport http --port 8900

# 完整参数
uv run gemini-mcp --help
```

也可以用环境变量 `GEMINI_COOKIE_FILE=/path/to/cookies.json` 代替 `--cookie-file`。

### 第 3 步：验证连接

> 以下脚本直接读取进程内配置，请先设置 `GEMINI_COOKIE_FILE` 环境变量
> （`export GEMINI_COOKIE_FILE=/path/to/cookies.json`），否则会报告无 cookie。

```bash
uv run python - <<'PY'
import asyncio
from gemini_mcp.server import mcp

async def main():
    result = await mcp.call_tool("gemini_status", {})
    print(result.content[0].text)

asyncio.run(main())
PY
```

`ok: true` 即就绪；它会列出已有哪些 / 缺哪些关键 cookie。

### 第 4 步：接入 MCP 客户端

**Claude Desktop**（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "gemini-web": {
      "command": "/home/you/.local/bin/uv",
      "args": ["run", "--directory", "/path/to/gemini-web-mcp", "gemini-mcp", "--cookie-file", "/path/to/cookies.json"]
    }
  }
}
```

**Cline / Roo Code / 其他 IDE**：同样的 `command` + `args`（stdio），或填
`http://127.0.0.1:8900/mcp`（HTTP 模式）。

### 第 5 步（可选）：接入 DSH（DeepSeek Harness）

DSH 原生支持 MCP 客户端插件，且**实时监控 patch 文件、改动即热加载、无需重启**：

1. 把以下内容合并进 `~/.dsh/profiles/<profile>/cordis.patch.yml`（当前 web profile 即
   `~/.dsh/profiles/web/cordis.patch.yml`；按需把 `command/args` 换成你的路径）：

```yaml
- insert:
    - id: gemini-web
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: gemini-web
        transport: stdio
        command: /home/ubuntuhong/.local/bin/uv
        args:
          - run
          - --directory
          - /home/ubuntuhong/Gemini Eyes/gemini-web-mcp
          - gemini-mcp
          - --cookie-file
          - /tmp/gemini_cookies.json
```

2. 保存后几秒内生效（`ps aux | grep gemini-mcp` 可看到子进程被拉起）
3. 新开会话，工具以 `mcp__gemini-web__*` 出现

项目里另附 `gemini-web.cordis.yml` 可作为 `dsh web --patch` 一次性加载的参考。

---

## 工具清单（9 个）

### 🗣️ 对话

| 工具 | 说明 |
|---|---|
| `gemini_send_message(message, conversation_id?, response_id?, language?, file_paths?)` | 发消息（可附图片/视频文件）；带 `conversation_id` 续聊 |
| `gemini_list_conversations(limit=13)` | 最近对话（含置顶、时间戳） |
| `gemini_read_conversation(conversation_id, limit=10)` | 读取对话轮次（新→旧，含媒体） |
| `gemini_delete_conversation(conversation_id)` | 删除对话（不可恢复） |

### 👁️ 视觉（Gemini 当"眼睛"）

| 工具 | 说明 |
|---|---|
| `gemini_analyze_media(file_path, prompt?)` | 上传图片/视频让 Gemini 识别。**Agent 收到用户上传的媒体时应优先调用它** |
| `gemini_download_media(url, save_path)` | 用登录会话下载媒体（googleusercontent 链接通常需要 cookie） |

### 🎨🎬 生成（Gemini 当"手"）

| 工具 | 说明 |
|---|---|
| `gemini_generate_image(prompt, reference_image?, save_dir?)` | 文生图；传 `reference_image` 图生图。自动下载到本地并返回路径 |
| `gemini_generate_video(prompt, save_dir?, timeout_seconds?)` | 文生视频。异步渲染，工具轮询直到完成，下载到本地返回路径 |

### 🛠️ 其他

| 工具 | 说明 |
|---|---|
| `gemini_status()` | 诊断：cookie 数量、关键 cookie 有无、令牌能否换到 |

---

## 典型工作流

**看图（眼睛）**——用户在 DSH 上传 `photo.jpg`：

```
gemini_analyze_media(file_path="/…/photo.jpg", prompt="描述这张图片")
→ {text: "蓝天下…", conversation_id: "c_…", …}
→ Agent 把描述转述给用户
```

**生图**：

```
gemini_generate_image(prompt="赛博朋克风的猫", save_dir="./media")
→ {media: [{url: "http://googleusercontent…", local_path: "./media/generated_image_…png"}]}
```

**生视频**（渲染 5~15 分钟，工具自动等待）：

```
gemini_generate_video(prompt="一只橘猫在窗台上看雨，电影镜头，5秒")
→ {media: [{url: "https://contribution.usercontent…", local_path: "./media/generated_video_…mp4"}]}
```

**多轮对话**：

```
1. gemini_send_message("帮我写一个快速排序")
   → {text: "…", conversation_id: "c_abc", response_id: "r_1", …}
2. gemini_send_message("改用 C++ 实现", conversation_id: "c_abc", response_id: "r_1")
   → 同一对话的下一轮
```

---

## 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `SNlM0e` 解析失败 / HTTP 400 | **先看能否自愈**：服务会自动尝试 RotateCookies 续期并重试；若返回 401 说明整个会话已失效，需重新导出一次 cookie。**之后只要服务常驻（DSH 挂着），每 25 分钟自动换新，不会再过期** |
| 提示 `RotateCookies 401` | 会话彻底失效（超过续期窗口）：浏览器重新登录 gemini.google.com，重新导出 cookie，并触发一次服务重载 |
| 提示 `portal/app-bound cookie encryption` | 新版浏览器无法离线解密：用 Cookie-Editor 导出 + `--cookie-file` |
| 找不到 Cookie 数据库 | 未在该浏览器登录 Google，或浏览器未关闭 |
| 错误码 `1037` | 用量/频率超限，稍等再试 |
| 错误码 `1060` | IP 被 Google 临时限制，换网络或等待 |
| 生图返回"额度重置后可创建更多图片" | 账号的 Imagen 生成额度用尽（设置页可查），等重置或换账号 |
| 生视频返回"正在生成视频"后工具超时 | 渲染需要 5~15 分钟；调大 `timeout_seconds`，或稍后用 `gemini_read_conversation` 查看 |
| 新对话不出现在网页端 | 网页端刷新即可；与网页端共享同一账号 |
| DSH 里看不到 `mcp__gemini-web__*` | 新开一个会话；确认 `cordis.patch.yml` 语法正确（YAML 数组） |

---

## 开发与测试

```bash
uv run pytest                 # 38 个测试：AES-GCM 解密、两阶段上传、分帧解析（含 UTF-16）、
                              # StreamGenerate 请求构造、batchexecute RPC、媒体解析、下载
uv run python -m gemini_mcp.cookie_extractor --help
```

测试全部使用合成加密数据，不需要真实浏览器或真实 cookie。

---

## 安全与免责声明

- Cookie 仅在**本机内存**中处理，程序不会上传到任何第三方；但解密后的 cookie 值等同
  于账号凭证（`--reveal` 输出请妥善保管，勿提交进 git / 勿外传）
- 本项目与 Google 无任何关联，基于网页端内部接口的逆向封装，接口随时可能变动
- 仅限个人学习与自动化使用，滥用可能导致账号受限；风险自负
