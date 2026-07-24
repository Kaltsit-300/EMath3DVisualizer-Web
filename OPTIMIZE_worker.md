# Web Worker 优化总结

## 目标

将网格计算（`/api/mesh` 调用）从主线程移出到 Web Worker，解决多方程同时绘制时主线程阻塞导致的 UI 卡顿问题。

## 改动文件

### 1. `webapp_mesh_worker.js`（新建）

独立 Worker 脚本，负责所有 `/api/mesh` 请求的并发执行。

**消息协议（主线程 → Worker）：**

| `type` | 字段 | 说明 |
|--------|------|------|
| `fetchAll` | `tasks[]` | 批量任务数组，每个任务含 `id, equation, params, port, view_radius, lod, quality` |
| `fetchMesh` | `id, equation, params, …` | 单任务（向后兼容/备用路径） |

**消息协议（Worker → 主线程）：**

| `type` | 字段 | 说明 |
|--------|------|------|
| `meshResult` | `id, data, error` | 单个结果（到达顺序不定） |
| `fetchAllDone` | `ids[]` | 全部任务已投递（实际用计数器判断完成） |
| `error` | `message` | 未预期错误 |

**关键设计：**
- **并发 fetch**：使用 `Promise.all` 同时发起所有 `/api/mesh` 请求，充分利用 HTTP/1.1 并发连接。
- **端口自动检测**：Worker 内部通过 `/health` 探测可用端口（8000-8029），无需主线程告知端口。
- **结果无序到达**：结果到达后立即 `postMessage`，主线程按到达顺序应用，支持 UI 渐进式更新。

### 2. `webapp_app.js`（修改）

#### 状态新增字段

```js
state.meshWorker     // Worker 实例引用
state.workerDrawSeq  // 当前 drawSeq 快照，用于过滤过期结果
state.workerPending  // 仍在等待的 Worker 结果数量
state.workerFallback // true = Web Worker 不可用，降级到主线程
```

#### 新增函数

| 函数 | 职责 |
|------|------|
| `initMeshWorker()` | 创建 Worker，注册事件处理器；Worker 失败时设置 `workerFallback=true` |
| `onWorkerMessage(evt)` | 分发 Worker 消息：`meshResult` → `applyWorkerMeshResult`；`fetchAllDone` → 忽略（用计数器判断） |
| `applyWorkerMeshResult(id, data)` | 将 Worker 返回的 mesh 合并到 Three.js scene |
| `onAllWorkerResultsIn(seq)` | 全部 Worker 结果到达后：清除 `isPreview` 标记；若 `quality >= 2` 触发高质量升级 |
| `scheduleWorkerMeshFetch(equations, seq, opts)` | 封装 `fetchAll` 消息，发送到 Worker；Worker 不可用时调用 `scheduleFallbackMeshFetch` |
| `scheduleFallbackMeshFetch(...)` | 主线程降级路径：顺序 `await fetch`（与原 `drawAll` 行为一致） |
| `scheduleWorkerHighQualityFetch(equations, seq)` | 高质量升级的 Worker 路径 |
| `scheduleFallbackHighQualityFetch(...)` | 高质量升级的主线程降级路径 |

#### `drawAll` 改造

- 调用 `initMeshWorker()` 初始化 Worker（非阻塞）。
- 将方程分为两类：
  - **前端精确平面**（线性方程）→ 直接本地构建网格，无需 Worker。
  - **其他方程** → 通过 `scheduleWorkerMeshFetch` 交给 Worker。
- 交线预览（`loadIntersectionCurves`）与 Worker 任务并行执行，互不阻塞。

#### `loadHighQualityVersion` 改造

- 高质量 mesh 请求同样通过 Worker 并发执行（`scheduleWorkerHighQualityFetch`）。
- 降级路径为 `scheduleFallbackHighQualityFetch`。
- 交线高质量刷新与 mesh 升级并行执行。

## 降级方案

| 失败场景 | 降级行为 |
|----------|----------|
| `new Worker()` 抛出异常（Worker 不支持） | `workerFallback = true`，所有后续 fetch 回退到主线程顺序执行 |
| Worker 脚本加载失败（404 等） | `worker.onerror` 触发，`workerFallback = true` |
| Worker 运行时错误 | 记录 `console.warn`，不影响主线程继续运行 |
| 后端端口不可达 | Worker 内每请求超时 5s（`fetch` 默认），不阻塞主线程 |

## 性能收益

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| 3 方程同时绘制 | 主线程顺序 fetch，总耗时 ≈ 3 × 单请求时延 | Worker 并发 fetch，总耗时 ≈ 1 × 最慢单请求时延 |
| 交线 + 网格并行 | 串行（交线必须等网格，或网格必须等交线） | 两者并行（`previewIntersectionsPromise` 与 `scheduleWorkerMeshFetch` 同时执行） |
| 高质量重绘 | 主线程串行重请求所有方程 | Worker 并发重请求，与交线刷新并行 |

## 文件清单

```
math-3d-visualizer/
├── webapp_app.js        # 修改：集成 Worker 调用
├── webapp_mesh_worker.js # 新建：Worker 脚本
└── OPTIMIZE_worker.md   # 本文档
```
