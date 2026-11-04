# 给另一台计算机上的 Agent 用：安装与接线

本仓库把 `gemini.google.com` **网页端**封装成 MCP 服务（不使用官方 API、不需要 Google API Key），
调用的是网页端内部接口，因此需要一份**已登录的会话 Cookie**。

- 源码：https://github.com/orphiczhou/Gemini-Eyes
- 配套闭环脚手架：https://github.com/orphiczhou/gemini-loop
- 上游（原作者）：https://github.com/ConsoleSun/Gemini-Eyes

---

## 1. 前置条件

| 项 | 要求 |
|---|---|
| Python | ≥ 3.10 |
| uv | `pip install uv` 或见 https://docs.astral.sh/uv/ |
| 网络 | 访问 `gemini.google.com`。部分地区需代理：`HTTPS_PROXY=http://<host>:<port>` |
| 账号 | 一个能登录 Gemini 网页端的 Google 账号 |

## 2. 安装

免安装直接跑（推荐先这样验证）：

```
uvx --from git+https://github.com/orphiczhou/Gemini-Eyes.git gemini-mcp
```

或者克隆后本地跑：

```
git clone https://github.com/orphiczhou/Gemini-Eyes.git
cd Gemini-Eyes
uv sync
uv run gemini-mcp
```

## 3. 准备 Cookie（唯一需要手工的一步）

服务读取 `~/.config/gemini-web-mcp/cookies.json`（也可用 `GEMINI_COOKIE_FILE` 指定），
内容形如 `{ "__Secure-1PSID": "...", "SID": "...", "SIDCC": "...", ... }`。

关键必须有：`SID`、`HSID`、`SSID`、`APISID`、`SAPISID`、`__Secure-1PSID`、`__Secure-1PSIDTS`。

两种获取方式：

**A. 手工导出（最通用）**
1. 在该机器的浏览器里登录 `https://gemini.google.com`
2. 装 Cookie-Editor 一类的扩展，导出 **google.com** 的 Cookie 为 JSON
3. 另存为 `~/.config/gemini-web-mcp/cookies.json`

**B. 专用浏览器 profile + CDP 导出（本仓自带，Windows）**

```
powershell -File tools/mcp-browser-profile.ps1 -Dump
```
它会用独立 profile 打开 Gemini（首次需在该窗口登录一次），再通过 DevTools 协议导出 Cookie 写盘。
浏览器端加密（v20）下这是唯一可行的自动方式。

> **注意**：Chromium 的 Cookie 库在 Windows 上被应用绑定密钥加密，**不能**离线解密；
> 直接拷贝 sqlite 文件在别的进程里读会得到乱码——必须让浏览器自己导。

## 4. 模型档位（可选）

网页端有四档，本服务默认用最高档：

| 环境变量 | 取值 | 说明 |
|---|---|---|
| `GEMINI_MODEL` | `argon`（默认）/ `pro` / `flash` / `flash-lite` / `legacy` | `argon` = Gemini 4，最强 |
| `GEMINI_THINKING` | `standard` / `extended`（默认）/ `deep` | `deep` 是异步的，同步接口拿不到结果 |
| `GEMINI_MODEL_FALLBACK` | 默认 `pro,flash-basic` | 高档不可用时按序降级 |

`GEMINI_MODEL=legacy` 可以完全回到「无模型头」的旧行为。

## 5. 接到 MCP 客户端

### Claude 风格（Proma / Claude Code / 多数 Agent）
```json
{
  "mcpServers": {
    "gemini-web": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/orphiczhou/Gemini-Eyes.git", "gemini-mcp"],
      "env": {
        "GEMINI_COOKIE_FILE": "<home>/.config/gemini-web-mcp/cookies.json",
        "HTTPS_PROXY": "http://<proxy-host>:<proxy-port>"
      }
    }
  }
}
```

### ZCode（`~/.zcode/cli/config.json`）
```json
{
  "mcp": {
    "servers": {
      "gemini-web": {
        "type": "stdio",
        "command": "uvx",
        "args": ["--from", "git+https://github.com/orphiczhou/Gemini-Eyes.git", "gemini-mcp"],
        "env": { "GEMINI_COOKIE_FILE": "<home>/.config/gemini-web-mcp/cookies.json" },
        "enabled": true
      }
    }
  }
}
```

### VS Code（`User/mcp.json`）
```json
{
  "servers": {
    "gemini-web": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/orphiczhou/Gemini-Eyes.git", "gemini-mcp"]
    }
  }
}
```

### Cherry Studio
设置 → MCP 服务器 → 添加：命令 `uvx`，参数同上，环境变量填 Cookie 路径与代理。

## 6. 验证

工具列表里应出现 10 个 `gemini-web` 工具：

```
gemini_status            gemini_send_message      gemini_analyze_media
gemini_generate_image    gemini_generate_video    gemini_download_media
gemini_list_conversations gemini_read_conversation gemini_delete_conversation
gemini_review
```

依次跑：

1. `gemini_status` → 期望 `ok: true`、`token_fetched: true`、`model.model = "argon"`
2. `gemini_send_message("只回复两个字：可用")` → 期望返回文本，且带 `"model": "4 Argon High"`
3. `gemini_review(paths=["somefile.py"], question="这段代码有什么问题？")` → 上传本地文件让它评审

## 7. 已知边界（重要，别踩）

- **一个会话只能有一个客户端在轮换**。同一份 Cookie 被两个客户端（例如浏览器 + 本服务）同时使用时，
  谁刷新 `__Secure-1PSIDTS` 谁就作废对方的，表现为「突然要重新登录」。要并行用，请用独立浏览器 profile。
- **`repo` 导入的仓库上下文无法通过 MCP 续聊**。在网页端「任务模式 → + → 更多上传 → 导入代码」
  导入的仓库，模型能读（任务模式有解压工具），但用 `conversation_id` 经 MCP 续问时，附件上下文不在。
  需要整库分析就走网页任务模式；需要 MCP 侧分析就用 `gemini_review` 传具体文件。
- 网页端导入仓库有硬上限：**单仓库约 5000 文件 / 100 MB**。
- 私有仓库导入需要在该网页端账号下连接过 GitHub。

## 8. 安全

- `cookies.json` 等同于账号登录态，**不要提交进任何仓库**（本仓 `.gitignore` 已覆盖）。
- 建议用一个**专用浏览器 profile / 小号**登录，避免主力账号被风控。
- 本仓不使用、不保存任何 Google API Key。

---

## 相关仓库

| 仓库 | 用途 |
|---|---|
| `orphiczhou/Gemini-Eyes` | 本仓：Gemini 网页端 → MCP |
| `orphiczhou/gemini-loop` | 把「Gemini 当架构师、本地 Agent 当施工队」的改进闭环脚手架 |