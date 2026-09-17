# mimo-desktop-bridge

**从外部驱动小米 MiMo 桌面版**（MiMo Desktop / 内嵌 MiMoCode 引擎）的最小可用桥接层。

MiMo Desktop 启动时会在用户数据目录写下一个 `desktop-api.json`，里面是它**自己暴露的本地 HTTP 接口**
（端口 + Bearer token）。本项目把这份接口封装成可直接调用的 CLI / Python 库，用于：

- 读 MiMo 的会话列表和完整对话内容
- **发指令给 MiMo**，让它在自己的项目上下文里干活
- 订阅 SSE 事件流，实时看它的思考 / 工具调用 / 权限请求
- **看透一个会话**：地址、模型、对话内容、**思维链**、工具调用、交付文件
- 跨 Agent 编排（本 Agent 规划，MiMo 执行）

不需要 playwright，不需要 UI 自动化 —— 走的是官方留下的**原生接口**。

---

## 快速开始

```bash
# 0) 自检：验证链路、鉴权、401/404 边界（只读，安全）
python scripts/mimo_api.py selftest

# 1) 看有哪些会话
python scripts/mimo_api.py sessions --limit 10

# 2) 让用户在 MiMo 界面点「新建任务」并随便发一句话，然后抓 sessionId
python scripts/watch_new_session.py --timeout 600

# 3) 发指令（model 自动探测；默认权限下会弹确认卡片）
python scripts/run_turn.py <sessionId> "把当前目录的文件列表告诉我" --events 40

# 4) 读某个会话的全部消息
python scripts/mimo_api.py messages <sessionId>

# 5) 把一个会话彻底看透（地址/模型/内容/思维链/工具/交付文件，六项一次给全）
python scripts/inspect_session.py <sessionId>
python scripts/inspect_session.py <sessionId> --turn "让它做点事" --perm "完全访问权限" --events 40
```

零依赖：只用 Python 标准库（`urllib`）。Python 3.8+。

---

## 能力矩阵

| 能做 | 不能做 |
|---|---|
| 列会话、读任意会话的全部消息 | ❌ **新建会话**（接口里没有这条路由） |
| 发一轮指令，异步收结果 | ❌ 代替用户批准权限（应答走引擎内部地址） |
| 订阅事件流（含权限请求事件） | ❌ 用别的 Host 头绕过白名单 |
| 读会话里出现过的文件 | |

---

## 两个硬限制（决定了自动化流程的形状）

### 1. 没有「新建会话」

`POST /v1/sessions` 不存在。`turns` 打到不存在的 id 只会 404/400，**不会隐式创建**。

→ 流程起手式：**让用户在 MiMo 里点一下「新建任务」**（空白任务可能不落库，所以让它随便发一句），
再用 `watch_new_session.py` 抓回 sessionId，之后全程 API 驱动。

### 2. 工具调用的审批默认阻塞会话

`turns` 的 `perm` 字段值是**中文字面量**：

| `perm` | 行为 |
|---|---|
| 不传 / `""` | 默认权限。受保护操作弹卡片，**会话进入 busy，后续 turns 一律 409** |
| `"帮我审批"` | 仅文件编辑类自动批准 |
| `"完全访问权限"` | **全自动批准** |

## ⚠️ 安全警告

传入 `"完全访问权限"` 意味着**从外部绕过 MiMo 的用户确认 UI**。一旦你这样调，
目标 Agent 的所有 bash / 文件写操作都会静默执行，用户看不到确认卡片。

- 默认不要用；
- 只在用户明确知情并授权时用；
- 用的时候必须在回复里明确告诉用户你做了这件事。

卡住时的症状：事件流出现 `{"type":"permission","req":{...}}`，之后所有 `turns` 返回
`409 {"code":"busy"}`。**本项目无法代替用户批准** —— 只能请人到 MiMo 界面点掉那张卡片。

---

## 接口一览

基址 `http://127.0.0.1:<port>/v1`，鉴权 `Authorization: Bearer <token>`，Host 头必须匹配该端口。

| 方法 | 路由 | 说明 |
|---|---|---|
| GET | `/v1/health` | 健康检查 |
| GET | `/v1/sessions?limit=N` | 会话列表（默认 200，上限 2000） |
| GET | `/v1/sessions/{id}/messages` | 会话消息 |
| GET | `/v1/sessions/{id}/events` | SSE 事件流 |
| POST | `/v1/sessions/{id}/turns` | 发指令 → `202 {"ok":true}` |
| GET | `/v1/sessions/{id}/files?u=<path>` | 读文件 |

完整规格、逆向证据、未确认项见 [`references/api-spec.md`](references/api-spec.md)。

---

## 测试

```bash
python tests/test_mimo_api.py      # 18 个用例，本地打桩服务，不需要 MiMo 在跑
python -m pytest tests/ -v         # 有 pytest 的话
```

打桩服务按真实服务端的准入规则复刻了 Bearer 校验、Host 白名单、路由分派和错误码，
所以这些测试同时是**接口契约的回归护栏**。

其中 `TestIsSettled` 是被真实 bug 逼出来的：assistant 消息在流式输出期间就已经出现在
`/messages` 里（`info.time.completed` 仍为 `None`），只看 role 会在半途误判结束。

---

## 免责声明

本项目基于对 MiMo Desktop 客户端（`app.asar`，未混淆）的静态分析与黑盒实测，
**非官方接口**。MiMo 升级后路径、字段名或 `perm` 字面量都可能变化 —— 先跑 `selftest`，
失败再对照 `references/api-spec.md` 重新核对。

逆向仅用于在自己机器上做合法的自动化，请遵守软件许可协议。
