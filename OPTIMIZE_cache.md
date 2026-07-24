# 缓存优化记录

**日期**: 2026-07-24
**目标**: 为 `/api/mesh` 端点添加 LRU 内存缓存，避免重复计算相同方程

---

## 改动概览

### 1. `api_server.py` — 新增三层 LRU 缓存

```python
from functools import lru_cache
```

新增三个带 `@lru_cache(maxsize=256)` 装饰器的函数：

| 函数 | 作用 | 缓存 key |
|------|------|----------|
| `_parse_cached(equation, params_json)` | 缓存 SymPy 解析结果（方程文本 → parsed dict） | `(equation, params_json)` |
| `_mesh_cached_impl(equation, params_json, view_radius, lod, quality)` | 实际执行网格计算 | `(equation, params_json, view_radius, lod, quality)` |
| `_mesh_cached(equation, params_json, ...)` | 入口包装，含参数自动补全逻辑 | 同 `_mesh_cached_impl` |

**设计说明**：

- `params`（dict）不可哈希，无法直接作为 `lru_cache` 的参数 → 改为 `params_json = json.dumps(sorted(params.items()), sort_keys=True)` 作为哈希 key
- `params_json` 保证相同参数的字典无论 key 顺序如何都能命中同一缓存
- `maxsize=256`：最多缓存 256 组不同的 (equation × params × 视图参数) 组合
- 两层分离（`parse` + `mesh`）是因为 `build_isosurface_data` 内部还会调用 `parse_for_api`，合并会导致同一方程解析两次

### 2. `/api/mesh` 端点改造

**改造前**（每次请求都执行）：
```
parse_for_api → build_isosurface_data → 网格计算
```

**改造后**（命中缓存时跳过计算）：
```
params_json → _mesh_cached() → [缓存命中? 直接返回]
                        ↓ 未命中
              parse_for_api → build_isosurface_data → [存入缓存 → 返回]
```

### 3. 已有持久化缓存保留

`services/cache.py` 的 SQLite 缓存（`mesh_cache.get` / `mesh_cache.put`）在 `build_isosurface_data` 内部继续生效，与新增的 LRU 内存缓存形成**两级缓存**：

```
请求 → LRU 内存缓存 (api_server.py)     ← 进程内，毫秒级
     → SQLite 持久化缓存 (services/cache.py) ← 磁盘，跨进程有效
     → 完整网格计算 (mesh_service.py)
```

## 验证结果

```bash
python -c "import api_server; print(api_server._mesh_cached.cache_info())"
# CacheInfo(hits=0, misses=0, maxsize=256, currsize=0)  ✅

python -c "import api_server; import api_server as srv; print('import 成功')"
# ✅ 无报错
```

## 预期效果

- **首次请求**：与原来相同，需要完整计算
- **同参数重复请求**：直接返回缓存数据，响应时间从百毫秒级降至微秒级
- **解析缓存**：SymPy 解析（`parse_for_api`）也被缓存，避免重复语法分析

## 可调参数

如需调整缓存容量或清空缓存，可在运行时调用：

```python
from api_server import _mesh_cached, _parse_cached

# 查看缓存状态
print(_mesh_cached.cache_info())

# 清空所有缓存
_mesh_cached.cache_clear()
_parse_cached.cache_clear()
```
