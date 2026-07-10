# FlowSight MVP 设计方案

## 1. 产品定位

FlowSight 是一个面向开发者和 AI coding agent 的运行时可视化调试地图。

它要解决的问题不是替代传统 debugger、APM 或 profiler，而是把一次真实请求的执行路径、代码结构、函数耗时、异常和关键变量快照放在同一个可交互视图里，让开发者快速回答：

- 这次请求实际经过了哪些代码？
- 哪一步开始出错？
- 哪个函数耗时异常？
- 哪些关键变量在路径中发生了变化？
- 静态代码结构和真实运行路径有什么差异？

第一版只做本地开发和测试环境，不做生产级阻塞调试。

## 2. MVP 范围

### 2.1 首选技术栈

第一版的目标应用只支持 Python FastAPI；FlowSight 自身使用两种主要语言：

- **Python：** 业务进程内 SDK、OpenTelemetry 集成、`@trace`、tracepoint backend、独立本地 sidecar、静态分析、Query API 和 SQLite 存储。
- **TypeScript：** React/React Flow 代码地图、timeline 和 Inspector。前端用 Vite 一类构建工具产出静态文件，随 Python wheel 发布。

最终用户运行时只需要 Python，不需要 Node.js。v1 不引入 Go、Rust、Electron 或独立 Node 服务；只有性能证据证明 Python sidecar 成为瓶颈后，才讨论替换局部实现。

选择 FastAPI 的原因：

- 路由结构清晰，适合映射到运行时路径。
- OpenTelemetry 对 FastAPI、HTTP client、数据库已有成熟 instrumentation。
- Python 可以通过 AST、decorator、middleware 和 frame inspection 较快实现原型。
- 适合做 AI coding agent 的本地调试辅助工具。

### 2.2 第一版用户闭环

目标闭环：

```python
from flowsight import FlowSight

fs = FlowSight()
fs.init_app(app)
```

开发者启动应用后，v1 明确分成两个可解释的接入层级：

**零额外代码层级：**

1. SDK 连接或启动本地 sidecar；开发者显式调用 `fs.open_ui()`，由 SDK 把带 token fragment 的 URL 交给本机浏览器而不打印 token。
2. 在打开的 UI 中发起一次 HTTP 请求。
3. UI 在请求结束后展示 recording HTTP server span，以及已启用 instrumentation 的 DB / 外部 HTTP span；若用户已有 sampler 丢弃该请求，状态页明确说明它不会被 FlowSight 看见。

**显式业务函数层级：**

4. 开发者给希望观察的 service / repository 函数添加少量 `@flowsight.trace`。
5. 再次请求后，代码地图点亮这些显式标记函数组成的真实运行路径。
6. 时间线展示每个节点耗时和状态；点击节点可查看源码位置、安全的入参/返回摘要和异常。
7. 给受支持的函数添加 tracepoint，选择要观察的局部变量。
8. 再次发起请求，看到该函数命中时的变量快照。

`fs.init_app(app)` **不承诺自动追踪任意业务函数**。零配置只保证 HTTP 和已启用的基础设施 spans；route → service → repository 的业务函数路径要求显式 `@flowsight.trace`。`include_packages` 自动包裹不在 v1 实现范围内，也不用于兑现 v1 的接入承诺。

### 2.3 v1 明确不做

第一版不做以下能力：

- 多语言支持
- 跨服务分布式 trace
- 生产环境 pause/resume
- 任意条件表达式断点
- 全量变量追踪
- WebSocket 实时流
- VS Code 插件
- AI 自动诊断
- 长期 trace 存储
- flame graph 或 CPU profiler
- Celery、后台任务和复杂异步任务追踪
- `include_packages` 自动扫描/包裹业务函数（v1 只支持显式 `@flowsight.trace`）
- 静态函数级 call graph（v1 静态层只做 contains/imports/route mapping）
- 多 worker 聚合、Gunicorn/进程管理器兼容
- 日志采集与日志查询（后置到 v1.1；v1 只保留异常和 span events）
- Windows sidecar/lock/process lifecycle（获得独立兼容性证据后再纳入）

这些能力都可以作为后续方向，但不应该进入第一版核心闭环。

### 2.4 关键边界约束（已确认）

以下四条是 v1 的硬边界，从隐含约定升级为显式约束，后续实现不得突破：

1. **回放，不实时干预。** 程序全速跑完并记录事件，UI 做事后回放。所谓"断点"是暂停动画，"流速"是播放速度，都不作用于真实进程。live 实时拦截是后续 milestone，不进 v1。
2. **只后端 HTTP 请求入口。** 可视化对象是 FastAPI 一次请求的后端执行路径。前端点击、端到端追踪不进 v1。
3. **下钻到函数级为止。** 节点粒度到类/函数，点进去看入参、返回、耗时、异常、子调用；不画函数内部逐行流动。
4. **v1 可视化调用流与函数边界摘要。** args/return 摘要挂在各自 span 上，不把它们连接成 ValueRef / ValueEdge 数据谱系（见 3.3）。

### 2.5 v1 支持矩阵

v1 的验收范围必须窄于“所有 Python/FastAPI 环境”：

- 标准 GIL-enabled CPython 3.12 和 3.13；free-threaded builds 不进入 v1 验收。
- macOS 和 Linux；Windows 不进入 v1 验收矩阵。
- FastAPI 单 worker、本地开发或测试进程。
- Uvicorn 普通启动和 `--reload`；多 worker 明确拒绝或给出不受支持提示。
- FastAPI、Starlette、OpenTelemetry 和 Pydantic 使用锁定并经 CI 验证的兼容范围；升级依赖必须先通过兼容性测试。
- Python 3.11 以下、CPython 3.14+、free-threaded CPython、其他 ASGI server 和多 worker 在获得独立兼容性证据前均不属于 v1 验收。

## 3. 核心设计模型

FlowSight 的核心是把软件拆成两层：

### 3.1 静态地图

静态地图描述项目本身的结构：

- 文件
- 模块
- 类
- 函数
- 方法
- FastAPI route
- 数据库调用点
- 外部 API 调用点

静态地图来自不执行源码的 LibCST 扫描，以及 SDK 对已加载 FastAPI router 生成的安全 route catalog。sidecar 不为扫描而 import 用户业务模块。

### 3.2 运行时水流

运行时水流描述某一次请求真实发生了什么：

- 请求从哪个 route 进入
- 实际经过哪些函数
- 每一步耗时多久
- 哪一步抛出异常
- 哪些安全的 span events / exception 属于这次请求
- 哪些 tracepoint 被命中
- 命中时关键变量是什么

UI 的主要视角应该是一次执行路径，而不是整个代码库。

### 3.3 数据谱系层（函数边界级，v1.1 实验，不进 v1 验收）

> **范围声明。** 数据谱系不是 v1 的验收条件。v1 只做 args/return 的**摘要展示**（挂在各自 span 上），**不做 ValueEdge 连线**、不做跨 span 的"数据流经 A→B→C"。完整的 ValueRef/ValueEdge 谱系（5.7、5.8）与其采集（6.6）属于 **v1.1 experimental**，排在主闭环打通之后（见第 11 节 Phase 6）。本节描述的是那个后置能力的目标形态，先记录方向，不占用 v1 工时。
>
> 原因：主产品价值是"这次请求走过哪些函数、哪里慢、哪里错、关键变量是什么"。数据谱系的对象身份、hash、脱敏、误连、断链 UI 会显著抬高工程复杂度，若混进 v1 会把项目从"可落地的 runtime map"拖成"半个 debugger + 半个 data lineage engine"。

在调用流之上，FlowSight 叠加一层可开关的"数据谱系"，回答：某块数据流经了哪些函数。

这一层的定位必须精确，避免过度承诺：

- **只追函数边界，不追函数内部。** 数据在 args 进入、在 return 离开时被观测，函数内部产生的中间变量不追。这与 2.4 的"函数级下钻"一致。
- **实现方式是叠加，不是核心。** 底座永远是调用流（稳定、成熟件支撑）；数据谱系是叠加在 span 树上的高亮层，可开关。它失败或断链都不影响调用流视图。
- **本质是尽力而为的启发式。** 值被拷贝或转换（如 `str(x)`、`x.id`）后身份会断链，只能靠值指纹做启发式续接，不保证 100% 准确。

之所以这一层现在可落地，是因为地基把它从"造引擎"降级成了"打标签"：

- `wrapt` 提供统一的函数边界拦截点，天然拿到每次调用的 args 和 return。
- OpenTelemetry 提供现成的 span 父子树，作为谱系线可以挂靠的骨架。
- 因此数据谱系退化为：在已有的 span 骨架上，对边界处的值打标并做匹配（见 5.7、5.8、6.6）。

明确的限制（UI 必须诚实呈现）：

- 只能看到函数进出口可见的数据，函数内中间变量不可见。
- 值经过拷贝/转换后身份可能断链，UI 在断链处显式标注"链路可能断裂"，不假装连上。
- 比纯调用流开销明显更大，默认作为可开关叠加层，而非常驻。

## 4. 系统架构

v1 采用“业务进程内 SDK + 独立本地 sidecar”的两进程结构。采集钩子必须留在业务进程内，UI/API/SQLite 则由跨 reload 稳定存活的 sidecar 统一拥有：

```text
Business process                         Local sidecar process
+-------------------------+              +--------------------------+
| FastAPI App             |              | Internal Event Ingest    |
| FlowSight Python SDK    | -- HTTP ---->| - authenticated batches  |
| - FlowSightSpanProcessor |  loopback   | - idempotency            |
| - @trace wrappers       |              | - overload reporting     |
| - tracepoint backend    |<-- config ---|                          |
| - safe event conversion|              | SQLite single writer     |
+-------------------------+              | Query / Tracepoint API   |
                                         | Static Analyzer          |
                                         | Bundled React UI         |
                                         +--------------------------+
```

这里的 HTTP 是 FlowSight 私有、版本化的本地事件协议，不是完整 OTLP receiver。v1 不为协议兼容性支付额外成本；未来如需接受外部进程或零代码 instrumentation，再单独增加 OTLP adapter。

### 4.1 技术选型基座

核心原则：FlowSight 不做另一个 APM、profiler 或 debugger，而是站在成熟生态上做"本地 runtime debugging map 产品层"。底座尽量拼成熟件，自研只集中在产品差异化的那几层（本地 ingest、代码地图点亮、tracepoint、回放）。

| 模块 | 选型 | 自研 or 复用 |
|---|---|---|
| 请求/DB/API trace | OpenTelemetry Python + FastAPI instrumentation | 直接用 |
| 本地进程模型 | Python sidecar + project-scoped state/lock file | **自研核心** |
| sidecar HTTP/UI API | Python FastAPI + Uvicorn，仅 loopback | 复用框架，自研协议与安全边界 |
| 用户级状态目录 | `platformdirs`，project_id 隔离 runtime/data | 复用 |
| 本地 ingest / storage | `FlowSightSpanProcessor` → SDK sender queue → loopback HTTP → SQLite | **自研核心** |
| 函数 span | `@flowsight.trace` + `wrapt` | 用 wrapt，不手写脆弱 decorator |
| 行级 tracepoint | `sys.monitoring` / 受限 `sys.settrace`，由前置 spike 决定支持矩阵 | 技术风险门通过后才实现 |
| 静态代码地图 | LibCST 提取结构；grimp 仅提供模块 import edges | 用，函数静态 call edge 不进 v1 |
| 图 UI | TypeScript + React + React Flow | 用，不从 D3 自搭 |
| 时间线 | 自建 waterfall，体验参考 VizTracer / Perfetto UI | 自研，仅借鉴 |

参考的最接近产品（借形态，不照搬）：Sentry Spotlight（本地 errors/traces/logs，形态最近，缺"代码地图点亮 + 变量 tracepoint"）、VizTracer（函数时间线最近，但是 profiler 不是请求级调试地图）、Jaeger/SigNoz/Tempo（trace waterfall 成熟，但主视角是服务/span 不是源码函数路径）、Phoenix/Langfuse/LangSmith（AI trace UX 值得借鉴，偏 LLM/agent）。

### 4.2 Runtime Process Model

`fs.init_app(app)` 到本地 UI 之间的所有权必须唯一，否则 reload、端口和 SQLite writer 都会变得不确定。v1 约定：

- **业务进程只运行 SDK。** SDK 安装幂等的 instrumentation，并通过有界缓冲把已经安全转换的事件发送给 sidecar；它不直接打开 SQLite，也不启动第二个 UI server。
- **sidecar 唯一拥有 UI/API、端口和 SQLite。** `fs.init_app(app)` 先读取 project-scoped state file 并探测健康状态；不存在时通过原子 file lock 竞选启动者，确保并发初始化最多启动一个 sidecar。
- **热重载只重新连接，不复用线程。** Uvicorn 新 worker 读取同一 state file 并连接仍存活的 sidecar；旧 worker 在 graceful shutdown 时只 flush 自己的 SDK buffer，不关闭共享 sidecar。
- **v1 只支持单业务 worker。** 检测到多个 worker/进程同时注册同一 app session 时，必须明确报错或标记 unsupported，不静默声称正确聚合。
- **只绑定 `127.0.0.1`。** sidecar 生成每次启动随机 capability token，校验 `Host`/`Origin`，默认不开放 CORS；内部 ingest 和写 API 都要求 token。
- **端口分配必须原子。** 默认尝试直接 bind `4040`，冲突时在同一 bind 循环中选择可用端口并把最终 URL 写入 state file；显式端口冲突则报错。
- **SQLite 使用 WAL + sidecar 内单写队列。** 队列必须有界；过载或写入失败不得阻塞业务请求或静默消失，而要增加 dropped/error 计数，并把受影响 trace 标记为 `incomplete`。
- **生命周期是显式的。** 提供 `FlowSight.flush()`、`FlowSight.shutdown()` 和 sidecar stop/idle-shutdown 路径。只承诺在可配置超时内完成 graceful flush，不承诺 crash、SIGKILL 或断电零丢失。
- **UI 以构建产物随 Python wheel 分发。** 最终用户运行 FlowSight 不需要 Node.js；Node 只用于仓库内前端开发和构建。

### 4.3 Internal Event Protocol

SDK 与 sidecar 使用 loopback HTTP + UTF-8 JSON 的私有版本化协议。v1 只保留最小端点：

```http
POST /internal/v1/hello
POST /internal/v1/renew
POST /internal/v1/events
POST /internal/v1/flush
POST /internal/v1/goodbye
GET  /internal/v1/tracepoints?since_revision=N
```

`hello` 建立单 producer lease，并携带 project/protocol/SDK/Python version 与能力；第二个同时存活的 producer 被明确拒绝为 `MULTI_WORKER_UNSUPPORTED`。`renew` 仅在 `(producer_id, lease_id)` 与当前 lease 精确匹配时延长同一 lease 的 TTL；它不会创建、替换或重新获取 lease。`renew` 响应丢失后，使用同一对 ID 重试对 lease 身份和所有权是幂等且安全的：重试只会再次延长同一个 lease。过期或不匹配的 lease 返回 `INVALID_LEASE`，重新获取必须调用 `hello` 并继续服从单 producer 规则。reload 新 worker 可以在有界时间内等待旧 lease 通过 `goodbye` 或续租停止后的短 TTL 到期释放。

事件 batch 至少包含 `protocol_version`、`project_id`、`producer_id`、`lease_id`、`batch_id`。每个事件包含唯一 `event_id`、单调 `producer_seq`、`type`、`schema_version`、时间戳和安全 payload；request-scoped 事件还携带 `request_trace_id`、`otel_trace_id`。v1 事件类型保持最少：`route_catalog.replaced`、`span.ended`、`span.enrichment`、`snapshot.captured` 和 `trace.drop_notice`。

不同 event type 使用独立 schema：route catalog 只有 project/session scope；span enrichment 使用完整 `(otel_trace_id, span_id)` 关联 args/return；drop notice 必须携带受影响的 request trace IDs 或明确的 producer sequence 范围，不能让 sidecar 猜某个全局 sequence gap 属于哪个请求。

- 发送语义是 at-least-once；sidecar 用 `event_id` / producer sequence 幂等去重，并且只有 SQLite transaction commit 后才 ACK。
- SDK 请求线程只做安全转换和非阻塞 enqueue，不直接等待网络；sender 负责批量发送、短超时和有限重试。
- 内部 HTTP 调用必须抑制 OTel instrumentation，避免自采集递归。
- batch、事件数和 payload 都有硬上限；协议、project、lease、schema 或认证不匹配时明确拒绝。
- tracepoint 配置通过 revision 增量轮询，不引入 WebSocket。
- project runtime metadata、token 和数据库放在每用户 runtime/data directory，权限限制为当前用户；默认不污染项目仓库。

### 4.4 仓库模块边界

Phase 0 起就使用可延续到 v1 的边界，避免先写成单文件再大拆：

```text
flowsight/
  __init__.py          # 只导出 FlowSight / trace 等稳定公共 API
  sdk/                 # app lifecycle、OTel processor、request context、sender
  protocol/            # SDK/sidecar 共用的版本化 wire schema；不放存储模型
  security/            # safe summary、redaction、path/host/origin validation
  sidecar/             # 独立进程入口、lifecycle、HTTP API/auth
  store/               # SQLite schema/migration/repository/single writer
  code_map/            # LibCST scan、route catalog merge、source hash
  static/              # ui/ 构建后进入 wheel 的只读产物
ui/
  src/                 # TypeScript/React/React Flow
tests/
  sdk/ sidecar/ store/ security/ protocol/ code_map/ packaging/ e2e/
```

依赖方向固定为：公共 API → `sdk`；`sdk` 只依赖 `protocol/security`，可以启动 sidecar CLI 但不得 import `store` 或直接写 SQLite；`sidecar` 依赖 `protocol/security/store/code_map`；`ui` 只通过 `/api/v1` 通信。`protocol` 不依赖 FastAPI、SQLite 或 React，防止传输 schema 与持久化/UI 模型互相绑死。前端构建产物只能由构建流程写入 `flowsight/static/`，运行时视为只读。

## 5. 数据模型

### 5.1 CodeNode

表示静态代码节点。

```text
id             # = stable_key
stable_key     # normalized_file_path + qualname + kind
kind
file_path
module
qualname
line_start
line_end
parent_id
location_hash  # hash(line_start + line_end + source_hash)
source_hash
```

节点身份必须拆成两层，不能用行号参与主 ID：

- **`stable_key = normalized_file_path + qualname + kind`** 作为稳定身份。用户在文件前面加几行不会改变它，因此 tracepoint 和历史 span 不会断。
- **`location_hash = hash(line_start + line_end + source_hash)`** 用来判断节点是否漂移（位置或源码变了）。
- **`source_hash`** 是函数体源码的 hash，用于检测内容变化。

漂移处理分阶段完成：Phase 2 只产出 CodeNode drift signal，并让旧 trace 显示 `source changed`；Phase 4 的 tracepoint 服务消费该 signal，把已存在的绑定标记为 `stale`，要求用户重新确认，而不是静默失效或错绑。Phase 2 不为尚未实现的 tracepoint 写库或改状态。

之所以要 `kind` 参与 `stable_key`，是因为项目中可能存在同名函数、嵌套函数、重载式 wrapper 或动态导入，qualname 本身不足以唯一。

### 5.2 StaticEdge

表示静态调用关系或结构关系。

```text
id
from_node_id
to_node_id
edge_type
confidence
```

`edge_type` 可以包括：

- `contains`
- `route_to_handler`
- `imports`

函数之间的真实运行边由 RuntimeSpan parent/child 关系派生。静态 `calls`、`db_access`、`external_http` 只能在后续 experimental 分析中出现，不是 v1 StaticEdge 的稳定类型。

Python 静态分析不可能完全准确，所以需要 `confidence` 字段区分确定关系和推断关系。

### 5.3 Trace

表示一次后端 HTTP 请求。v1 不把 CLI、后台任务或 WebSocket 会话伪装成 Trace。

```text
request_trace_id       # FlowSight 本地请求主键
otel_trace_id
root_span_id           # 本地 HTTP server span；与 otel_trace_id 一起标识一次请求
app_session_id
name
request_method
request_route          # 优先存路由模板；原始 path 必须安全处理
status_code
started_at
ended_at
duration_ms
outcome                # ok | error
collection_status      # collecting | complete | incomplete
error_summary
dropped_event_count
finalized_at
```

同一个合法上游 `otel_trace_id` 可以包含多个本地 HTTP server spans，因此不能用 OTel trace ID 单独代表“一次请求”。`request_trace_id` 是 FlowSight 本地主键，固定由 `project_id + otel_trace_id + root_span_id` 派生；`app_session_id` 只记录进程会话来源，不参与把两个 server spans 合并成一个请求。`complete` 表示该 request root span 已结束且 late-arrival 窗口关闭；只要 SDK/sidecar 报告丢弃、写入失败或超时缺口，就必须是 `incomplete`，UI 不得把部分 waterfall 冒充完整 trace。

### 5.4 RuntimeSpan

表示运行时中的一个执行片段。

```text
id
request_trace_id
otel_trace_id
span_id
parent_span_id
code_node_id
name
kind
start_ns
end_ns
duration_ns
status
exception_type
exception_summary
source_location
source_hash_at_capture
attributes_summary_json
args_summary_json
return_summary_json
redaction_report
truncated
```

`(otel_trace_id, span_id)` 必须唯一，重复 ingest 使用幂等 upsert；每个被 FlowSight 接受的 span 最终还必须归属一个 `request_trace_id`。child span 先于 server span 到达时，只能在有界关联 buffer 中暂存，待完整 parent chain 解析后归属，不能仅按相同 OTel trace ID 合并多个请求。默认 1 秒 TTL 后仍无法证明存在本地 server ancestor 的启动期、手工或后台 span 属于 v1 scope 外：丢弃并增加 `unscoped_span_count`，不创建 Trace，也不把任何现有 Trace 标成 incomplete。嵌套本地 server spans 以最近的 server ancestor 为归属边界，每个 server span 生成自己的 `request_trace_id`。`source_hash_at_capture` 用于检测历史 trace 与当前源码不一致；不一致时 Inspector 显示“源码已变化”，不能静默展示成执行当时源码。

`kind` 可以包括：

- `http_server`
- `route_handler`
- `function`
- `db_query`
- `http_client`
- `tracepoint`

### 5.5 Tracepoint

表示用户配置的观察点。

```text
id
code_node_id
file_path
line_no
location_hash_at_create
watched_vars
state           # enabled | disabled | stale | unsupported
backend
last_error
hit_limit
hit_count
revision
created_at
updated_at
```

**v1 tracepoint 语义（严格收敛，写死）：**

- 只支持**可执行语句行**，不支持任意行（空行、注释、纯定义行拒绝）。
- 捕获语义是"**执行该行之前**"的 locals；不承诺赋值语句执行后的新值。
- 只支持**指定变量名**，不支持任意 Python 表达式。
- 绑定的 CodeNode 漂移（`location_hash` 变化）时置 `stale`，需用户重新确认后才继续命中（见 5.1）。

后端实现的语义边界见 6.5。TRIAL-005 完成且 `phase4-tracepoint` gate 打开前，任何 backend 和 async 支持都不得标记为稳定；不受支持的函数形态必须在创建 tracepoint 时被明确拒绝。

### 5.6 Snapshot

表示 tracepoint 命中时捕获的局部状态。

```text
id
request_trace_id
otel_trace_id
span_id
tracepoint_id
captured_at
locals_json
redaction_report
truncated
payload_bytes
truncation_reasons_json
source_hash_at_capture
event_id
```

`locals_json` 存储序列化后的安全摘要，不存原始对象。

### 5.7 ValueRef（v1.1 experimental）

> 属于数据谱系层，不进 v1 验收（见 3.3）。v1 只在 span 上存 args/return 摘要，不建 ValueRef 表。

表示一次函数边界处被观测到的值，是数据谱系的节点。

```text
id
request_trace_id
otel_trace_id
span_id
role           # arg | return | field
name           # 参数名 / 字段名
py_id          # id(obj)，仅进程内有效
fingerprint    # type + 内容 hash + 关键字段，跨拷贝可比
summary        # 复用 5.6 的安全摘要
```

`py_id` 存在 GC 后对象地址复用的风险，因此不能单独用于匹配，必须与 `fingerprint` 联合判定。

### 5.8 ValueEdge（v1.1 experimental）

> 属于数据谱系层，不进 v1 验收（见 3.3）。

表示两个 ValueRef 之间的传播关系，是数据谱系的边。

```text
id
request_trace_id
otel_trace_id
from_value_ref_id
to_value_ref_id
match_kind     # same_object | same_fingerprint | derived_guess
confidence
```

匹配强度分三档：

- `same_object`：同 trace 内 `py_id` 一致（且 fingerprint 不冲突）——强边。
- `same_fingerprint`：`fingerprint` 一致但对象不同——中边。
- `derived_guess`：靠类型链/字段关系推断——弱边，UI 需标注为推断。

### 5.9 LogEvent（v1.1 experimental）

日志采集不进入 v1 验收。v1 只展示安全的异常摘要和 OTel span events；待主闭环稳定后，v1.1 再验证 OTel logging 的版本兼容、脱敏和查询体验。

```text
id
request_trace_id
otel_trace_id
span_id
timestamp
severity
logger_name
message_summary
attributes_summary_json
source_location
redaction_report
truncated
```

即使后续实现 LogEvent，也只能存预先安全转换的摘要，不能先存原文再在 UI 层脱敏。

### 5.10 IngestHealth

sidecar 必须暴露当前会话的采集健康状态：

```text
app_session_id
sdk_queue_depth
sidecar_queue_depth
dropped_event_count
unscoped_span_count
last_error_summary
last_success_at
```

这些字段用于把背压和存储错误显式显示在 UI，而不是把“没有数据”误解为“请求没有经过这里”。

### 5.11 EventReceipt 与 trace completion

sidecar 使用最小 EventReceipt 做传输去重：

```text
event_id
producer_id
producer_seq
request_trace_id
event_type
received_at
```

receipt 与目标表写入必须在同一 SQLite transaction，保留期随 request trace 一起清理。completion 状态机固定为：第一个 request-scoped 事件创建 `collecting`；root request `span.ended` 到达后等待默认 250ms late-arrival window，无 request-scoped sequence gap/drop/schema/storage 错误则为 `complete`，否则为 `incomplete`。正常存储条件下，从 SDK 观察到 root end 到 sidecar finalized 的总预算是 750ms，为 UI 查询/刷新留下 250ms。producer lease 消失或超时仍缺 root terminal event 时，标记 `terminal_missing`。collecting trace 可以查询，但 UI 不允许把它当成完整 replay。finalized trace 在保留期内收到合法超晚事件或 drop notice 时必须增加 revision、重新计算状态并让 UI 失效旧 replay 缓存。

project/session-scoped events 的 `request_trace_id` 为空，并按自己的 event ID 去重；它们不参与某个请求的 completion 计算。

## 6. 采集策略

### 6.1 请求级采集

OpenTelemetry FastAPI instrumentation 是 OTel trace/span identity 的唯一来源。FlowSight 从本地 server span 派生 `request_trace_id`，middleware 只补充安全的请求元数据和完成标记，不创建竞争性的 root trace/span，也不改写上游传播进来的合法 trace context。

请求边界读取当前 recording server span，按 5.3 派生 `request_trace_id`，并把 `request_trace_id/root_span_id` 放进请求级 `contextvars`。`FlowSightSpanProcessor.on_start` 只把该关联复制进有界的 span-association map，`on_end` 再生成事件；关联缺失的 span 只能在 SDK 的有界 TTL buffer 中按完整 parent chain 等待，绝不能只按 `otel_trace_id` 猜归属，也不能把 scope 外的 orphan 发送给 sidecar。TRIAL-004 必须验证 async、线程池、嵌套 server spans、scope 外 spans 和两个共享上游 trace 的并发请求不会串线；若所选 FastAPI/OTel 版本拿不到 recording server span，初始化必须明确失败或显示 unsupported，不能偷偷创建第二个 root。

需要记录：

- method
- route template 和安全 path summary
- status code
- start time
- end time
- exception
- `request_trace_id`（由本地 server span identity 派生）

### 6.2 OpenTelemetry 集成与所有权契约

使用 OpenTelemetry 负责框架级和基础设施级事件：

- FastAPI server span
- HTTP client span
- DB span
- Redis span
- 其他 OTel 已支持的库

FlowSight 不应该重复造 OpenTelemetry 已经解决的问题，而应该把 OTel span 映射到代码地图和 UI 时间线。

v1 的集成契约：

- 如果用户已经安装可扩展的 SDK `TracerProvider`，FlowSight 只附加一个 `FlowSightSpanProcessor`，不替换 provider、sampler 或用户已有 exporter；未知/不可扩展 provider 明确报不支持。
- FlowSight 不改写用户 sampler；若现有 sampler 产生 non-recording spans，状态页必须说明采集不可用。v1 的共存验收只覆盖 recording sampler。
- 如果进程尚无 SDK provider，FlowSight 创建自己拥有的 local-development provider，并使用 AlwaysOn recording sampler；只有这种默认路径承诺每个请求可见。
- `init_app` 必须幂等；重复初始化同一 app 不得重复 instrument、重复发送或重复注册 shutdown hook。
- Python OTel provider 没有可靠的公共 remove-processor 契约，因此每个进程/provider 最多永久注册一个可原子 enable/disable 的 `FlowSightSpanProcessor`。`FlowSight.shutdown()` drain 后把它变成 no-op；同一 provider 上 re-init 复用该实例，provider 被外部替换后则明确拒绝 re-init。
- `FlowSightSpanProcessor.on_start` 只维护有界的 request/span ID 关联，不读取运行时值；`on_end` 只做有界安全转换和非阻塞 enqueue。两个 OTel callback 都不访问网络，独立 sender 才批量发送给 sidecar。v1 不实现完整 OTLP receiver。
- `@flowsight.trace` 的 args/return 摘要走 FlowSight 私有 side-channel 并按 `(otel_trace_id, span_id)` 合并，不能写入公开 OTel attributes，以免用户已有 exporter 把本地调试值发送到外部系统。
- sidecar 自身的 API/UI 请求不得被 FlowSight instrumentation 再次采集，避免递归和噪音。
- 安全转换发生在进入 SDK 队列和跨进程发送**之前**；sidecar 再做一次 schema/size 校验，但不依赖 UI 脱敏。
- 为满足“recording 请求结束后 1 秒可见”，sender 默认批次间隔不超过 100ms，并在 root request 结束时唤醒发送；不把 FlowSight 建在用户或 OTel 默认 batch 周期上。
- shutdown 顺序固定为：停止接受新 FlowSight 事件 → drain FlowSight processor/sender queue → sidecar ACK/commit → 释放 producer lease。若 FlowSight 不拥有全局 provider，绝不 shutdown 用户 provider。每一步都有总超时和可见错误。
- span 允许乱序到达；root request span 结束后开启短暂 late-arrival 窗口，窗口关闭再把 trace 标记为 `complete`。重复 span 按 `(otel_trace_id, span_id)` 幂等处理。

### 6.3 函数级 instrumentation

v1 不做全量函数追踪。函数级 span 的包裹统一走 `wrapt`，不手写脆弱 decorator。

**v1 必做（稳定能力）：**

- 把 OTel FastAPI server span 映射到 route handler CodeNode；不再额外脆弱地自动包裹所有 route callable。
- 用户显式添加了 `@flowsight.trace` 的同步或异步函数（具体形态由兼容测试约束）。
- 已通过 TRIAL-005 支持矩阵、且命中 tracepoint 的函数。

显式 `@flowsight.trace` 会创建真实 OTel child span。这意味着用户已有 exporter 会看到函数代码身份、时间和 status code；使用 decorator 即表示用户同意导出这些最小 span 元数据。wrapper 必须按 [OpenTelemetry Python trace API](https://opentelemetry-python.readthedocs.io/en/latest/api/trace.html#opentelemetry.trace.Tracer.start_as_current_span) 用 `record_exception=False`、`set_status_on_exception=False` 创建 context-managed span；异常时只手工设置**无 description** 的 `StatusCode.ERROR` 并原样 re-raise。args/return 和安全异常摘要只走 FlowSight 私有 side-channel，不写 OTel attributes/event；共享 OTel span 中不得出现 exception event、异常 attributes、status description 或原始异常文本。

`include_packages` 自动扫描/包裹不实现于 v1。它需要单独的 v1.1 experimental 任务验证 async、classmethod、staticmethod、叠加 decorator、FastAPI dependency 注入、导入时机和热重载，再决定是否进入产品；当前配置模型不得接受一个“看似可用、实际未验收”的 `include_packages` 选项。

示例：

```python
from flowsight import trace

@trace
def calculate_price(order):
    ...
```

### 6.4 安全摘要与变量快照

安全摘要器是 Phase 0 的存储边界，不是 Phase 4 才补的 UI 功能。OTel attributes、异常、args/return、span events 和 snapshot 在进入队列前都必须经过同一套限制。变量快照仍只在 tracepoint 命中时捕获。

默认规则：

- 只捕获用户指定变量
- 最大深度 3
- 单变量最大 1KB
- 单次 snapshot 最大 16KB
- 序列化过程中按变量名和字段路径脱敏
- 不执行用户表达式
- 只展开明确白名单的标量和内建容器
- 未知对象只记录模块名、类型名和截断标记
- 不调用未知 `__repr__`、property、iterator、自定义 serializer 或其他用户代码

安全摘要器必须能处理循环引用、恶意 `__repr__`、抛异常的属性访问和超大容器；任何单值失败只能产生安全占位符，不得让业务请求失败，也不得退回原始字符串。

### 6.5 行级 tracepoint backend 风险门

行级 tracepoint（见 5.5、4.1）的目标是“低开销触发行事件 + 安全拿到该行前的局部变量”。这两件事在不同 backend 上并不天然一起满足：

- `sys.settrace` 的 line event 会提供 `frame`，但它安装在线程级；async coroutine 在 `await` 时可能与同线程其他请求交错，也可能与 debugger/coverage 冲突。
- `sys.monitoring`（PEP 669）的 LINE callback 只提供 code 对象和行号，不天然提供 frame locals。

因此不能在设计阶段预先宣布自动退化一定可行。TRIAL-005 必须对 CPython 3.12/3.13 的 sync、async、线程池、并发、嵌套 tracepoint、旧 tracer 恢复和开销做 go/no-go 验证，并产出明确支持矩阵：

- 只有验证通过的 backend / 函数形态进入 v1。
- 若 async 隔离无法证明，v1 明确拒绝 async tracepoint，但不影响 async 函数的普通 `@trace` span。
- 若 `sys.monitoring` 无法安全取得目标 frame，只有在受限 `sys.settrace` 方案证明不会越出目标调用范围时才可启用；否则缩小功能，不允许启用全局 trace。
- 创建 tracepoint 时就校验支持性，不能等请求运行后静默失败。

**TRIAL-005 本地候选结论（尚未完成任务或打开 gate）：** `go-with-scope-reductions`，选择 `sys.monitoring` 的 per-code `LINE` event，global event mask 必须始终为 0；不提供 `sys.settrace` fallback。最终采用前仍需显式批准下列范围收缩，并让同一不可变提交通过 macOS/Linux × CPython 3.12/3.13 CI：

- 仅支持标准 GIL-enabled CPython 3.12/3.13，并要求 generic monitoring tool ID 3/4 至少一个可安全占用；不得抢占 debugger、coverage、profiler、optimizer 或其他 tool。
- 每次启动必须用真实 local-line event 自检 `sys._getframe(1)` 能取得 callback 对应的精确 frame/code/line；由于 [`sys.monitoring` 的公开 callback 形状](https://docs.python.org/3.13/library/sys.monitoring.html)不提供 frame，自检失败、audit 拒绝、残留 LINE callback、非零 global mask 或 probe/已配置 code 的非零 local mask 时必须 fail closed 且保留外部状态。公开 API 无法枚举无关 code 的 stale local mask，因此支持声明不得扩展到该不可检测情形。
- 支持通过完整创建校验的 exact Python sync/coroutine function、bound instance/class method 和 static function，包括已验证的 closure free/cell vars、嵌套、递归和并发 coroutine。lambda/comprehension、generator/async-generator、one-line/目标自身 definition-line、不能解析为 exact Python function 的对象和非法直接 spec 在创建时拒绝。code-object-only backend 无法可靠识别所有 nested pure `def`/`class` line；Phase 4 创建 API 仍必须按 5.5 用 source/AST validator 拒绝这些行，不能把 backend 接受误报成产品支持。
- 线程池只在调用方显式传播 `contextvars` request context 时归属请求；raw/unpropagated executor work 不捕获。即使 context 已复制，request scope 结束后恢复的 task/thread 也必须因 active-token 失效而不捕获。
- 合成 existing-`sys.settrace` callback 可共存，且 FlowSight 不读写其 slot；这不等于真实 debugpy/coverage 集成已验证，后两者在 v1 tracepoint 支持矩阵中保持 unverified/unsupported。TRIAL-005 的负向重叠实验在 3.12/3.13 都证明 per-coroutine install/restore 会串请求、丢事件并残留 tracer，因此禁止该退化路径。
- 普通 callback/serializer/sink 错误 fail open 并进入有界 health/drop 计数；`KeyboardInterrupt`/`SystemExit` 等 process-control `BaseException` 不吞掉。完整 start/stop lifecycle 串行化；shutdown 依次关闭 callback admission、停 local events、bounded drain 已进入 callback、注销 callback、释放 tool ID，timeout 可重试，并发 stop 必须幂等。

候选 Phase 4 spike regression budget 由 digest `sha256:3993fd75a45b1e14be3e04d56534928cadc928a92dce5af6473398e5c14c30e9` 固定并在每个支持矩阵 job 执行：unconfigured-code median/p95 paired ratio 分别不得超过 1.15×/1.75×；configured-but-unscoped median/p95 分别不得超过 15µs/25µs；captured-hit median/p95 分别不得超过 200µs/300µs。该预算是单 callback/hit 的 spike regression guard，不是 Phase 5 的 10ms request SLA，不覆盖 production queue/transport，也不得按 hit limit 相乘解释为 request 预算。绝对阈值只有在不可变 macOS/Linux × 3.12/3.13 CI 全通过后才成为已接受预算；放宽阈值必须改变 digest 并单独 review。最终 digest 同步记录在 `spikes/tracepoint_backend/RESULT.md`，工作树变动期间不得从设计文字推断旧 digest 仍有效。

候选支持矩阵、负向证据、benchmark digest 与原始统计记录在 `spikes/tracepoint_backend/RESULT.md`。这段候选记录不是 Phase 4 acceptance；只有任务卡的批准、review、不可变 SHA、CI 和 command-backed fact evidence 全部完成后才可把该 backend 标记为稳定。

### 6.6 数据谱系采集（v1.1 experimental）

数据谱系（3.3）复用函数级 span 的 `wrapt` 边界，不新增拦截机制：

- 在被追函数的边界处，对 args 和 return 生成 ValueRef（`py_id` + `fingerprint` + 安全摘要）。
- 只对被追函数生效，不追第三方库内部，与函数级 span 的 include/exclude 共用同一套范围。
- 匹配在**同一个 trace 内**进行：先按 `py_id` 连强边，再按 `fingerprint` 连中边，最后按类型/字段推断弱边。
- 全部限深、限长、限数量，超出即截断并标记，绝不为谱系放宽 6.4 的安全上限。

示例：

```json
{
  "user_id": 123,
  "order": {
    "type": "Order",
    "summary": "<UNINSPECTED Order>",
    "truncated": true
  }
}
```

## 7. 安全和隐私策略

安全策略必须是默认开启，而不是可选插件。

### 7.1 全入口预持久化脱敏

脱敏与限长发生在业务进程内的安全事件转换阶段，早于 SDK queue、网络发送和 SQLite。sidecar 只接受已经符合安全 schema 的事件，并再次校验；禁止“先存原文，查询或展示时再脱敏”。

规则覆盖 request route/path、URL/query/header、OTel attributes/events、SQL 摘要、异常、args/return 和 snapshot。变量名、字段名或 key 命中以下关键词时直接脱敏：

```text
password
passwd
pwd
token
secret
key
auth
credential
cookie
session
api_key
access_token
refresh_token
```

脱敏结果：

```text
"<REDACTED>"
```

### 7.2 值内容脱敏

对字符串值做基础 PII 检测：

- email
- phone
- SSN
- credit card like number
- bearer token
- JWT like token

### 7.3 采集限制

默认限制：

```text
max_active_tracepoints = 20
max_tracepoints_per_function = 5
max_snapshot_size = 16KB
max_value_size = 1KB
max_value_depth = 3
max_span_event_size = 16KB
max_events_per_trace = 2000
max_events_per_batch = 128
max_batch_payload = 1MB
sdk_queue_capacity = 2048
sidecar_writer_queue_capacity = 4096
sender_batch_interval = 100ms
late_arrival_window = 250ms
default_retention = 1 hour
```

队列满时默认 fail-open：业务请求继续执行，FlowSight 产生可见错误、增加 dropped counter，并把 trace 标为 `incomplete`。不得无限扩容、无限阻塞业务线程或静默丢弃。

### 7.4 本地绑定

v1 UI 和 API 只监听：

```text
127.0.0.1
```

不提供远程访问能力。但 loopback 不是鉴权，sidecar 还必须：

- 每次启动生成不可猜的 capability token，不写入应用日志或 trace。
- 首次 UI URL 使用 `#token=...` fragment，避免 token 进入 HTTP request line；UI 读取后放入 `sessionStorage` 并清除地址栏 fragment，后续 API 通过 `Authorization` header 发送。
- `init_app` 只可记录不含 token 的 ready/port 状态。`open_ui()` 是显式敏感动作；headless 用户必须通过同样显式的 CLI 命令获取 URL，自动日志和 FlowSight telemetry 永远不包含 token。
- 内部 ingest、源码、snapshot 和 tracepoint 写 API 都验证 token。
- 只接受预期 `Host`；浏览器写请求同时要求 bearer token 和精确匹配的 `Origin`，缺失/不匹配都拒绝，以此构成 v1 的 CSRF 防护；默认不发送允许跨域读取的 CORS headers。
- 所有日志、异常、源码和 snapshot 按不可信文本渲染，禁止注入 HTML。
- source API 将 realpath 限制在配置的 project root 内，拒绝 `..`、symlink escape 和任意文件读取。
- macOS/Linux runtime/data 目录使用当前用户私有权限（目录 0700，token/state/SQLite 文件 0600）。

## 8. 存储和查询

### 8.1 存储选择

v1 使用 SQLite。

原因：

- 本地开发足够
- 部署简单
- 方便用户删除
- 查询 trace 和 timeline 足够快
- 不引入额外服务

数据库只由 sidecar writer 打开写连接。Query API 使用独立只读连接、`busy_timeout` 和短事务；schema 必须有版本号和前向 migration。retention sweeper 按 trace 级联删除 spans/snapshots，并在测试中覆盖并发读取、WAL checkpoint 和启动恢复。

### 8.2 主要索引

```text
Trace(started_at)
Trace(collection_status, started_at)
RuntimeSpan UNIQUE(otel_trace_id, span_id)
RuntimeSpan(request_trace_id, start_ns)
RuntimeSpan(code_node_id, start_ns)
Snapshot(request_trace_id, span_id)
Tracepoint(code_node_id, state)
```

### 8.3 主要查询

v1 只需要支持几类查询：

1. 最近 trace 列表及 collecting/complete/incomplete 状态
2. 某个 trace 的完整 span 时间线和 dropped-event 标记
3. 某个 trace 中涉及的 code node
4. 某个 span 的安全详情与 snapshots
5. 某个 code node 的 tracepoint 配置
6. 当前 app session 的 ingest health
7. project-root 内、带 source hash 校验的源码片段

## 9. UI 信息架构

### 9.1 主界面

主界面采用三栏布局：

```text
+------------------+---------------------------+------------------+
| Trace List        | Timeline                  | Inspector        |
| Code Graph        | Runtime Path              | Snapshot         |
| Ingest Health     |                           | Events / Errors  |
+------------------+---------------------------+------------------+
```

### 9.2 左侧：Trace 和代码地图

左侧包括：

- 最近 trace 列表
- 当前 trace 状态
- 代码地图
- 搜索函数或文件

代码地图默认只加载当前 trace 子图及一层静态上下文，不一次性把整个仓库塞进 React Flow。API 必须分页，UI 必须有节点上限、按模块折叠和“继续展开”动作；超限时显示截断状态。

### 9.3 中间：运行时间线

中间区域展示：

- 请求整体耗时
- span 瀑布图
- 函数调用嵌套
- DB/API 调用
- 异常点
- tracepoint 命中点

支持：

- 播放
- 暂停
- 快进
- 拖动时间点
- 点击 span

播放本质是事件回放，不是把真实程序跑慢。默认时间轴保留耗时相对关系，但对极长 I/O 和极短 span 做有上下限的视觉压缩，避免真实 60 秒请求真的播放 60 秒或微小 span 完全不可见。

### 9.4 右侧：Inspector

点击不同对象时展示不同内容：

点击 trace：

- path
- method
- status
- duration
- error summary

点击 span：

- 函数名
- 源码位置
- duration
- args summary
- return summary
- exception
- safe span events
- source changed 状态

点击 tracepoint：

- watched vars
- captured locals
- redaction report
- hit count

## 10. API 草案

### 10.1 SDK API

```python
from flowsight import FlowSight, trace

fs = FlowSight(
    project_root=".",
    ui_port=4040,
)

fs.init_app(app)
fs.open_ui()  # explicit; opens the tokenized URL without logging it

@trace
def my_function():
    ...

# tests / explicit lifecycle
fs.flush(timeout=2.0)
fs.shutdown(timeout=2.0)
```

v1 配置模型不接受 `include_packages`；业务函数采集只通过显式 `@flowsight.trace` 开启。

### 10.2 Tracepoint API

```http
POST /api/v1/tracepoints
Content-Type: application/json
Authorization: Bearer <startup-token>

{
  "code_node_id": "node_123",
  "line_no": 42,
  "watched_vars": ["user_id", "order"],
  "hit_limit": 10
}
```

完整管理接口：

```http
GET    /api/v1/tracepoints
PATCH  /api/v1/tracepoints/{tracepoint_id}
DELETE /api/v1/tracepoints/{tracepoint_id}
POST   /api/v1/tracepoints/{tracepoint_id}/reconfirm
```

列表接口使用 cursor pagination。`PATCH` 只修改 enable、watched vars 和 hit limit；`reconfirm` 必须提交客户端刚看到的 current `location_hash`，避免确认期间代码再次漂移。disable 与 delete 是不同语义。

### 10.3 Trace 查询 API

```http
GET /api/v1/traces
GET /api/v1/traces/{request_trace_id}
GET /api/v1/traces/{request_trace_id}/spans
GET /api/v1/traces/{request_trace_id}/spans/{span_id}
GET /api/v1/traces/{request_trace_id}/spans/{span_id}/snapshots
GET /api/v1/code-nodes
GET /api/v1/code-nodes/{code_node_id}
GET /api/v1/code-nodes/{code_node_id}/source
GET /api/v1/status
```

`status` 返回 sidecar/SDK/protocol version、producer lease、queue depth、drop count、storage error 和当前 tracepoint backend 支持状态，不返回 token 或敏感路径。

内部事件协议不属于公共 API：

```http
POST /internal/v1/events
Authorization: Bearer <startup-token>
```

它只接受限量、版本化且已安全转换的 batches；sidecar 必须做 schema、总大小、事件数量和 app session 校验。

## 11. 实现阶段

阶段划分的原则：**v1 = Phase 0–5 的调试闭环**（本地请求 trace + 显式标记的函数路径 + 源码定位 + 变量快照 + 回放）；**Phase 6 数据谱系是 v1.1 experimental**，不进 v1 验收。每个阶段是一个薄的可验证纵切，且任一后置阶段失败都不塌前面的链路。

### 开工前风险门

风险门不是“以后补测试”，而是允许后续阶段开始的前置证据：

1. **产品与支持契约：** 冻结 2.2、2.3、2.5；明确零配置/显式 `@trace` 的差异、logs 不进 v1、单 worker 与 Python 版本范围。
2. **TRIAL-004 进程与 OTel spike：** 证明 sidecar 单例、普通启动/reload、端口冲突、已有 `TracerProvider` 共存、幂等 init 和有界 shutdown；失败则不得进入持续 Phase 0/1。
3. **TRIAL-003 安全事件摘要：** 在任何 telemetry 持久化前证明全入口脱敏、限长、恶意对象无副作用；失败则不得进入 Phase 1。
4. **TRIAL-005 tracepoint backend spike：** 在 Phase 4 前给出 backend/函数形态 go/no-go 和支持矩阵；不允许用“后面再退化”替代证据。

### Phase 0：可安装骨架 + sidecar 进程模型 + 安全存储边界

目标：

- Python package、FastAPI demo 和锁定的 CPython 3.12/3.13 测试矩阵
- sidecar 单例、state/lock file、token、127.0.0.1、原子端口选择和显式生命周期（见 4.2）
- SQLite schema version、WAL、sidecar 有界单写队列、错误/丢弃健康状态
- 全入口安全摘要 primitive（复用 TRIAL-003 结论）
- 根目录 `package.json`/`package-lock.json` 管理 `ui/` 下的 TypeScript/React 工程；检查顺序固定为前端 check/test/build → Python tests → 临时目录构建 wheel → 干净 venv 安装该 wheel，并运行复制到临时目录的自包含 UI-serving probe。probe 清空 `PYTHONPATH`/user site、使用 importlib mode，必须证明导入路径来自已安装 wheel；无论是否提交 bundle 都不能拿工作区源码或旧产物冒充通过
- `make check` 真正运行 format-check、lint、type check、Python/前端测试；bootstrap-only 状态必须明确显示，不能冒充产品验收

交付：

- 干净虚拟环境安装 wheel 后无需 Node 即可运行 `FlowSight().init_app(app)` 并打开带 token 的空 UI
- 普通启动和一次 Uvicorn reload 后 UI URL/SQLite owner 保持唯一，新 worker 正确重连
- 重复 init 幂等；显式 flush/shutdown 在有界超时内落盘所有已接受事件
- queue overload、SQLite 错误和不支持的多 worker 都产生可观察结果，不静默失败

### Phase 1：OTel walking skeleton + 完整 trace 状态 + waterfall

目标：

- 接入 OTel FastAPI/httpx/一个 DB instrumentation
- `FlowSightSpanProcessor` → 安全内部事件 → sender → sidecar → SQLite
- 定义幂等、乱序、late-arrival、complete/incomplete 和 retention 行为
- UI 展示 trace list 和 span waterfall

交付：

- recording 请求结束后 1 秒内 UI 出现 finalized 的 `complete` 或明确的 `incomplete` trace
- 可以看到 route template、status、duration、HTTP/DB/API span 瀑布和 ingest health
- 用户已有 provider/exporter 仍正常工作，FlowSight 不生成重复 root span
- query/header/SQL/exception fixtures 证明敏感原文没有进入 SDK queue、内部请求或 SQLite

### Phase 2：保守静态结构地图 + route/source 映射

目标：

- LibCST 扫描 project root，提取文件/模块/类/函数结构
- grimp 只提供模块 import edges；函数级静态 `calls` edge 不进 v1
- 提取函数、类、路由，生成 CodeNode（stable_key + location_hash，见 5.1）
- 建立 route 到 handler 的映射
- source API 强制 project-root 边界，并保存执行时 source hash

交付：

- UI 分页展示当前 trace 子图及有限静态上下文
- 当前 trace 能关联到 route handler
- 源码变化后旧 trace 明确显示 source changed，并产出供 Phase 4 消费的 drift signal；本阶段不创建或更新 tracepoint

### Phase 3：函数级路径 + 源码 Inspector

目标：

- 支持 `@flowsight.trace`（wrapt）
- OTel server span 映射 route handler，业务函数只靠显式 `@flowsight.trace`
- 生成 RuntimeSpan，点击 span 看源码位置、耗时、安全异常、args/return 摘要

交付：

- demo 在 service / repository 显式加 `@trace` 后，可以看到 route → service → repository 路径
- 点击 span 能跳到源码位置并看到摘要
- 只调用 `init_app` 而未加 decorator 时，UI 明确说明业务函数路径尚未采集
- 用户已有 OTel exporter 只看到显式 decorator 产生的函数身份、时间和 status；args/return 与安全异常摘要只留在 FlowSight 私有通道

### Phase 4：受限 tracepoint + 变量快照

目标：

- TRIAL-005 已完成，支持矩阵与性能预算已冻结
- UI 只允许给受支持函数添加 tracepoint（受限语义，见 5.5、6.5）
- snapshot 复用 Phase 0 安全摘要器，不另造 serializer

交付：

- 下一次请求命中 tracepoint 后能看到"执行该行前"的指定变量快照
- 默认不泄露常见 secret
- CodeNode 漂移后 tracepoint 正确置 stale
- 不受支持的 async/backend/已有 tracer 组合在配置时明确拒绝，不污染其他并发请求

### Phase 5：回放 + 联动 + polish

目标：

- timeline 播放/暂停/调速，节点逐步高亮
- span 和 graph 联动
- 播放时间轴保留相对耗时但采用有界视觉压缩，手动调速为附加功能
- 完成 clean-install、reload、并发、过载、存储故障、安全和浏览器 E2E

交付：

- 用户可以像看水流一样回放一次请求
- 默认播放即可一眼看出耗时瓶颈
- 干净环境 5 分钟内接入；最终用户运行无需 Node
- 在冻结的 demo harness、同一环境开/关 FlowSight 对照下，连续 100 个请求无无界内存/磁盘增长，baseline trace 模式 p95 额外延迟不超过 10ms
- **至此 v1 验收闭环完成**

### Phase 6：数据谱系实验层（v1.1 experimental，不进 v1 验收）

目标：

- 在 `wrapt` 边界对 args/return 生成 ValueRef（见 5.7）
- 同 trace 内做 py_id / fingerprint 匹配，生成 ValueEdge（见 5.8、6.6）
- UI 增加可开关的谱系高亮层

交付：

- 能高亮"某块数据流经 A -> B -> C"
- 断链处显式标注"链路可能断裂"
- 关闭谱系层后不影响调用流视图

这一阶段排在主闭环之后；即使效果不理想也不影响 v1。v1 阶段只保留 args/return 摘要展示，不建 ValueRef/ValueEdge 表。

## 12. 技术风险

### 12.1 函数追踪噪音

风险：

函数级追踪很容易产生大量无意义 span。

策略：

- 默认只追 OTel 基础 spans 和显式 `@flowsight.trace`
- experimental auto-wrapper 才使用 include/exclude
- 默认不追第三方库内部函数
- 对短函数可以聚合或隐藏

### 12.2 Python 动态特性

风险：

静态调用图不准确。

策略：

- v1 不生成函数级静态 `calls` edge
- 运行时路径优先
- 静态图只提供 contains/imports/route mapping 导航

### 12.3 变量序列化副作用

风险：

访问对象属性可能触发 property、lazy load 或副作用。

策略：

- 只对白名单标量与内建容器做有限展开
- 未知对象只输出模块名、类型名和截断标记
- 不调用未知 `__repr__`、property、iterator、Pydantic/dataclass 自定义 serializer 或任何用户代码
- 限深、限长、限元素数，并覆盖循环引用与恶意对象测试

### 12.4 性能开销

风险：

`sys.settrace` 或全局 instrumentation 会明显拖慢应用。

策略：

- 默认不启用全局 trace
- tracepoint 只对 TRIAL-005 证明可隔离的目标函数形态生效
- 采样和 hit limit 默认开启
- SDK/sidecar 队列有界，记录 dropped events 和 p50/p95 开销
- 明确标注 v1 只用于本地开发

### 12.5 依赖选型与不依赖清单

明确 v1 依赖（详见 4.1）：Python/FastAPI、OpenTelemetry、wrapt、LibCST、grimp（仅 import graph）、platformdirs、SQLite、TypeScript/React/React Flow，以及由 TRIAL-005 选定的 `sys.monitoring`/受限 `sys.settrace` backend。NetworkX 只有出现明确图算法需求时才加入，不作为骨架默认依赖。

明确 v1 **不依赖**（只借鉴，不作为核心）：

- `debugpy` / `pydevd`：断点语义和变量查看值得借鉴，不嵌入成核心。
- `PyCG`：项目已 archived，算法可参考，不作核心依赖。
- `pyan3`：许可证与分析深度都不适合当产品核心。
- bytecode rewrite：v1 不碰，复杂度会迅速失控。

### 12.6 sidecar 与 OTel 生命周期风险

风险：reload 竞态、陈旧 state file、重复 instrumentation、sidecar 崩溃或已有 OTel provider 会造成重复数据、孤儿进程或业务进程阻塞。

策略：

- 原子 lock + PID/health/token 校验，不只相信 state file。
- SDK 发送路径有严格超时并 fail-open；sidecar 不可用时业务请求继续，FlowSight 明确报错。
- init/flush/shutdown/重新连接全部幂等，并用真实子进程测试普通启动和 reload。
- OTel provider 所有权、processor 添加和卸载顺序按 6.2 固定，不覆盖用户配置。

### 12.7 数据谱系断链风险

风险：

值经拷贝或转换后身份断裂，谱系线中断或误连。

策略：

- `py_id` 必须与 `fingerprint` 联合判定，规避 GC 地址复用。
- 三档 `match_kind` 区分强/中/弱边，弱边标注为推断。
- 断链在 UI 显式呈现，不做静默续接。
- 谱系层可开关，失败不影响调用流底座。

## 13. 成功标准

v1 成功标准不是功能多，而是能稳定完成一个高频调试动作：

```text
我发了一个请求
我看见 OTel 基础路径，以及我显式标记的业务函数实际走过哪些代码
我知道哪里慢或哪里错
我能在关键函数抓到变量
我能把这次执行回放给自己或 AI agent 看
```

可以认为 v1 完成的判断标准（对应 Phase 0–5）：

- 干净 CPython 3.12/3.13 环境中 5 分钟内接入一个单 worker FastAPI demo，运行时无需 Node
- 普通启动和 Uvicorn reload 都只有一个 sidecar、一个 UI URL 和一个 SQLite writer
- recording 请求结束后 1 秒内 UI 出现 `complete` 或明确的 `incomplete` trace；用户 sampler 丢弃的请求只产生状态提示，不伪造 trace
- timeline 零额外代码展示 HTTP/DB/API；给 demo service/repository 加 `@trace` 后展示完整业务路径
- 点击 span 能看到源码位置、耗时、异常、args/return 摘要
- tracepoint 只在 TRIAL-005 证明支持的函数形态下捕获指定变量
- query/header/SQL/exception/args/return/snapshot 安全测试证明常见 secret 在持久化前已脱敏
- 队列、sidecar 或 SQLite 故障不阻塞业务请求，并在 UI 标记错误/incomplete
- graceful shutdown 在有界超时内 flush 已接受事件；工具关闭后不影响业务应用正常运行
- clean-install、浏览器 E2E、100 请求有界资源和 baseline p95 额外延迟不超过 10ms 的验收证据齐全

**明确不在 v1 验收范围内**（属于 v1.1 experimental / 后置）：

- 数据谱系 ValueEdge 连线（"数据流经 A→B→C"）——v1 只做 args/return 摘要
- `include_packages` 自动 wrapper——v1 不提供该配置，只保证 OTel route mapping 和显式 `@trace`
- 日志采集与日志查询
- 多 worker 聚合、CPython 3.11 以下和未经 CI 验证的运行时
- 任何未通过 TRIAL-005 的 tracepoint backend 或函数形态

v1 只需证明这一条闭环稳定成立："本地请求 trace + 函数路径 + 源码定位 + 变量快照 + 回放"。数据谱系很有价值，但排在主闭环之后，避免项目从"可落地的 runtime map"变成"半个 debugger + 半个 data lineage engine"。

## 14. 一句话版本

FlowSight 不做另一个 APM、profiler 或 debugger，而是用业务进程内 Python SDK、独立本地 Python sidecar、SQLite 和 TypeScript/React UI，把“HTTP/DB/API 基础路径 + 显式标记的业务函数路径 + 安全变量快照”产品化。v1 先让单 worker FastAPI 本地开发中的一次请求可被稳定记录、映射、检查和回放；数据谱系、日志、多 worker 与更广运行时支持全部后置。
