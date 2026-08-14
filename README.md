# gemini-web-mcp

一个 **MCP（Model Context Protocol）服务**，让任何 Agent（Claude、Cline、各类 MCP 客户端）直接和 **Gemini 网页端**（gemini.google.com）对话。

- ✅ **不走官方 API**：不需要 API Key、不产生 API 计费
- ✅ **"反编译 cookie" 鉴权**：自动提取并解密本地 Chrome / Edge / Chromium 浏览器里已登录的 Google 会话 Cookie（Windows DPAPI / macOS 钥匙串 / Linux "peanuts" 密钥），原样重放浏览器请求
- ✅ 完全模拟网页端行为：`StreamGenerate` 流式接口 + `batchexecute` RPC（列会话 / 读历史 / 删会话）
- ✅ 与网页端共享历史：新开的对话会出现在 gemini.google.com 的侧边栏里

> ⚠️ 注意：本项目属于对 Gemini 网页端内部接口的逆向封装，非官方 API。仅用于个人学习与自动化。**请勿滥用**，行为受 Google 服务条款约束，账号风险自负。

---

## 工作原理

```
┌────────────┐   MCP(stdin/HTTP)   ┌─────────────────┐
│  Agent     │ ◄──────────────────► │  gemini-web-mcp  │
└────────────┘                     └────────┬────────┘
                                            │ requests（带上解密的 Cookie）
                                            ▼
                                 ┌──────────────────────────┐
                                 │ gemini.google.com 网页端   │
                                 │  StreamGenerate /         │
                                 │  batchexecute（内网 RPC）  │
                                 └──────────────────────────┘
```

1. **反编译 Cookie**（`gemini_mcp/cookie_extractor.py`）
   - 从浏览器配置目录找到 Cookie 数据库（SQLite）和 `Local State` 加密密钥文件
   - 按平台解密：
     - **Windows**：`DPAPI`（`CryptUnprotectData`）解出 AES-256-GCM 主密钥
     - **macOS**：`security` 命令读钥匙串 "Chrome Safe Storage"，PBKDF2 派生 AES-128 密钥
     - **Linux**：Chromium 硬编码口令 `"peanuts"` 解出 AES-256-GCM 主密钥
   - 逐条解密 `v10` 前缀的 AES-GCM Cookie；旧版明文/base64 直接读
   - 先把数据库复制到临时文件再读，避免浏览器运行时的 SQLite 锁
2. **换令牌**（`gemini_mcp/gemini_client.py`）
   - 访问 `https://gemini.google.com/app`，从页面提取 `SNlM0e`（CSRF 令牌）、`cfb2h`（build label）、`FdrFJe`（session id）
3. **对话**：POST `/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate`，流式解析长度前缀分帧响应，累积文本、思考内容、会话/回复/候选 ID

---

## 安装

需要 Python ≥ 3.10（推荐用 [uv](https://docs.astral.sh/uv/)）：

```bash
cd gemini-web-mcp
uv sync --extra dev     # 安装依赖（mcp / requests / pycryptodome）
```

## 使用前的准备（一次性）

1. 在 **Chrome / Edge / Chromium** 里登录 `https://gemini.google.com`（能看到 Gemini 界面即可）
2. **关闭浏览器**（否则 Cookie 数据库被锁，虽然程序会自动复制一份，但保险起见先关）
3. 可选：指定非默认配置目录
   - Linux: `~/.config/google-chrome`（Edge: `~/.config/microsoft-edge`）
   - Windows: `%LOCALAPPDATA%\Google\Chrome\User Data`
   - macOS: `~/Library/Application Support/Google/Chrome`

先用调试 CLI 验证 Cookie 能被正确"反编译"：

```bash
# 列出找到的 google.com cookie 名（值已打码）
uv run python -m gemini_mcp.cookie_extractor --browser chrome --list --domain google.com

# 查看解密后的完整值（敏感！仅调试用）
uv run python -m gemini_mcp.cookie_extractor --browser chrome --reveal --domain google.com
```

## 启动 MCP 服务

```bash
# 标准输出传输（大多数 MCP 客户端用这个）
uv run gemini-mcp --browser chrome

# Edge / 指定配置文件
uv run gemini-mcp --browser edge --profile "Profile 1"

# HTTP 传输（SSE/streamable-http，供远程 Agent 或调试）
uv run gemini-mcp --transport http --port 8900

# 手动导出的 cookie JSON（[{name,value,...}] 或 {name:value}）
uv run gemini-mcp --cookie-file cookies.json
```

也可以设置环境变量 `GEMINI_COOKIE_FILE=/path/to/cookies.json` 代替 `--cookie-file`。

### Claude Desktop 配置示例

`claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "gemini-web": {
      "command": "/home/you/.local/bin/uv",
      "args": ["run", "--directory", "/home/you/Gemini Eyes/gemini-web-mcp", "gemini-mcp", "--browser", "chrome"]
    }
  }
}
```

### Cline / Roo Code / 其他支持 MCP 的 IDE

同样的 `command` + `args`，或 stdio 方式接入；HTTP 模式则填 `http://127.0.0.1:8900/mcp`。

---

## 工具清单

### 对话

| 工具 | 说明 |
|---|---|
| `gemini_send_message(message, conversation_id?, response_id?, language?, file_paths?)` | 发消息（可附图片/视频文件）；带 `conversation_id` 则继续已有对话 |
| `gemini_list_conversations(limit=13)` | 列出账号最近的对话（含置顶标记和时间戳） |
| `gemini_read_conversation(conversation_id, limit=10)` | 读取某对话的完整轮次（新→旧，含引用的媒体） |
| `gemini_delete_conversation(conversation_id)` | 删除对话（不可恢复） |

### 视觉（Gemini 当"眼睛"）

| 工具 | 说明 |
|---|---|
| `gemini_analyze_media(file_path, prompt?)` | 上传一张图片/视频让 Gemini 识别，返回描述文本。**Agent 收到用户上传的图片/视频时应优先调用它** |
| `gemini_download_media(url, save_path)` | 用登录会话下载媒体 URL 到本地（某些 googleusercontent 链接需要 cookie） |

### 生成（Gemini 当"手"）

| 工具 | 说明 |
|---|---|
| `gemini_generate_image(prompt, reference_image?, save_dir?)` | 文生图（Imagen）；传 `reference_image` 可图生图。自动把生成图下载到本地并返回路径 |
| `gemini_generate_video(prompt, save_dir?, timeout_seconds?)` | 文生视频（Veo）。生成是异步的，工具会轮询直到渲染完成，下载到本地并返回路径 |

### 其他

| 工具 | 说明 |
|---|---|
| `gemini_status()` | 诊断：Cookie 数量、关键 Cookie 是否齐全、令牌能否换到 |

多轮对话示例（Agent 视角）：

```
1. gemini_send_message("帮我写一个快速排序") 
   → {text: "...", conversation_id: "c_abc", response_id: "r_1", ...}
2. gemini_send_message("改用 C++ 实现", conversation_id: "c_abc", response_id: "r_1")
   → 同一对话内的下一轮
```

"眼睛 + 手"工作流示例（DSH 中）：

```
用户上传 photo.jpg
→ gemini_analyze_media(file_path="/.../photo.jpg", prompt="描述这张图片")
→ 把 Gemini 的描述转述给用户

用户说"画一只赛博朋克风的猫"
→ gemini_generate_image(prompt="赛博朋克风的猫")
→ 返回 {local_path: "./media/generated_image_xxx.png"}，把本地路径展示给用户
```

> ⚠️ 生成额度：图片生成（Imagen）和视频生成（Veo）消耗账号的
> **网页端生成额度**（免费账号有限额，设置页可查看）。额度用尽时 Gemini
> 会在回复文本中说明。视频渲染通常需要 5~15 分钟，`gemini_generate_video`
> 会轮询等待并自动下载成品。

---

## 接入 DSH（DeepSeek Harness）

DSH 原生支持 MCP 客户端插件（`@deepseek-ai/dsh-mcp-client`）。项目里已附 `gemini-web.cordis.yml` 示例：

```sh
dsh web --patch "/home/ubuntuhong/Gemini Eyes/gemini-web-mcp/gemini-web.cordis.yml"
```

按需修改该文件里的 `command`、`args`、`env.GEMINI_COOKIE_FILE` 路径。启动后工具会以
`mcp__gemini-web__gemini_analyze_media` 等形式暴露给 Agent。

> 注意：DSH 的 stdio 桥接器会主动移除名称疑似凭据的环境变量；cookie 通过
> `GEMINI_COOKIE_FILE`（指向磁盘文件）传递不受影响。

---

## 开发

```bash
uv run pytest          # 跑单元测试（合成加密数据，无需真实浏览器）
uv run python -m gemini_mcp.cookie_extractor --help
```

测试覆盖：AES-GCM v10 解密、Linux "peanuts" Local State、分帧流解析（含 UTF-16 长度）、
StreamGenerate 请求构造、batchexecute 会话 RPC。

### 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `SNlM0e` 解析失败 / HTTP 400 | Cookie 过期：浏览器重新登录 gemini.google.com，重启服务 |
| 找不到 Cookie 数据库 | 未在该浏览器登录 Google，或浏览器正开着导致复制失败 |
| 提示 `portal/app-bound cookie encryption` | 2025 年后的新版 Chrome/Edge 改用系统 portal 加密，**无法离线解密**。用浏览器扩展（如 Cookie-Editor、EditThisCookie）把 `gemini.google.com` 的 cookie 导出为 JSON，然后 `uv run gemini-mcp --cookie-file cookies.json`（或设环境变量 `GEMINI_COOKIE_FILE`） |
| 1037 错误码 | 用量/频率超限，稍等再试 |
| 1060 错误码 | IP 被 Google 临时限制，换网络或等一会 |
| 新对话不在网页端出现 | 网页端需要刷新；本服务与网页端共享同一账号历史 |

### 手动导出 Cookie（新版浏览器必读）

新版 Chrome/Edge 的 cookie 加密无法自动解密时：

1. 浏览器打开 `https://gemini.google.com` 并保持登录
2. 用扩展 **Cookie-Editor**（或同类工具）导出全部 cookie 为 JSON
3. 保存为 `cookies.json`，格式二选一：

```json
[
  {"name": "__Secure-1PSID", "value": "xxxx", "domain": ".google.com", "path": "/"},
  {"name": "SID", "value": "xxxx", "domain": ".google.com", "path": "/"}
]
```

或简化为 `{"__Secure-1PSID": "xxxx", "SID": "xxxx"}`。

4. 启动：`uv run gemini-mcp --cookie-file cookies.json`

**需要哪些 cookie？**

- **必带**：`__Secure-1PSID`（账号主会话）；若浏览器里有 `__Secure-1PSIDTS` 则必须一起带（缺它会 401）
- **推荐全带**：`SID`、`HSID`、`SSID`、`APISID`、`SAPISID`、`__Secure-1PAPISID`、`__Secure-3PSID`、`__Secure-3PSIDTS`、`__Secure-3PAPISID`、`NID`、`AEC`、`SIDCC`、`__Secure-1PSIDCC`、`1P_JAR`
- 最简单的做法：**全部导出，一个不落**（20~40 个都没关系）

启动后用 `gemini_status` 工具自查：它会列出已有哪些、缺哪些关键 cookie。

> ⚠️ `__Secure-1PSIDTS` 过期很快（几小时~几天），失效后重新导出即可。

## 免责声明

本项目与 Google 无任何关联。Cookie 仅保存在本机内存中，不会上传；但请妥善保管解密后的 Cookie 值（`--reveal` 输出），泄露等于账号泄露。
