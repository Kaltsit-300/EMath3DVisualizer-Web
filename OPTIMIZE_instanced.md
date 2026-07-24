# InstancedMesh / 共享几何优化总结

日期：2026-07-24
文件：`webapp_app.js`

## 一、场景分析结论

| 场景 | 现状（优化前） | 是否适合实例化 | 采用方案 |
|---|---|---|---|
| 精确平面（`buildExactPlaneObject`） | 每个平面方程创建独立 `PlaneGeometry(width, height)` + 独立 `MeshPhysicalMaterial`，N 个平面 = N 个 geometry + N 次 draw call | ✅ 最佳目标：同为矩形，仅尺寸/朝向/颜色不同 | **InstancedMesh + setColorAt** |
| 地面平面（`_createGroundPlane`） | 每次缩放触发 `updateGroundGrid` 重建时都 new 一个 `PlaneGeometry(size, size)` 并 dispose 旧的 | ✅ 顶点数据可完全复用 | **共享单位 PlaneGeometry + mesh.scale** |
| 坐标轴刻度线（`createAxisGroup`） | 每个刻度一个独立 2 顶点 `THREE.Line`（3 轴 × N 刻度 = 3N 个对象、3N 次 draw call、3N 份 geometry/material） | ⚠️ 可行但非最优 | **按轴合并为 LineSegments**（见下文论证） |
| 通用曲面网格（`buildMeshObject` 主路径） | 每个方程的顶点数据来自后端、彼此不同 | ❌ 顶点数据不相同，无法实例化 | 不改动（front/back 双 mesh 已共享同一 geometry） |
| 坐标轴主线/箭头/数字 Sprite | 各 3 个，数量固定且小 | ❌ 收益可忽略 | 不改动 |

## 二、具体改动

### 1. 共享单位平面几何（`sharedGeometries.unitPlane`）

新增全局共享的 1×1 `PlaneGeometry`（`getSharedUnitPlane()`），生命周期与页面一致，永不 dispose：

- 精确平面实例化的基底几何；
- 地面平面 `_createGroundPlane` 直接复用它，用 `mesh.scale.set(size, size, 1)` 达到目标尺寸——网格重建（缩放视野时高频发生）不再重新分配顶点缓冲。

所有引用共享几何的对象都打上 `userData.sharedGeometry = true` 标记，`removeMeshById` / `disposeSceneObjects` / `updateGroundGrid` 中的 dispose 逻辑均已加守卫，防止误释放共享顶点缓冲。

### 2. 精确平面 → 单个 InstancedMesh（`planeInstances`）

- `buildExactPlaneObject` 不再创建独立 geometry/material/mesh，改为计算实例矩阵：
  `Matrix4.makeBasis(uDir*width, vDir*height, nDir).setPosition(center)`
  —— 宽高烘焙进基向量缩放，1×1 单位平面经矩阵变换即得到任意位置/朝向/尺寸的平面。返回轻量占位 `Group`（携带 `userData.planeMatrix` / `planeColor` / `planeInstance`）。
- `replaceEquationMesh` 检测占位组后调用 `setPlaneInstance(eqId, matrix, color)` 注册；`removeMeshById` 对应调用 `removePlaneInstance`。
- `rebuildPlaneInstances` 把全部条目写入单个 `THREE.InstancedMesh`：
  - `setMatrixAt(i, matrix)` —— 每实例变换；
  - `setColorAt(i, color)` —— 每实例颜色（instanceColor 与白色基底材质相乘）；
  - `instanceMatrix.setUsage(DynamicDrawUsage)`，容量按 1.5× 预留、不足时扩容重建；
  - `frustumCulled = false`（实例位置由矩阵决定，避免整体包围盒误剔除）。
- 效果：**N 个平面方程 → 1 个 geometry + 1 个 material + 1 次 draw call**；平面视野变化重绘（`refreshLocalPlaneMeshesOnViewChange`）只更新实例矩阵缓冲，不再有 geometry 分配/GC 压力。
- 已知取舍：`emissive` 无法逐实例设置，实例化材质去掉了原每平面 0.08 的自发光（不透明度 0.18 下视觉差异可忽略）。

### 3. 与并行 LOD 改动的集成

代码库中并行合入了 `THREE.LOD` 优化（`createLODObject` / `loadLODForEquation`）。已加保护：`geometry_type === "plane"` 的方程直接返回 null 跳过 LOD 包装——平面只有 2 个三角形，本就无需 LOD，且占位组装进 `THREE.LOD` 会导致不渲染。LOD 对象的 dispose 路径同样加了 `sharedGeometry` 守卫。

### 4. 坐标轴刻度线：LineSegments 合并（关于 InstancedBufferGeometry 的结论）

**结论：刻度线不用 InstancedBufferGeometry，用顶点合并（LineSegments）更优。**

论证：
- 每条刻度线只有 2 个顶点（24 字节位置数据）。InstancedBufferGeometry 每实例仍需一个偏移 attribute（≥12 字节）加自定义 shader/onBeforeCompile 维护成本，节省的带宽近乎为零；
- 实例化的收益在于"单实例顶点多、实例数大"，2 顶点线段是反例；
- `LineSegments` 把每轴全部刻度装进一个 BufferGeometry，一样把 draw call 从 3×N 降到 3，且零 shader 定制、零维护成本。

实现：`createAxisGroup` 的刻度循环改为向 `xTickPts/yTickPts/zTickPts` 数组累积顶点，循环后每轴创建一个 `LineSegments`。轴组每次缩放重建时，对象数量从 `3N 线 + 3N Sprite` 降为 `3 LineSegments + 3N Sprite`（数字 Sprite 因每个贴图内容不同无法合并，维持原状；后续可选优化：数字纹理图集 + InstancedMesh quad）。

## 三、性能收益汇总（以 6 个平面方程 + 默认视野为例）

| 指标 | 优化前 | 优化后 |
|---|---|---|
| 平面 geometry 数 | 6 | 1（共享，含地面平面共用同一份顶点） |
| 平面 draw call | 6 | 1 |
| 刻度线 draw call（约 20 档/轴） | ~60 | 3 |
| 缩放重建时的 geometry 分配 | 地面平面 + 每平面 + 每刻度线 | 仅 3 个刻度 LineSegments |

## 四、验证

- `node --check webapp_app.js` 语法通过；
- dispose 路径逐一核对：`removeMeshById` / `disposeSceneObjects` / `updateGroundGrid` / LOD 清理均不会释放共享几何；
- 平面增删（`removeEquation` / `clearAll`）通过 `removePlaneInstance` 同步收缩实例计数（`im.count`）。

## 五、后续可选优化（未实施）

1. 数字刻度 Sprite：改为字形纹理图集 + 单个 InstancedMesh quad（当前每个数字一个 CanvasTexture + Sprite，是轴组重建时的最大对象来源）。
2. 交线（intersection polylines）：多段线顶点数不同，不适合实例化，但可合并为单个 LineSegments（材质完全相同）。
3. 若未来出现"同一方程族多参数副本"（如 z=x²+y²+c 的 c 扫描），通用曲面也可上 InstancedMesh。
