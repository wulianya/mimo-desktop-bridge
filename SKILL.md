---
name: mimo-desktop-bridge
description: 从外部驱动小米 MiMo 桌面版（MiMo Desktop / 内嵌 MiMoCode 引擎）。当需要「让 MiMo 去做某件事」「把指令发给 MiMo」「读 MiMo 的会话内容」「跨 Agent 编排」「在 MiMo 里开新任务并接管」时使用。原理是 MiMo 运行期在用户数据目录写下的 desktop-api.json（port+token），暴露一个仅监听 127.0.0.1 的 HTTP API。
agent_created: true
---

# MiMo Desktop Bridge

把小米 MiMo 桌面版当作**可被外部程序驱动的 Agent**：读会话、发指令、订阅事件流、取文件。

## 什么时候用

- 用户说「你直接操作 MiMo / 让 MiMo 去干 / 把这个任务转给 MiMo」
- 需要跨 Agent 编排：本 Agent 做规划，MiMo 在自己的项目上下文里执行
- 需要批量读 MiMo 的历史会话（做归档、复盘、统计）
- 用户抱怨「MiMo 卡住了」→ 用事件流判断是不是挂在权限确认上

**不要**用浏览器自动化（playwright / computer-use）走这条路 —— **有原生 HTTP API**，见下方。

## 前置事实（先记这五条，省掉 90% 试错）

1. **API 只在 MiMo 运行期存在。** 启动时把 `{api:1, port, token, pid}` 写进
   `%APPDATA%\Xiaomi MiMo\desktop-api.json`（权限 0600），**退出即删除**。
   文件不存在 = MiMo 没开，不代表没有这个能力。端口每次启动随机。
2. **鉴权**：`Authorization: Bearer <token>`，正则 `/^Bearer[ \t]+(\S+)$/i`（不区分大小写）。
3. **Host 头强校验**：必须是 `127.0.0.1:<port>` / `localhost:<port>` / `[::1]:<port>`，否则 403。
   用标准 HTTP 客户端按 `http://127.0.0.1:<port>/...` 请求即可，不要手动覆盖 Host。
4. **只有 6 条路由**，别去猜别的：
   | 路由 | 方法 | 说明 |
   |---|---|---|
   | `/v1/health` | GET | 健康检查，返回 `{"ok":true,"api":1,"app":...,"engine":"ready"}` |
   | `/v1/sessions?limit=N` | GET | 会话列表（默认 200，上限 2000） |
   | `/v1/sessions/{id}/messages` | GET | 该会话全部消息 |
   | `/v1/sessions/{id}/events` | GET | SSE 事件流 |
   | `/v1/sessions/{id}/turns` | POST | 发一轮指令 → **202** |
   | `/v1/sessions/{id}/files?u=<path>` | GET | 读会话内文件 |
5. **存在两个硬限制**（见下节），它们决定了整套自动化的写法。

## 两个硬限制（决定了流程形状）

### 限制一：没有「新建会话」路由

`POST /v1/sessions` 不存在；`turns` 打到不存在的 id 只会 404/400，**不会隐式创建**。

→ **起手式**：让用户在 MiMo 界面点「新建任务」并随便发一句话（空白新任务可能不落库），
然后用 `scripts/watch_new_session.py` 抓回 sessionId，之后全程 API 驱动。

### 限制二：工具调用的审批默认会阻塞会话

`turns` 的 `perm` 字段值是**中文字面量**（从 app.asar 的 `Ef` 枚举实测得到）：

| `perm` | 行为 |
|---|---|
| 不传 / `""` | 默认权限。每个受保护操作（bash / write / …）弹确认卡片，**会话进入 busy，后续 turns 一律 409** |
| `"帮我审批"` | 仅 `edit`/`write`/`apply_patch` 自动批准；bash 等仍走审查 |
| `"完全访问权限"` | **全自动批准**，不再弹卡片（实测：事件流里零 `permission` 事件） |

卡住时的症状与解法：
- 事件流里出现 `{"type":"permission","req":{"id":"per_...","permission":"bash","patterns":[...]}}`
- 之后所有 `turns` 返回 `409 {"code":"busy","message":"session busy"}`
- **desktop-api 没有权限应答路由**（应答走引擎内部接口，外部够不到）→ 只能请人到 MiMo 界面点掉卡片。

> ⚠️ **安全边界**：传 `"完全访问权限"` 等于从外部绕过用户的确认 UI。
> 默认不要用；只在用户明确知情并授权时用，并且要在回复里明确告知这一点。

## 快速上手

```bash
# 0) 自检（只读，验证链路、鉴权、401/404 边界）
python scripts/mimo_api.py selftest

# 1) 看有哪些会话
python scripts/mimo_api.py sessions --limit 10

# 2) 让用户点「新建任务」发一句，然后抓 sessionId
python scripts/watch_new_session.py --timeout 600

# 3) 发指令（自动探测 model；默认权限会弹卡片）
python scripts/run_turn.py <sessionId> "把当前目录的文件列表告诉我" --events 40

# 4) 需要无人值守时（⚠️ 绕过确认 UI，须用户授权）
python scripts/run_turn.py <sessionId> "在 D:\\tmp\\out.txt 写入 hello" --perm "完全访问权限" --events 60
```

## 关键坑位（都是踩过的）

- **`model` 是必填**。服务端第一行就是 `if(!message.trim() || !sessionId || !model) return bad-request`。
  不传 → `400 bad-request`。正确做法：从会话历史里探测（`run_turn.py` 已实现自动探测）。
- **`dir` 参数不改变目标的运行时 cwd**（实测：传了沙箱路径，目标仍报默认会话目录
  `C:\Users\<u>\XiaomiMiMoProjects\.mimo-sessions\<date>\<title>`）。
  想让它在某个目录干活，要么**让用户建任务时就选好项目目录**，要么**在指令里写绝对路径**。
  机理未确认，别照着文档猜。
- **`turns` 是异步的**。POST 回 202 就结束了；要拿结果必须另开 `events`（SSE）或轮询 `messages`。
- **202 不代表会话存在**。带合法 body 打一个**不存在**的 sessionId，服务端照样回 202
  （校验只查 message/sessionId/model 三个字段，会话真实存在性在异步后续才暴露）。
  所以拿到 202 之后必须靠 `messages` / `events` 确认，不能凭 202 断定成功。
- **`events` 里 `busy` 事件会大量重复**，这是心跳不是错误；判断结束看 `session.idle`。
- **409 busy 是按会话隔离的**：某个会话卡住时，**别的会话仍然能正常收发**；
  换 `origin` 也解不开（实测）。所以卡住时的标准动作是**换一个干净会话**，不是重试。
- ★ **判断「一轮结束」不能只看「末条是不是 assistant」**：assistant 消息在流式输出期间
  就已经出现在 `/messages` 里，此时 `info.time.completed` 是 `None`、工具 part 可能还是 `running`。
  正确条件 = `completed` 有值 + 无 running part + 连续两次消息数不变。
  用 `mimo_api.is_settled()` / `settle_wait()`，别自己写循环。
- **思维链是可读的**：`parts[].type=="reasoning"` 里有模型显式输出的推理原文；
  `parts[].type=="step-finish"` 里有 token 与 cost 细分。要审计它在想什么，读这两处。
- **别采信模型的自述**：实测协议层 `info.model.modelID` 是 `xiaomi/mimo-x-pro-preview`，
  而它自己回答"我是 mimo-v2.5-pro"。**要准确值就读协议字段。**
- **`model` 形如 `provider/modelID`**，例如 `xiaomi/mimo-x-pro-preview`（从 messages 的
  `info.model.{providerID,modelID}` 拼）。
- 本机另有端口 62384（一律 404）与 55153（一律 401），**都不是可用接口**，别浪费时间。

## 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `找不到 desktop-api.json` | MiMo 没运行 | 请用户启动 MiMo（**不要擅自重启**） |
| `401 unauthorized` | token 过期（MiMo 重启过） | 重新读 cred 文件 |
| `403 forbidden-host` | 手动改了 Host 头 | 用 `http://127.0.0.1:<port>` 正常请求 |
| `404 not-found` | 路径不是那 6 条之一 / sessionId 不存在 | 对照路由表 |
| `409 busy` | 该会话在引擎侧被判定未空闲（不只是权限卡片） | **换一个干净会话**；换 origin 无效 |
| 发了 202 但目标没动 | 202 只代表 gate 放行，不代表会话存在 | 轮询 `messages` / 订阅 `events` 确认 |
| `503 engine-not-ready` | 引擎没起来或未登录 | 让用户在 MiMo 里发一条消息预热 |
| 发出去没反应 | 忘了 202 只是受理 | 订阅 `events` 或轮询 `messages` |

## 文件

| 路径 | 说明 |
|---|---|
| `scripts/mimo_api.py` | 客户端库 + CLI（6 路由封装、`selftest`、`is_settled`/`settle_wait`） |
| `scripts/run_turn.py` | 闭环驱动：发指令 → 订阅事件 → 等收尾 → 打印结果 |
| `scripts/inspect_session.py` | **看透一个会话**：地址 / 模型 / 内容 / 思维链 / 工具调用 / 交付文件 |
| `scripts/watch_new_session.py` | 捕捉用户在 UI 里新建的会话 |
| `references/api-spec.md` | 完整 API 规格 + 逆向证据 + 实测记录 + 未确认项 |
| `tests/test_mimo_api.py` | 离线单元测试（本地打桩服务，不需要 MiMo 在跑） |
