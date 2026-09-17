# MiMo Desktop 本地 API 规格（逆向 + 实测）

> 来源：`C:\SoftwareFiles\Xiaomi MiMo\resources\app.asar`（未混淆，可直接字符串检索）
> 验证：2026-09-17，MiMo Desktop `26.914.142245`，引擎 `desktop-5198ff5`
> 原则：本文每条都注明**证据出处**或**实测命令**。没有证据的推测一律标注「未确认」。

## 1. 服务怎么起来的

| 环节 | 证据（asar 内符号） |
|---|---|
| 凭证文件名常量 | `const Hwe = "desktop-api.json"`，`function d5(e){return Y.join(e,Hwe)}` |
| 写入凭证 | `function Gwe(e,t,r,n,i)` → `writeFileSync(o, JSON.stringify(dJ(t,r,n)), {mode:384})` + `chmodSync(o,384)`（384 = 0o600） |
| 凭证结构 | `function dJ(e,t,r){return {api:Ch, port:e, token:t, pid:r}}`，`const Ch = 1` |
| 删除凭证 | `function Vwe(e,t)` → `rmSync(d5(e),{force:true})`（进程退出时调用 `m()`） |
| 监听 | `function h()` → `p.listen(0,"127.0.0.1")`，随机端口；成功后 `Gwe(userDataDir, port, token, process.pid)` |
| token 生成 | `Xwe({api, token: Gs(), userDataDir})` —— 每次启动重新生成 |

结论：**端口随机、token 每次启动变、文件生命周期 = 进程生命周期**。

## 2. 请求准入（三道闸）

```
1) Host 校验    function _4(host, port)
                → 只接受 127.0.0.1:<port> / localhost:<port> / [::1]:<port>
                → 不通过：403 {"code":"forbidden-host","message":"host not allowed"}

2) Token 校验   function E4(headers) 用 /^Bearer[ \t]+(\S+)$/i 提取
                → 与 e.token 做 timingSafeEqual
                → 不通过：401 {"code":"unauthorized","message":"invalid or missing token"}

3) 路由分类     function uJ(method, url)
                → 不匹配任何路由：404 {"code":"not-found","message":"not found"}
```

错误码映射（`function aJ(code)` + `oJ` 文案表）：

| code | HTTP | message |
|---|---|---|
| unauthorized | 401 | invalid or missing token |
| forbidden-host | 403 | host not allowed |
| forbidden-file | 403 | file not allowed |
| not-found | 404 | not found |
| busy | 409 | session busy |
| bad-request | 400 | bad request |
| engine-not-ready | 503 | engine not ready |
| not-logged-in | 503 | not logged in |

## 3. 路由表（全部，`uJ()` 就是全集）

`uJ` 先剥 `/v1` 前缀再按段数分派，`sessionId` 必须过 `/^[A-Za-z0-9_.-]{1,64}$/`（`function cJ`）。

| 方法 | 路径 | 分派 | 转发到引擎 |
|---|---|---|---|
| GET | `/v1/health` | `{kind:"health"}` | 本地（`function a()`） |
| GET | `/v1/sessions?limit=N` | `{kind:"sessions",limit}` | `GET /experimental/session?roots=true&limit=N` |
| GET | `/v1/sessions/{id}/messages?dir=` | `{kind:"messages",sessionId,dir}` | `GET /session/{id}/message?directory=` |
| GET | `/v1/sessions/{id}/events?dir=` | `{kind:"events",...}` | `GET /event?directory=`（SSE 转发） |
| POST | `/v1/sessions/{id}/turns` | `{kind:"turns",sessionId}` | 走 harness（非纯 HTTP） |
| GET | `/v1/sessions/{id}/files?u=<path>&dir=` | `{kind:"file",sessionId,path,dir}` | 本地，读 `messages` 里出现过的文件 |

`limit` 归一：`function lJ(v)` —— 非正/非整数 → 200，上限 2000（`const iJ = 2e3`, `const u1 = 200`）。

### POST /v1/sessions/{id}/turns

请求体字段与校验（`function d(h)` + 外层解析）：

```js
message: string   // 必填，且 trim() 后非空
sessionId: string // 来自路径
model:   string   // ★ 必填！ provider/modelID，例如 "xiaomi/mimo-x-pro-preview"
dir:     string?  // 可选
perm:    string?  // 可选，取值见 §4
origin:  string?  // 可选
files:   string[]?   // 可选，过滤空串
plugins: string[]?   // 可选，过滤空串
```

```js
async function d(h){
  const m = typeof h.message=="string" ? h.message : "";
  if(!m.trim() || !h.sessionId || !h.model) return Nr("bad-request");   // ← 400
  ...
  e.runHarness({message, model, sessionID, dir, perm, origin, files, plugins}, {onGate})
}
```

响应：

- 受理成功 → **202 `{"ok":true}`**（注意：只是受理，不是完成）
- gate 被阻塞 → 409 busy；超时阈值 `e.gateWaitMs ?? 5000`（`const Ywe = 5e3`）
- 请求体 > 1_000_000 字节 → 400（`const Wwe = 1e6`）

### SSE 事件流 `GET /v1/sessions/{id}/events`

- 响应头 `Content-Type: text/event-stream`，先发一帧 `event: meta` → `{"api":1,"sessionId":"..."}`
- 心跳：每 25s 一帧注释 `: ping`（`const Kwe = 25e3`）
- 数据帧 `data: {json}`，实测出现过的 `type`：

| type | 含义 |
|---|---|
| `busy` | 心跳/进行中，**会大量重复，不是错误** |
| `ui` | 归一化后的 UI 事件，`ui.kind` ∈ `text` / `tool` / `title` … |
| `text-partial` | 已聚合的文本增量 |
| `permission` | **权限确认请求**，`req` 里有 `id` / `permission` / `patterns` |
| `idle` | 本轮结束 |
| `closed` | 流关闭 |

实测样例（截断）：
```
{"type":"ui","ui":{"kind":"tool","tool":"bash","status":"running","callID":"call_...","input":{"command":"$dir = \"...\"; New-Item ..."}}}
{"type":"permission","req":{"id":"per_g001a0aeb68f08001BStmWelGp","sessionID":"...","permission":"bash","patterns":["New-Item -ItemType Directory -Force -Path $dir", ...]}}
```

## 4. `perm` 的合法取值（关键）

```js
const Ef = { AcceptEdits: "帮我审批", FullAccess: "完全访问权限" };

function dw(e, t) {
  return e === Ef.FullAccess ? { mode: "auto", reply: "always" }
       : e === Ef.AcceptEdits && bP(t) ? { mode: "auto", reply: "always" }
       : { mode: "ask" };
}
// bP(t): t.trim().toLowerCase() ∈ {"edit","write","apply_patch"}
```

→ `"完全访问权限"` 时 mode 为 auto、reply 固定 `always`（即不再询问）。
→ UI 三档对应：默认权限（`""`）/ 帮我审批（`"帮我审批"`）/ 完全访问权限（`"完全访问权限"`）。

**权限应答不在本 API 内**：桌面端应答权限用的是
`Hr(engineUrl, engineAuth, permissionId, {reply:"once"|"allow-always"...}, dirQuery)`，
走的是**引擎内部地址 + 引擎凭证**，外部拿不到。所以外部无法代替用户批准。

## 5. 引擎侧（外部够不到，仅供理解）

- 引擎是 **MiMoCode**（opencode 系），由主进程内嵌启动，凭证是 `Basic <user>:<password>`。
- 引擎自身的路由全集（源码 `app.route(...)`）：
  `/project` `/pty` `/config` `/experimental` `/session` `/permission` `/workflows`
  `/question` `/bash-interactive` `/provider` `/capability?` `/sync`
  → 其中 `POST /session` 就是「新建会话」，但**只对引擎地址开放，desktop-api 没有转发**。
- 本机 `netstat` 实测另外两个监听端口 62384（一律 404）与 55153（一律 401），不是可用接口。

## 6. 实测记录

| 测试 | 命令 | 结果 |
|---|---|---|
| 健康检查 | `GET /v1/health` | `{"ok":true,"api":1,"app":"26.914.142245","engine":"ready"}` |
| 列会话 | `GET /v1/sessions?limit=5` | 200，返回真实会话数组 |
| 读消息 | `GET /v1/sessions/{id}/messages` | 200，含 `info.model = {providerID:"xiaomi", modelID:"mimo-x-pro-preview"}` |
| 错误 token | `Authorization: Bearer deadbeef` | 401 unauthorized |
| 伪造 Host | `Host: evil.com` | 403 forbidden-host |
| 假 sessionId | `GET .../messages` | 404 not-found |
| 缺 model | `POST .../turns {"message":"..."}` | 400 bad-request |
| 缺 sessionId | `POST /v1/sessions/ses_probe.../turns` | 400 bad-request（**不会隐式建会话**） |
| 正常发指令 | `POST .../turns`（带 model） | **202 `{"ok":true}`**，目标实际回复 |
| 默认权限写文件 | 同上 + 要求写文件 | 事件流出现 `permission` → 会话 busy → 后续 turns 409 |

## 7. 未确认项（别当结论用）

- `dir` 参数是否在**某些条件下**能改变目标 cwd —— 实测一次未生效，机理未查。
- `files` / `plugins` / `origin` 三个字段的实际语义，未做实验。
- `/v1/sessions/{id}/files` 的可读范围（源码里有 `zue(...)` 做白名单，来自 messages 里出现过的文件；
  403 `forbidden-file` 的精确触发条件未穷举）。
- `api` 版本号目前只有 `1`，未来若升到 2，路径前缀会变成 `/v2`（`Ch` 既用于前缀也用于凭证字段）。
