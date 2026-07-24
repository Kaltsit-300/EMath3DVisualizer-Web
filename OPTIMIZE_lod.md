# Three.js LOD (Level of Detail) 实现总结

## 概述

为 math-3d-visualizer 项目添加了 Three.js LOD 功能，根据相机距离动态切换高低模，优化渲染性能。

## 实现日期
2026-07-24

## 后端修改 (api_server.py)

### 1. 新增 MeshLODRequest 模型

```python
class MeshLODRequest(BaseModel):
    equation: str = Field(..., description="Equation text")
    params: dict[str, float] = Field(default_factory=dict)
    view_radius: float = Field(default=10.0, ge=0.05, le=500.0)
    quality: int = Field(default=1, ge=1, le=3, description="1=low, 2=medium, 3=high")
```

### 2. 新增 /api/mesh_lod 端点

```python
@app.post("/api/mesh_lod")
def build_mesh_lod(req: MeshLODRequest) -> dict[str, Any]:
    """Build mesh with LOD (Level of Detail) support.
    
    quality=1: Low resolution (resolution ~30)
    quality=3: High resolution (resolution ~60)
    """
```

**功能说明：**
- 接收 `{equation, params, view_radius, quality}` 参数
- `quality=1` 返回低模 (resolution ~30)
- `quality=3` 返回高模 (resolution ~60)
- 启用 `lod=True` 优化标志

### 3. 修改 mesh_service.py 分辨率计算

修改 `_calc_resolutions` 函数：

```python
def _calc_resolutions(view_radius: float, quality: int, lod: bool):
    if lod:
        # LOD mode: low quality uses lower resolution
        # quality=1 -> ~30 resolution (low poly)
        # quality=3 -> ~60 resolution (high poly)
        quality = int(max(1, min(3, int(quality))))
        coarse_res = {1: 28, 2: 38, 3: 48}[quality]
        fine_res = {1: 30, 2: 45, 3: 60}[quality]
        max_voxels = {1: 500_000, 2: 800_000, 3: 1_200_000}[quality]
        return coarse_res, fine_res, max_voxels
    # ... 原有逻辑
```

## 前端修改 (webapp_app.js)

### 1. 状态管理扩展

```javascript
const state = {
  // ... 原有状态
  // LOD state management
  lodObjects: new Map(),  // Map<eqId, THREE.LOD>
  lodPending: new Map(),  // Map<eqId, Promise>
  lodEnabled: true,  // Enable LOD by default
};
```

### 2. 新增 fetchMeshLOD 函数

```javascript
async function fetchMeshLOD(eq, quality = 1) {
  const resp = await fetch(`${apiBase()}/api/mesh_lod`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      equation: eq.text,
      params: paramsObject(),
      view_radius: currentRequestViewRadius(),
      quality: quality,  // 1=low, 3=high
    }),
  });
  // ...
}
```

### 3. 新增 createLODObject 函数

```javascript
function createLODObject(eq, lowMesh, highMesh) {
  const THREE = view.THREE;
  if (!THREE || !view.meshGroup) return null;
  
  const lod = new THREE.LOD();
  
  // Create low detail mesh (visible at far distance)
  const lowObj = buildMeshObject(lowMesh, eq.color, true);
  if (lowObj) {
    const baseDistance = state.viewRadius * 2.0;  // Far threshold
    lod.addLevel(lowObj, baseDistance);
  }
  
  // Create high detail mesh (visible at close distance)
  const highObj = buildMeshObject(highMesh, eq.color, false);
  if (highObj) {
    const nearDistance = state.viewRadius * 0.8;  // Near threshold
    lod.addLevel(highObj, nearDistance);
  }
  
  lod.userData.eqId = eq.id;
  return lod;
}
```

### 4. 新增 loadLODForEquation 函数

```javascript
async function loadLODForEquation(eq, seq) {
  // 并行获取低模和高模
  const [lowData, highData] = await Promise.all([
    fetchMeshLOD(eq, 1),  // quality=1 -> low resolution
    fetchMeshLOD(eq, 3),  // quality=3 -> high resolution
  ]);
  
  // 创建 LOD 对象并替换原 mesh
  const lodObj = createLODObject(eq, lowMesh, highMesh);
  // ...
}
```

### 5. 新增 updateAllLODs 函数

```javascript
function updateAllLODs() {
  if (!state.lodEnabled || !view.camera) return;
  
  for (const [eqId, lod] of state.lodObjects.entries()) {
    if (lod && typeof lod.update === 'function') {
      lod.update(view.camera);
    }
  }
}
```

### 6. 修改 animate 循环

```javascript
const animate = () => {
  // ... 原有渲染逻辑
  
  // Update LOD objects based on camera distance
  updateAllLODs();
  
  requestAnimationFrame(animate);
};
```

### 7. 修改 onAllWorkerResultsIn 函数

绘制完成后触发 LOD 加载：

```javascript
function onAllWorkerResultsIn(seq) {
  // ... 原有逻辑
  
  // Load LOD for all equations in background if enabled
  if (state.lodEnabled && state.quality >= 2) {
    setTimeout(() => {
      for (const eq of state.equations) {
        if (!isFrontendExactPlaneEquation(eq)) {
          loadLODForEquation(eq, seq);
        }
      }
    }, 800);
  }
}
```

### 8. 修改 removeMeshById 和 clearAllMeshes

清除 LOD 状态：

```javascript
function removeMeshById(id) {
  // ... 原有清理逻辑
  
  // Also remove LOD object if exists
  const lodObj = state.lodObjects.get(id);
  if (lodObj && view.meshGroup) {
    view.meshGroup.remove(lodObj);
    // ... 清理 geometry 和 material
    state.lodObjects.delete(id);
  }
  state.lodPending.delete(id);
}

function clearAllMeshes() {
  for (const id of [...state.meshById.keys()]) removeMeshById(id);
  state.lodObjects.clear();
  state.lodPending.clear();
}
```

## LOD 距离阈值

根据 `viewRadius` 动态计算：

- **低模 (far)**: `viewRadius * 2.0` — 相机距离超过此值时显示
- **高模 (near)**: `viewRadius * 0.8` — 相机距离低于此值时显示

## 性能优化

1. **渐进式加载**: 先显示快速预览，后台异步加载 LOD
2. **并行获取**: 低模和高模同时请求，减少网络延迟
3. **缓存复用**: 利用现有的 LRU 缓存机制
4. **选择性启用**: 仅在 `quality >= 2` (高清/超清) 时启用 LOD

## API 端点对比

| 端点 | 参数 | 说明 |
|------|------|------|
| `/api/mesh` | equation, params, view_radius, lod, quality | 原有端点，支持渐进式加载 |
| `/api/mesh_lod` | equation, params, view_radius, quality | **新增**，专为 LOD 设计，固定启用 lod=True |

## 兼容性

- 保持原有 `/api/mesh` 端点不变，向后兼容
- LOD 功能通过 `state.lodEnabled` 标志可开关
- 前端精确平面 (frontendExactPlane) 不使用 LOD，保持原有逻辑

## 测试建议

1. 添加复杂曲面方程（如球面、抛物面）
2. 选择"高清"或"超清"画质
3. 点击绘制，观察后台加载 LOD
4. 拉近/拉远相机，观察模型切换
5. 检查控制台日志确认 LOD 加载成功

## 文件修改清单

| 文件 | 修改类型 |
|------|----------|
| `api_server.py` | 新增端点 + 模型 |
| `mesh_service.py` | 修改分辨率计算 |
| `webapp_app.js` | 新增函数 + 修改状态/循环 |

---

*此文档由 AI 自动生成*
