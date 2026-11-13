# gemini-web MCP → 标准 API 服务可行性研究

> 研究任务书：`docs/briefs/gemini-api-feasibility-brief.md`（lead 派发，2026-11-13）
> 性质：纯研究，不改任何代码。所有结论带证据（文件路径+行号 / 实测输出），详见文末「证据索引表」。
> 安全声明：本研究未读取、未复制、未输出任何 Cookie 内容；未对 gemini.google.com 发起任何真实调用（全部结论来自读码 + 本地合成测试与本地内省）；本文不含任何凭证片段、token 值或账号标识。

---

## 一页结论

**能转，而且已经转了一半。** 现有 server 自带的 `--transport http`（streamable HTTP MCP，127.0.0.1 绑定 + Bearer 常量时间鉴权，`server.py:96-102/694-712`）**就是一个对本机 Agent 可用的标准 API 服务**——DSH 的 dsh-mcp-client 已原生支持 `streamable-http` 传输 + 自定义 headers + `${credential:}` 凭据模板（asar 内 `lib/index.js:121/309-318` 实证），**零代码即可接线**。

**主推架构（双层）**：
1. **第 0 层（今天就能用，0 行代码）**：现有 HTTP MCP 服务 + 固定 `GEMINI_HTTP_TOKEN` + DSH streamable-http 接线 → DSH 全部子会话获得 10 个 `gemini_*` 工具。
2. **第 1 层（MVP 约 0.5~1 天，约 400 行）**：新增 `gemini_mcp/api.py` OpenAI 兼容薄层（`POST /v1/chat/completions` + `GET /v1/models`），复用现有 `GeminiWebClient`，服务 Cherry Studio / OpenAI SDK 等非 MCP 客户端。SSE 流式、每请求选模型放二期。

**关键判断**：b) OpenAI 兼容层值得做但只是「翻译层」——全部难点（会话态、错误码、每请求模型、上传）在 `gemini_client.py` 都有现成的可复用挂点（`resolve_model_setting` 纯函数、`GeminiWebError.code`、`upload_file` 两阶段封装）；c) 简洁自研 REST 生态价值低于 a+b 组合，不单独做；d) DSH 消费已核实可行，是第 0 层本身。

**最大约束不是技术是治理**：同一份 Cookie 全机只允许一个客户端轮换 `__Secure-1PSIDTS`（`INSTALL-FOR-AGENTS.md` §7）→ 整机只跑**一个**服务进程，所有 API 客户端共享它；服务端需加串行锁（并发安全缺口见 review-20261102 P1-1）。

---

## 问题 1：现状盘点（对外接口形态 / 鉴权 / 并发 / 超时 / 长任务）

### 1.1 对外接口形态：两种

| 形态 | 入口 | 证据 |
|---|---|---|
| **stdio MCP**（默认） | `gemini-mcp` → `mcp.run_stdio_async()` | `server.py:663-664`（`--transport` 参数）、`server.py:713-716` |
| **streamable HTTP MCP** | `--transport http --port 8900` | `server.py:665-666`、`server.py:694-712`、`_run_http_async` `server.py:96-102`（uvicorn + `mcp.streamable_http_app`） |

工具注册共 **10 个**（本报告本地内省实测，非联网）：
`gemini_send_message / gemini_analyze_media / gemini_generate_image / gemini_generate_video / gemini_review / gemini_list_conversations / gemini_read_conversation / gemini_delete_conversation / gemini_download_media / gemini_status`。
> ⚠️ 一致性核对：`README.md:109` 写「工具一览（9 个）」且表中漏列 `gemini_review`，而 `INSTALL-FOR-AGENTS.md:128-135` 与实测均为 10 个——README 文档滞后，属仓库文档缺陷（本研究只记录不修改）。

### 1.2 鉴权（HTTP 模式）

- `_TokenAuthMiddleware` 对所有 http scope 强制 `Authorization: Bearer <token>`，`hmac.compare_digest` 常量时间比较，失败返回 401 + `WWW-Authenticate`（`server.py:57-93`）。
- token 来源优先级：`--http-token` > 环境变量 `GEMINI_HTTP_TOKEN` > 启动时随机生成并**明文打印到 stdout**（`server.py:667-670, 698-711`）。
- 默认绑定 `127.0.0.1`（`server.py:712`），不暴露局域网；`--http-allow-anonymous` 可显式关鉴权并打警告（`server.py:671-674, 699-704`）。
- **缺口**：随机打印模式不利于无人值守服务（stdout 抓取不可靠、重启换 token）——固定 env 注入即可解，属配置面非代码缺口。

### 1.3 并发模型

- 全局单例 `GeminiWebClient`（`server.py:46, 144-154`）+ 会话元数据缓存 `_metadata_cache`（无上限 dict，`server.py:51`）。
- 后台 daemon 线程每 25 分钟轮换短效 Cookie 并写回文件（`server.py:53-54, 190-199, 692`）。
- 工具函数是**同步 def**（如 `server.py:305-349`），stdio 模式下串行天然安全；HTTP 模式下并发请求 + 轮换线程共享同一 `requests.Session`，**无锁**——review-20261102 P1-1 已定级 P0（`docs/review-20261102.md:202-214`）：「`--transport http` 或未来并发调用会放大」。
- **对 API 化的直接含义**：任何 HTTP/REST 层都必须先把 generate 调用**串行化**（进程内锁或单 worker），这与「单 Cookie 单写者」约束（§7 边界）方向一致——本来也不该并发打网页端。

### 1.4 超时与韧性

- `timeout=180s` 是**单次 socket 读间隔**而非总预算（`gemini_client.py:464`，传给 `session.post(stream=True)` 于 `gemini_client.py:814`）。
- 两层有界恢复：会话自愈（HTTP 400/401/403 → 轮换+重抓令牌，`gemini_client.py:175, 649-665`）+ 瞬态重试（1013/5xx/传输错误，2 次退避 0.8s/2.0s，`gemini_client.py:177-180`）。
- 模型降级链：argon → pro → flash-basic → legacy 无头请求（`gemini_client.py:124-129, 496-508`；1037/1052 触发降档 `:154`，1050 跳 legacy `:157`）。
- **已知缺口（review P0-6，`docs/review-20261102.md:190-198`）**：无总预算上限，最坏组合可阻塞接近 1 小时，远超 DSH 侧 `toolCallTimeoutMs=600000`（`README.md:80`）。API 化后对外层（HTTP 服务）必须自己设 deadline。

### 1.5 长任务（生视频）

- `gemini_generate_video` 是**同步长轮询**：发起请求后每 15s 调 `read_conversation` 探测成品，默认 `timeout_seconds=600`；超时未成返回 `generated_video: null` + note 提示稍后用 `gemini_read_conversation` 查看（`server.py:500-549`，轮询体 `:529-543`，超时返回 `:544-547`）。
- 语义上这是「同步尽力等、失败不丢任务」——REST 化时既可原样保留（长请求），也可拆成「提交 + 轮询读」两段（见问题 3 第 6 项裁决）。

---

## 问题 2：四条候选路线对比

### 2.a MCP streamable HTTP（现状已有）——完整性与缺口

**完整性（已验证）**：Bearer 中间件覆盖全部 http 请求（含 SSE 流）、127.0.0.1 绑定、10 工具注册齐全（§1 实测）、后台 Cookie 保活与持久化闭环（`server.py:157-199`）。

**缺口**：
1. token 交付方式（stdout 打印）不适合服务化 → 用 `GEMINI_HTTP_TOKEN` 固定即可，无需改码；
2. 并发安全未加固（P1-1，§1.3）→ HTTP 模式下建议限单并发或先修 P1-1；
3. 无总超时预算（P0-6）→ 外层 HTTP 服务必须设 deadline；
4. 每请求模型/思考档不可选（问题 3 第 4 项）。

**生态适配**：DSH ✅（原生，见 2.d）；支持 MCP 的客户端（Claude Desktop/Cline 等）✅；Cherry Studio/OpenAI SDK ❌（不识别 MCP 协议）。

### 2.b OpenAI 兼容 `/v1/chat/completions`——可行，翻译层定位

- **适配面最大**：OpenAI SDK、Cherry Studio、各类 Agent 框架通吃；`gemini-` 前缀模型名天然兼容（见问题 3 第 4 项）。
- **流式 SSE 可行**：`generate()` 支持逐帧回调 `on_frame`（`gemini_client.py:466, 724`），且候选文本是**累积快照**而非增量（`_consume_frame` 每帧整体覆盖 `result.text`，`gemini_client.py:941-943`）→ SSE 层做「新帧 vs 旧快照」diff 出 delta 即可，无需改 FrameParser。
- **成本**：会话态、system prompt、错误码、媒体四类映射（问题 3 逐项裁决，全部「能」但各有妥协）。
- **长任务语义**：OpenAI 协议无异步任务概念 → 生视频走「长请求同步等」或扩展字段返回 conversation_id 让客户端自轮询。
- **结论**：值得做，但作为第 1 层薄适配（一个新文件），不动 client。

### 2.c 简洁自研 REST——可行但不单独做

按 10 工具映射 `/tools/{name}` 语义最直白，改造成本与 b 相当（同样要处理会话态/上传/长任务），但生态适配面（谁都直接吃 `POST /tools/gemini_send_message` 这种私有协议？）远小于 a+b 组合。a 本质上就是「REST over JSON-RPC」，c 与 a 信息重复。**裁决：不单独做；b 的端点设计里已经覆盖了 c 的诉求**（若确有非 OpenAI 非 MCP 的裸 HTTP 客户端需求，可在 api.py 加 2 个直通端点，成本 +30 行）。

### 2.d 挂进 DSH（HTTP MCP 被 dsh-mcp-client 消费）——已核实可行（asar 读证）

| 事实 | 证据（`D:/DSHarness/resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-mcp-client/`） |
|---|---|
| 客户端依赖官方 MCP SDK 并引入 StreamableHTTP 传输 | `package.json:53`（`@modelcontextprotocol/client 2.2.0`）；`lib/index.js:4`（`import { Client, StreamableHTTPClientTransport, ... }`） |
| transport 按 config 分派，`streamable-http` 分支存在且带自定义 headers | `lib/index.js:113-123`（`createTransport`），`:121`：`case "streamable-http": return new StreamableHTTPClientTransport(new URL(config.url), { requestInit: { headers: config.headers } })` |
| headers/url 支持 `${credential:REF}` 凭据模板（运行时经 credentials provider 解析并脱敏） | `lib/index.js:250-294`（模板语法/解析），`:309-318`（streamable-http 配置的 `config.url` 与全部 `config.headers` 值参与模板解析 + secret 脱敏） |
| stdio 分支 | `lib/index.js:115-120` |

**结论**：DSH 侧接线是**纯配置**——新增一个 `transport: "streamable-http"` 的 MCP server 配置，`url=http://127.0.0.1:8900/mcp`，`headers: {"Authorization": "Bearer ${credential:GEMINI_WEB_MCP_TOKEN}"}`，凭据走 DSH credential seam，不落明文。未核实项：无（上述四点均读到运行时 JS 原文；`.d.ts` 被剔除不影响本次证据面）。

### 对比总表

| 维度 | a) HTTP MCP（现状） | b) OpenAI 兼容层 | c) 自研 REST | d) DSH streamable-http 接线 |
|---|---|---|---|---|
| DSH 子会话 | ✅（stdio 现用） | ➖需再桥一层 | ➖ | ✅ **零代码** |
| Cherry Studio / OpenAI SDK | ❌ | ✅ 主目标 | ❌ | ❌ |
| 流式体验 | ✅ MCP sampling/SSE | ✅ SSE（快照 diff，二期） | 需自定 | ✅ |
| 长任务语义 | 工具内同步轮询（现装） | 需扩展约定 | 需自定 | 同 a |
| 鉴权改造 | 无（已有 Bearer） | 复用同一 Bearer | 复用 | `${credential:}` 模板 |
| 单 Cookie 单写者影响 | 全客户端共享 1 进程 ✅ | 同左（同进程） | 同左 | 同左（1 进程） |
| 改造成本 | 0 行（配置即可） | ~400 行新文件 | ~400 行（价值低） | 0 行（配置） |

---

## 问题 3：映射难点逐个裁决

### 3.1 多轮会话：conversation_id ↔ 无状态协议 —— **能**

- 服务端已把 `(conversation_id, response_id)` 做成显式续聊参数（`server.py:306-312`），`_metadata_for` 从缓存重建完整 metadata（`server.py:202-209`），`_remember` 在每轮后回填缓存（`server.py:212-214`）。
- **方案**：OpenAI 层用 `extra_body` 透传 `gemini_conversation_id` / `gemini_response_id`（OpenAI SDK 原生支持 extra_body），响应中以扩展字段/响应头回传这两个 id。备选：服务端 session 表（按 client+model 键控）——不推荐，无状态透传更简单且与 MCP 工具语义一致。

### 3.2 system prompt 注入位置 —— **能（妥协方案）**

- 网页端**没有独立 system 槽**：`gemini_send_message` 签名只有 message/conversation_id/response_id/language/file_paths（`server.py:306-312`）；StreamGenerate 请求体由 `_build_inner(prompt, metadata, file_data, plan, session_uuid)` 构建，无 system 参数（`gemini_client.py:767-795`）。
- **裁决**：新会话首条消息前拼接 `<system instructions>…</system>\n\n` 前缀注入；续聊轮不重复注入（历史已在服务端会话里）。局限：非强 system 语义、可能被模型当普通文本——如实告知用户。

### 3.3 错误码翻译 —— **能（全部五码有出处）**

`ERROR_CODES`（`gemini_client.py:299-305`）+ `GeminiWebError.code/.transport`（`:308-330`）是现成的翻译底座：

| 服务端码 | 语义（源码注释） | 建议 HTTP | 错误体 error code |
|---|---|---|---|
| 1013 | temporary server error | 503 + Retry-After | `upstream_transient` |
| 1037 | usage limit exceeded | 429 | `upstream_rate_limited` |
| 1050 | model inconsistent with conversation history | 409 | `conversation_model_conflict` |
| 1052 | model header invalid/unavailable | 400（入参 model 非法） | `model_unavailable` |
| 1060 | IP temporarily blocked | 429（或 503） | `ip_blocked` |
| transport=True | 超时/断流/重置 | 502/504 | `upstream_transport` |
| SNlM0e 解析失败 | Cookie 失效（`gemini_client.py:566-570`） | 503 + 提示 `gemini_status` | `auth_session_expired` |

**红线沿用 review P1-5**（`docs/review-20261102.md:260-271`）：错误体**不得透传**上游 `resp.text` 片段（`:1064/:1070` 现状会把上游文本带进异常消息）——API 层只回错误码+人话，不回原文。

### 3.4 每请求选模型/思考档 —— **能（小改，~30-50 行，行号级出处）**

- **现状**：模型链绑定在 client 实例——构造时 env 兜底（`gemini_client.py:481-488`：显式参数优先于 `GEMINI_MODEL/GEMINI_THINKING/GEMINI_MODEL_CAPACITY/GEMINI_MODEL_FALLBACK`），解析为 `self.model_setting` 与 `self.model_chain`（`:496-508`）；`generate()` 遍历 `self.model_chain` 做降级（`:742-765`）。而 `server.py` 构造 client 时只传 cookies/language（`server.py:153`）→ 完全 env 驱动，故 README 说「模型档位靠环境变量」。
- **改法（最小侵入）**：`generate()` 增加 `model: str | None, thinking: int|str | None` 形参；入口处若非 None，用**现成纯函数** `resolve_model_setting(model, thinking, capacity)`（`gemini_client.py:239-278`）构造 per-request plans 替代 `self.model_chain`，保留 1050→legacy 跳转逻辑（`:752-759`）与 MODEL_RETRY_CODES 降档（`:760-761`）不动。OpenAI 层 model 名直接复用 `_normalise_model_name`（`:208-212`，**已内置 `gemini-` 前缀剥离**）→ 客户端写 `"model": "gemini-pro"` 即映射到 pro 档，别名表 `MODEL_ALIASES`（`:101-120`）同步生效。
- 服务端 MCP 工具侧同理给 `gemini_send_message` 加可选参数（二期）。

### 3.5 文件上传：resumable 两阶段 → multipart —— **能（协议不用动）**

- `upload_file` 已把两阶段封装成「本地路径进、StreamGenerate 文件条目出」（`gemini_client.py:1009-1023`）：Phase 1 `x-goog-upload-command: start` 取 resumable URL（`:1045-1067`），Phase 2 `upload, finalize` 传字节（`:1073-1094`）。
- **API 层方案**：multipart 收文件 → 落临时目录 → 调 `upload_file(path)`。OpenAI 协议的 `image_url`（data URI）解码后同样走这条路。无需触碰 resumable 协议。
- 顺手项：响应里的 `local_path` 沿用 review P2-5 建议做脱敏/相对化（`docs/review-20261102.md:326-331`），API 层返回 URL 而非宿主机绝对路径。

### 3.6 生图/生视频同步/异步语义 —— **能（语义要显式选）**

- **生图**：现状同步（generate + 立即下载到 save_dir，`server.py:470-497`、`_with_saved_media:575-596`）→ OpenAI 层建议映射为 assistant 消息携带 `image_url`（指向 API 层自己的媒体代理端点，因为 googleusercontent URL 部分需要登录态才能取——这正是 `gemini_download_media` 存在的原因，`server.py:552-572`）。
- **生视频**：现状同步长轮询 600s 上限（§1.5）。REST/OpenAI 层裁决：**默认同步长请求**（与现行为一致、实现最薄）；`wait=false` 扩展参数时立即返回 202 + `gemini_conversation_id`，客户端后续轮询（复用 read 端点）。deep-think 档（thinking=3）天然异步首帧占位（`gemini_client.py:131-135` 注释），API 层直接沿用「返回占位+conversation_id」语义即可。

---

## 问题 4：推荐方案

### 4.1 主推：双层架构（a+d 为第 0 层，b 为第 1 层）

**为什么不主推另外几条**：c 已并入 b（§2.c）；单独 d 不解决非 MCP 客户端；单独 b 反而丢了 DSH 零代码通道。双层让「本机 DSH Agent」今天就可用，「OpenAI 生态客户端」一天后可用。

### 4.2 第 0 层接线（DSH，0 行代码）

1. 服务常驻：`uv run gemini-mcp --transport http --port 8900`（cookie 走默认路径）；
2. 固定 token：启动前设 `GEMINI_HTTP_TOKEN`（`server.py:698` 已支持，不再 stdout 打印）；
3. DSH profile 的 MCP 配置加一条 streamable-http server：`url: http://127.0.0.1:8900/mcp`，`headers: {Authorization: "Bearer ${credential:<登记的凭据名>}"}`（模板与脱敏见 §2.d 证据）；
4. DSH 工具超时维持 bundle 现值 600s（`README.md:80`）。

### 4.3 第 1 层：OpenAI 兼容端点清单（MVP）

| 端点 | 方法 | 映射 |
|---|---|---|
| `/v1/chat/completions` | POST | 非 `stream` → `client.generate()`；`stream=true` → 二期 SSE |
| `/v1/models` | GET | 由 `MODEL_CATALOG`+`MODEL_ALIASES`（`gemini_client.py:83-120`）生成 id 列表 |
| `/v1/status` | GET | 直通 `gemini_status` 逻辑（脱敏字段白名单） |
| `/v1/conversations` / `/v1/conversations/{id}` | GET/DELETE | 直通 list/read/delete_conversation |
| `/media/{name}` | GET | 登录态媒体下载代理（包装 `download_media`，`gemini_client.py:1097-1135`） |

请求/响应示例（非流式，MVP）：

```json
// POST /v1/chat/completions
{
  "model": "gemini-pro",
  "messages": [{"role": "user", "content": "总结这个目录的改动"}],
  "stream": false,
  "extra_body": {                       // OpenAI SDK 允许 extra_body 透传
    "gemini_conversation_id": "c_xxx",  // 可选：续聊
    "gemini_response_id": "r_xxx"       // 可选：续聊定位
  }
}

// 200 响应（裁剪示意）
{
  "id": "chatcmpl-...", "object": "chat.completion",
  "model": "gemini-pro",
  "choices": [{"index": 0, "finish_reason": "stop",
               "message": {"role": "assistant", "content": "……"}}],
  "gemini": {                            // 扩展字段：续聊游标 + 实际服务档位
    "conversation_id": "c_xxx", "response_id": "r_xxx",
    "served_model": "3.1 Pro"            // 来自响应帧 inner[42]，gemini_client.py:926-928
  }
}
```

SSE 事件序列（二期，示意）：

```
data: {"choices":[{"delta":{"role":"assistant"}}]}        // role 帧
data: {"choices":[{"delta":{"content":"……增量文本……"}}]}   // 每帧快照 diff 出的增量（重复直到 completed）
data: {"choices":[{"delta":{},"finish_reason":"stop"}],
       "gemini":{"conversation_id":"c_xxx","response_id":"r_xxx"}}
data: [DONE]
```

实现挂点：`generate(prompt, metadata, files, on_frame=...)`（`gemini_client.py:719-725`）在流消费过程中逐帧回调 `on_frame`（`_stream_once` 构造 FrameParser 后逐帧 `_consume_frame`，`:819-845`）→ SSE 线程从回调队列取帧 diff。**注意**：回调拿到的是原始帧，文本快照提取逻辑与 `_consume_frame:936-943` 相同（`candidate[1][0]`），复用 `get_nested` 即可。

### 4.4 鉴权与配置面（env 清单）

- 鉴权：第 1 层与第 0 层共用同一 `Authorization: Bearer`（中间件可复用 `_TokenAuthMiddleware`，`server.py:57-93`）；token 唯一来源 `GEMINI_HTTP_TOKEN`。
- env 清单（全部已有，**零新增**即可跑 MVP）：

| env | 作用 | 出处 |
|---|---|---|
| `GEMINI_HTTP_TOKEN` | 两个层的 Bearer token | `server.py:698` |
| `GEMINI_COOKIE_FILE` | Cookie 文件路径 | `server.py:118-121` |
| `GEMINI_MODEL` / `GEMINI_THINKING` / `GEMINI_MODEL_CAPACITY` / `GEMINI_MODEL_FALLBACK` | 默认模型档（每请求参数二期覆盖之） | `gemini_client.py:481-494`，README `:93-98` |
| `HTTPS_PROXY` | 上游代理（本机需要） | INSTALL `:18` |

### 4.5 目录/文件级落点（**本轮不实施**，仅方案）

| 落点 | 动作 | 复用 |
|---|---|---|
| `gemini_mcp/api.py`（新增） | OpenAI 兼容层（建议 Starlette/FastAPI，与 uvicorn 同栈；挂独立端口） | `_TokenAuthMiddleware`、`_get_client`、`_metadata_for/_remember`（`server.py`）、`resolve_model_setting/_normalise_model_name`、`ERROR_CODES`（`gemini_client.py`） |
| `gemini_mcp/server.py`（二期，+参数） | `gemini_send_message` 增 `model/thinking` 可选参透传 generate | §3.4 |
| `tests/test_api.py`（新增） | 合成测试（见 §5.3） | mock `server._get_client` |
| `pyproject.toml`（+1 依赖） | fastapi（或直接用 mcp 已带 starlette 栈） | — |
| `README.md` | 服务化小节（顺带修 §1.1 的「9 个」笔误） | — |

### 4.6 MVP 裁剪边界

**做**：`/v1/chat/completions`（非流式）、`/v1/models`、`/v1/status`、错误码映射表全量、conversation_id 透传、进程内串行锁。
**不做（二期+）**：SSE 流式、每请求模型（OpenAI 层一期先吃 env 默认档）、multipart 上传（一期文本对话先够用）、媒体代理端点、system prompt 自动前缀（一期用户自行写进首条消息）。

---

## 问题 5：工作量与实施计划

### 5.1 步骤与代码量级

| 步骤 | 内容 | 动的文件 | 量级 | 预估 |
|---|---|---|---|---|
| 0 | 第 0 层接线（DSH 配置 + 固定 token + 起常驻进程） | 无代码 | 0 行 | 0.5h |
| 1 | MVP：api.py 四端点 + 错误映射 + 串行锁 + 合成测试 | api.py、test_api.py、pyproject | ~400 + ~250 行 | 0.5~1 天 |
| 2 | SSE 流式 + 每请求模型（api 层 + server 工具参） | api.py、server.py | ~150 行 | 1 天 |
| 3 | multipart 上传 + 媒体代理端点 + system 前缀选项 | api.py | ~150 行 | 0.5~1 天 |
| 4 | （顺带修 review 迭代一止血项中影响 API 的部分：P0-6 总预算、P1-5 错误脱敏） | gemini_client.py/server.py | 见 review | 与上游迭代合并 |

### 5.2 测试策略（全合成，不触网）

- **mock 层**：`monkeypatch` 替换 `server._get_client` 返回桩 client（`generate` 返回固定 `GenerationResult` / 抛带 code 的 `GeminiWebError`）——现成先例：`tests/test_gemini_client.py` 全部 43 例均基于合成帧/合成数据（review `:300`：3 文件 63 用例全合成）。
- **SSE**：httpx `ASGITransport` 流式读取，断言事件序列（role → N×delta → finish+DONE）与最终拼接文本。
- **上传**：构造 multipart 内存文件，断言桩 `upload_file` 收到的路径/次数（不真传）。
- **错误映射**：对 §3.3 表逐码断言 HTTP 状态与 error body。
- **续聊**：两次调用，断言第二次把缓存 metadata 正确带入（`_metadata_for` 行为）。

### 5.3 风险清单

| 风险 | 依据 | 缓解 |
|---|---|---|
| 网页端接口漂移（RPC id、模型头 key、帧槽位） | 常量集中可查：`gemini_client.py:78, 293-296`；槽位协议 `:70-77, 926-948` | 常量单点维护；`gemini_status` 作健康探针；README 已声明随时可能变（`:159-160`） |
| 封号/ToS 风险 | README `:18-22` 明示 | 专用小号（README 既有建议）；API 层不加"多账号池"——review §四明确不做（`:381`） |
| **单 Cookie 单写者 → 多 Agent 并发受限** | INSTALL §7（`:145-146`） | ①整机单进程：所有客户端（DSH + OpenAI 层）共享同一 server；②服务端串行锁 + 排队（P1-1 加固一并做）；③确需并行只能多浏览器 profile 多账号独立部署——属用户决策项，项目默认不做 |
| 长任务超预算阻塞（P0-6） | review `:190-198` | API 层设硬 deadline（如 300s 可配），超时返回 504 + conversation_id 供后续轮询 |
| 凭证经日志/错误体泄漏（P1-5） | review `:260-271` | API 层错误体白名单（§3.3）；token 只走 env/credential seam，不打 stdout |

---

## 验证记录（本研究的实测）

| 命令 | 结果 |
|---|---|
| `uv run pytest -q`（仓库根，2026-11-13） | **62 passed, 1 failed**（1.85s）。唯一失败 `tests/test_gemini_client.py::test_persist_cookies_writes_file`，断言 `st_mode & 0o777 == 0o600` 在 Windows 上不成立——**review P0-1 已预先记载的既有问题**（`docs/review-20261102.md:103-107`：「Windows 上 `os.chmod(path, 0o600)` 根本不生效……该失败在本次评审前已存在」），非本研究引入，与合成测试全绿基线（62 绿）一致 |
| `uv sync --extra dev` | 为跑测试补装 pytest 9.1.1（任务书授权的验证动作；不改任何源码） |
| 本地内省 `mcp.list_tools()`（不联网） | 注册 **10 个工具**（清单见 §1.1），与 INSTALL §6 一致；README「9 个」为文档滞后 |
| asar 读证（仅 read 工具） | `dsh-mcp-client` streamable-http + headers + credential 模板四点实证（§2.d 表） |

## 证据索引表

| # | 结论 | 证据 |
|---|---|---|
| E1 | 两种 transport：stdio / streamable HTTP | `gemini_mcp/server.py:663-666, 694-716, 96-102` |
| E2 | Bearer 中间件（常量时间比较、401、覆盖全部 http scope） | `server.py:57-93` |
| E3 | token 优先级与 stdout 打印；127.0.0.1 绑定 | `server.py:667-674, 698-712` |
| E4 | 全局单例 client / metadata 缓存 / 后台轮换线程 | `server.py:46-54, 144-154, 190-199, 692` |
| E5 | timeout=180s 语义、韧性层参数、降级链 | `gemini_client.py:464, 175-180, 124-129, 496-508` |
| E6 | 生视频同步轮询 600s/15s + 超时 note | `server.py:500-549` |
| E7 | 10 工具注册（实测）；README 9 个系滞后 | 本报告 §1.1 实测输出；`README.md:109`；`INSTALL-FOR-AGENTS.md:128-135` |
| E8 | 续聊参数与 metadata 缓存机制 | `server.py:202-214, 306-312` |
| E9 | 错误码字典与异常结构 | `gemini_client.py:299-330`；SNlM0e 失败 `:566-570` |
| E10 | 每请求模型改造点：env 兜底位置 / 纯函数 / gemini- 前缀剥离 / generate 遍历链 | `gemini_client.py:481-508, 239-278, 208-212, 742-765` |
| E11 | 上传两阶段封装 | `gemini_client.py:1009-1094`（start `:1045-1067`，finalize `:1073-1094`） |
| E12 | 流式快照语义与 on_frame 挂点 | `gemini_client.py:941-943, 466, 724, 819-845` |
| E13 | DSH 原生支持 streamable-http + headers + `${credential:}` 模板 + 脱敏 | asar `dsh-mcp-client/lib/index.js:4, 113-123（:121 streamable-http 分支）, 250-294, 309-318`；`package.json:53` |
| E14 | 已知缺口 P0-6（无总预算）/ P1-1（并发无锁，http 放大）/ P1-5（错误泄漏）/ P2-5（路径泄漏） | `docs/review-20261102.md:190-198, 202-214, 260-271, 326-331` |
| E15 | 单 Cookie 单写者边界 | `INSTALL-FOR-AGENTS.md:143-151` |
| E16 | DSH 工具超时 600s 配置先例 | `README.md:80` |
| E17 | pytest 62 passed / 1 failed（Windows chmod 既有问题） | 本报告「验证记录」；与 review `:103-107` 记载一致 |

---

*研究完成：2026-11-13（Asia/Shanghai）。执行：gemini-api-research（DSH 子会话）。未改动任何源码/配置，唯一写入文件为本报告。*
