const state = {
  equations: [],
  meshById: new Map(),
  intersectionObjects: [],
  params: new Map(),
  drawSeq: 0,
  drawTimer: null,
  highQualityTimer: null,
  intersectionRefreshTimer: null,
  quality: 1,  // 默认标准画质，首屏流畅
  viewRadius: 10,  // 减小默认视野范围
  hasDrawn: false,
  settings: {
    showGrid: true,
    showPlane: true,
    showLegend: true,
  },
  userInteracted: false,
  autoCenterFrames: 30,
  mathJaxReady: false,
  mathJaxLoading: false,
  axisTickInterval: null,
  axisVisibleLength: null,
  labelCache: new Map(),
  // LOD state management
  lodObjects: new Map(),  // Map<eqId, THREE.LOD>
  lodPending: new Map(),  // Map<eqId, Promise>
  lodEnabled: true,  // Enable LOD by default
  // Web Worker state
  meshWorker: null,
  workerDrawSeq: 0,       // drawSeq snapshot for in-flight worker results
  workerPending: 0,        // how many worker results we are still waiting for
  workerFallback: false,   // true if Worker is not supported / failed
};

const DEFAULT_VIEW_RADIUS = 10;
const MIN_VIEW_RADIUS = 0.05;
const MAX_VIEW_RADIUS = 160;

const palette = ["#2d7ef7", "#22c55e", "#ef4444", "#a855f7", "#f59e0b", "#06b6d4"];

const viewerEl = document.getElementById("viewer");
const eqInput = document.getElementById("eqInput");
const addBtn = document.getElementById("addBtn");
const drawBtn = document.getElementById("drawBtn");
const clearBtn = document.getElementById("clearBtn");
const eqList = document.getElementById("eqList");
const paramList = document.getElementById("paramList");
const legendEl = document.getElementById("legend");
const statusEl = document.getElementById("status");
const qualitySel = document.getElementById("qualitySel");
const zoomInBtn = document.getElementById("zoomInBtn");
const zoomOutBtn = document.getElementById("zoomOutBtn");
const resetViewBtn = document.getElementById("resetViewBtn");
const resetViewBtnTop = document.getElementById("resetViewBtnTop");
const inputPad = document.getElementById("inputPad");
const closeInputPadBtn = document.getElementById("closeInputPadBtn");
const miniAxisEl = document.getElementById("miniAxis");
const navToggleBtn = document.getElementById("navToggleBtn");
const toggleGrid = document.getElementById("toggleGrid");
const togglePlane = document.getElementById("togglePlane");
const toggleLegend = document.getElementById("toggleLegend");
const toggleKeyboardBtn = document.getElementById("toggleKeyboardBtn");


const view = {
  THREE: null,
  scene: null,
  camera: null,
  renderer: null,
  controls: null,
  meshGroup: null,
  intersectionGroup: null,
  gridHelper: null,
  groundPlane: null,
  axisGroup: null,
  miniRenderer: null,
  miniScene: null,
  miniCamera: null,
  miniAxisGroup: null,
  axisMetaKey: "",
  gridMetaKey: "",
  resize: null,
  started: false,
  zoomDirty: false,
};

function rendererPixelRatioCap() {
  if (state.quality >= 3) return Math.min(window.devicePixelRatio || 1, 1.5);
  if (state.quality === 2) return Math.min(window.devicePixelRatio || 1, 1.25);
  return 1.0;
}

function setStatus(text, type = "idle") {
  const dot = statusEl.querySelector(".status-pip");
  const txt = statusEl.querySelector(".status-text");
  if (txt) txt.textContent = text;
  if (dot) {
    dot.classList.remove("working", "error");
    if (type === "working") dot.classList.add("working");
    if (type === "error") dot.classList.add("error");
  }
}

function esc(text) {
  return String(text).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function uid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return `eq-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
}

function parseLinearSide(side) {
  const normalized = String(side || "")
    .replace(/\s+/g, "")
    .replace(/−/g, "-")
    .replace(/(\d)([xyz])/gi, "$1*$2");
  if (!normalized) return { x: 0, y: 0, z: 0, c: 0 };
  const terms = normalized.match(/[+\-]?[^+\-]+/g) || [];
  const out = { x: 0, y: 0, z: 0, c: 0 };

  for (const rawTerm of terms) {
    const term = rawTerm.trim();
    if (!term) continue;
    if (/[()/^]/.test(term)) return null;
    if (/(sqrt|sin|cos|tan|log|ln|exp|abs)/i.test(term)) return null;

    const vars = [...term.matchAll(/[xyz]/gi)].map((m) => m[0].toLowerCase());
    if (vars.length > 1) return null;
    if (vars.length === 1) {
      const varName = vars[0];
      const coeffText = term.replaceAll("*", "").replace(varName, "");
      let coeff = 1;
      if (coeffText === "" || coeffText === "+") coeff = 1;
      else if (coeffText === "-") coeff = -1;
      else coeff = Number(coeffText);
      if (!Number.isFinite(coeff)) return null;
      out[varName] += coeff;
      continue;
    }

    const constant = Number(term);
    if (!Number.isFinite(constant)) return null;
    out.c += constant;
  }

  return out;
}

function extractLinearPlaneCoefficients(eqText) {
  const normalized = String(eqText || "").replace(/\s+/g, "").replace(/−/g, "-");
  if (!normalized.includes("=")) return null;
  const parts = normalized.split("=");
  if (parts.length !== 2) return null;
  const left = parseLinearSide(parts[0]);
  const right = parseLinearSide(parts[1]);
  if (!left || !right) return null;

  const coeffs = {
    a: left.x - right.x,
    b: left.y - right.y,
    c: left.z - right.z,
    d: left.c - right.c,
  };
  const norm = Math.abs(coeffs.a) + Math.abs(coeffs.b) + Math.abs(coeffs.c);
  return norm > 1e-9 ? coeffs : null;
}

function buildPlaneDisplayQuadFromCoefficients(coeffs, viewRadius) {
  if (!coeffs) return null;
  const a = Number(coeffs.a);
  const b = Number(coeffs.b);
  const c = Number(coeffs.c);
  const d = Number(coeffs.d);
  if (![a, b, c, d].every(Number.isFinite)) return null;

  const normal = [a, b, c];
  const norm = Math.hypot(a, b, c);
  if (!(norm > 1e-9)) return null;
  const n = normal.map((v) => v / norm);
  const denom = a * a + b * b + c * c;
  if (!(denom > 1e-9)) return null;

  const center = [(-d * a) / denom, (-d * b) / denom, (-d * c) / denom];
  let ref = [0, 0, 1];
  const dot = n[0] * ref[0] + n[1] * ref[1] + n[2] * ref[2];
  if (Math.abs(dot) > 0.95) ref = [1, 0, 0];

  const u = [
    n[1] * ref[2] - n[2] * ref[1],
    n[2] * ref[0] - n[0] * ref[2],
    n[0] * ref[1] - n[1] * ref[0],
  ];
  const uNorm = Math.hypot(...u);
  if (!(uNorm > 1e-9)) return null;
  const uu = u.map((v) => v / uNorm);

  const v = [
    n[1] * uu[2] - n[2] * uu[1],
    n[2] * uu[0] - n[0] * uu[2],
    n[0] * uu[1] - n[1] * uu[0],
  ];
  const vNorm = Math.hypot(...v);
  if (!(vNorm > 1e-9)) return null;
  const vv = v.map((val) => val / vNorm);

  const half = Math.max(2.5, Number(viewRadius || state.viewRadius || 10) * 1.2);
  return [
    [center[0] - half * uu[0] - half * vv[0], center[1] - half * uu[1] - half * vv[1], center[2] - half * uu[2] - half * vv[2]],
    [center[0] + half * uu[0] - half * vv[0], center[1] + half * uu[1] - half * vv[1], center[2] + half * uu[2] - half * vv[2]],
    [center[0] - half * uu[0] + half * vv[0], center[1] - half * uu[1] + half * vv[1], center[2] - half * uu[2] + half * vv[2]],
    [center[0] + half * uu[0] + half * vv[0], center[1] + half * uu[1] + half * vv[1], center[2] + half * uu[2] + half * vv[2]],
  ];
}

function forceExactPlaneMeta(mesh, eqText) {
  if (!mesh) return mesh;
  const coeffs = extractLinearPlaneCoefficients(eqText);
  if (!coeffs) return mesh;
  const quad = buildPlaneDisplayQuadFromCoefficients(coeffs, state.viewRadius);
  if (!quad) return mesh;
  mesh.meta = {
    ...(mesh.meta || {}),
    geometry_type: "plane",
    display_quad: quad,
    frontend_plane: true,
  };
  return mesh;
}

function buildLocalExactPlaneMeshData(eq) {
  const coeffs = eq?.exactPlaneCoeffs || extractLinearPlaneCoefficients(eq?.text || "");
  if (!coeffs) return null;
  const quad = buildPlaneDisplayQuadFromCoefficients(coeffs, state.viewRadius);
  if (!quad) return null;
  return {
    vertices: [],
    faces: [],
    meta: {
      geometry_type: "plane",
      display_quad: quad,
      frontend_plane: true,
      local_only: true,
    },
  };
}

function meshHasRenderableGeometry(mesh) {
  if (!mesh) return false;
  if (mesh.meta?.geometry_type === "plane" && Array.isArray(mesh.meta?.display_quad) && mesh.meta.display_quad.length === 4) {
    return true;
  }
  return !!(mesh.vertices?.length && mesh.faces?.length);
}

function equationStillExists(eqId) {
  return state.equations.some((eq) => eq.id === eqId);
}

function replaceEquationMesh(eq, mesh) {
  removeMeshById(eq.id);
  const obj = buildMeshObject(mesh, eq.color);
  if (!obj || !view.meshGroup) return false;
  obj.userData.eqId = eq.id;
  obj.userData.geometryType = mesh.meta?.geometry_type || "";
  obj.userData.localOnly = !!mesh.meta?.local_only;
  view.meshGroup.add(obj);
  state.meshById.set(eq.id, obj);
  // 若为精确平面占位对象，注册到共享 InstancedMesh（setColorAt 设置每实例颜色）
  if (obj.userData.planeInstance) {
    setPlaneInstance(eq.id, obj.userData.planeMatrix, obj.userData.planeColor);
  }
  return true;
}

function createLODObject(eq, lowMesh, highMesh) {
  const THREE = view.THREE;
  if (!THREE || !view.meshGroup) return null;
  
  // 精确平面走共享 InstancedMesh 路径（2 个三角形，无需 LOD），
  // buildMeshObject 会返回 planeInstance 占位组，不能装进 THREE.LOD。
  const lodGeomType = lowMesh?.meta?.geometry_type || highMesh?.meta?.geometry_type || "";
  if (lodGeomType === "plane") return null;

  const lod = new THREE.LOD();
  
  // Create low detail mesh (visible at far distance)
  const lowObj = buildMeshObject(lowMesh, eq.color, true);
  if (lowObj) {
    // Calculate LOD distances based on view radius
    const baseDistance = state.viewRadius * 2.0;  // Far threshold
    lod.addLevel(lowObj, baseDistance);
  }
  
  // Create high detail mesh (visible at close distance)
  const highObj = buildMeshObject(highMesh, eq.color, false);
  if (highObj) {
    // High detail visible when camera is close
    const nearDistance = state.viewRadius * 0.8;  // Near threshold
    lod.addLevel(highObj, nearDistance);
  }
  
  lod.userData.eqId = eq.id;
  lod.userData.geometryType = lowMesh.meta?.geometry_type || highMesh.meta?.geometry_type || "";
  
  return lod;
}

async function loadLODForEquation(eq, seq) {
  if (!state.lodEnabled || isFrontendExactPlaneEquation(eq)) return false;
  
  // Check if already pending
  if (state.lodPending.has(eq.id)) return false;
  
  const THREE = view.THREE;
  if (!THREE) return false;
  
  try {
    // Fetch both low and high quality meshes in parallel
    const [lowData, highData] = await Promise.all([
      fetchMeshLOD(eq, 1),  // quality=1 -> low resolution
      fetchMeshLOD(eq, 3),  // quality=3 -> high resolution
    ]);
    
    if (seq !== state.drawSeq) return false;
    if (!equationStillExists(eq.id)) return false;
    
    const lowMesh = forceExactPlaneMeta(lowData.mesh, eq.text);
    const highMesh = forceExactPlaneMeta(highData.mesh, eq.text);
    
    if (!meshHasRenderableGeometry(lowMesh) || !meshHasRenderableGeometry(highMesh)) {
      return false;
    }
    
    // Create LOD object
    const lodObj = createLODObject(eq, lowMesh, highMesh);
    if (!lodObj) return false;
    
    // Remove old mesh and add LOD object
    removeMeshById(eq.id);
    view.meshGroup.add(lodObj);
    state.meshById.set(eq.id, lodObj);
    state.lodObjects.set(eq.id, lodObj);
    
    return true;
  } catch (err) {
    console.warn(`LOD load failed for ${eq.text}:`, err);
    return false;
  }
}

function updateAllLODs() {
  if (!state.lodEnabled || !view.camera) return;
  
  for (const [eqId, lod] of state.lodObjects.entries()) {
    if (lod && typeof lod.update === 'function') {
      lod.update(view.camera);
    }
  }
}

function disposeSceneObjects(objects, parent) {
  for (const obj of objects || []) {
    if (parent && obj?.parent === parent) parent.remove(obj);
    obj?.traverse?.((child) => {
      if (child.geometry && !child.userData?.sharedGeometry) child.geometry.dispose();
      if (child.material) child.material.dispose();
    });
  }
}

// ---------------------------------------------------------------------------
// Shared geometry & InstancedMesh optimization
// 相同几何类型复用顶点数据：所有精确平面共用一个 1x1 单位 PlaneGeometry，
// 通过 InstancedMesh 的每实例矩阵（旋转+平移+缩放）和 setColorAt 区分。
// ---------------------------------------------------------------------------
const sharedGeometries = {
  unitPlane: null, // 1x1 PlaneGeometry，被地面平面 + 所有精确平面实例共享
};

function getSharedUnitPlane() {
  const THREE = view.THREE;
  if (!sharedGeometries.unitPlane) {
    sharedGeometries.unitPlane = new THREE.PlaneGeometry(1, 1, 1, 1);
  }
  return sharedGeometries.unitPlane;
}

const planeInstances = {
  mesh: null,      // 单个 THREE.InstancedMesh，承载所有精确平面
  capacity: 0,
  entries: new Map(), // eqId -> { matrix: THREE.Matrix4, color: THREE.Color }
};

function _createPlaneInstancedMaterial() {
  const THREE = view.THREE;
  // 基色为白色，实际颜色由 setColorAt 提供（instanceColor 与 material.color 相乘）。
  // 注意：emissive 无法逐实例设置，因此实例化平面不带自发光（视觉差异极小）。
  return new THREE.MeshPhysicalMaterial({
    color: 0xffffff,
    transparent: true,
    opacity: 0.18,
    side: THREE.DoubleSide,
    roughness: 0.45,
    metalness: 0.35,
    clearcoat: 0.45,
    depthWrite: false,
    depthTest: true,
    blending: THREE.NormalBlending,
    polygonOffset: true,
    polygonOffsetFactor: -1,
    polygonOffsetUnits: -2,
  });
}

function ensurePlaneInstanceCapacity(count) {
  const THREE = view.THREE;
  if (planeInstances.mesh && planeInstances.capacity >= count) return planeInstances.mesh;
  const old = planeInstances.mesh;
  if (old) {
    old.parent?.remove(old);
    old.material?.dispose?.();
    old.dispose?.(); // 仅释放实例属性缓冲；共享 geometry 不 dispose
  }
  const capacity = Math.max(4, Math.ceil(count * 1.5));
  const im = new THREE.InstancedMesh(getSharedUnitPlane(), _createPlaneInstancedMaterial(), capacity);
  im.count = 0;
  im.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  im.renderOrder = 10;
  im.frustumCulled = false; // 实例位置由矩阵决定，跳过过期包围盒剔除
  im.userData.sharedGeometry = true;
  planeInstances.mesh = im;
  planeInstances.capacity = capacity;
  if (view.meshGroup) view.meshGroup.add(im);
  return im;
}

function rebuildPlaneInstances() {
  if (!view.THREE) return;
  const count = planeInstances.entries.size;
  if (!count) {
    if (planeInstances.mesh) planeInstances.mesh.count = 0;
    return;
  }
  const im = ensurePlaneInstanceCapacity(count);
  if (view.meshGroup && im.parent !== view.meshGroup) view.meshGroup.add(im);
  let i = 0;
  for (const entry of planeInstances.entries.values()) {
    im.setMatrixAt(i, entry.matrix);
    im.setColorAt(i, entry.color);
    i += 1;
  }
  im.count = i;
  im.instanceMatrix.needsUpdate = true;
  if (im.instanceColor) im.instanceColor.needsUpdate = true;
}

function setPlaneInstance(eqId, matrix, color) {
  planeInstances.entries.set(eqId, { matrix, color });
  rebuildPlaneInstances();
}

function removePlaneInstance(eqId) {
  if (planeInstances.entries.delete(eqId)) rebuildPlaneInstances();
}

function replaceIntersectionObjects(objects) {
  const nextObjects = Array.isArray(objects) ? objects : [];
  if (!view.intersectionGroup) {
    state.intersectionObjects = nextObjects;
    return;
  }
  for (const obj of nextObjects) {
    if (obj && obj.parent !== view.intersectionGroup) view.intersectionGroup.add(obj);
  }
  disposeSceneObjects(state.intersectionObjects, view.intersectionGroup);
  state.intersectionObjects = nextObjects;
}

function scheduleIntersectionRefresh(delay = 80, opts = {}) {
  if (!state.hasDrawn) return;
  if (state.equations.length < 2) {
    clearIntersections();
    return;
  }
  clearTimeout(state.intersectionRefreshTimer);
  state.intersectionRefreshTimer = setTimeout(async () => {
    state.intersectionRefreshTimer = null;
    const seq = ++state.drawSeq;
    try {
      await loadIntersectionCurves(seq, {
        lod: !!opts.lod,
        quality: Number(opts.quality ?? state.quality) || state.quality,
      });
    } catch (err) {
      if (seq === state.drawSeq) console.warn("交线刷新失败:", err);
    }
  }, Math.max(0, delay));
}

function refreshLocalPlaneMeshesOnViewChange() {
  if (!state.hasDrawn || !state.equations.length) return false;
  if (!state.equations.every((eq) => isFrontendExactPlaneEquation(eq))) return false;

  let updated = 0;
  for (const eq of state.equations) {
    const mesh = buildLocalExactPlaneMeshData(eq);
    if (!mesh) continue;
    if (replaceEquationMesh(eq, mesh)) updated += 1;
  }
  return updated > 0;
}

function apiBase() {
  return window.location.origin;
}

function paramsObject() {
  const out = {};
  for (const [k, v] of state.params.entries()) out[k] = v.value;
  return out;
}

// ---------- Web Worker for mesh fetching ----------

function currentApiPort() {
  try {
    const url = new URL(window.location.href);
    return Number(url.port) || 8000;
  } catch {
    return 8000;
  }
}

function initMeshWorker() {
  if (state.meshWorker || state.workerFallback) return;
  try {
    const worker = new Worker("webapp_mesh_worker.js");
    worker.onmessage = onWorkerMessage;
    worker.onerror = (err) => {
      console.warn("[Worker] mesh worker error, falling back to main-thread fetch:", err);
      worker.terminate();
      state.meshWorker = null;
      state.workerFallback = true;
    };
    state.meshWorker = worker;
    // pass current port so the worker can skip port probing
    state.meshWorkerPort = currentApiPort();
    console.log("[Worker] mesh worker initialised on port", state.meshWorkerPort);
  } catch (err) {
    console.warn("[Worker] Web Worker not supported, using main-thread fetch:", err);
    state.workerFallback = true;
  }
}

function onWorkerMessage(evt) {
  const msg = evt.data;

  // --- individual mesh result (may arrive out-of-order) ---
  if (msg.type === "meshResult") {
    const { id, data, error } = msg;
    if (id === undefined) return;
    // Ignore results from a stale draw sequence
    if (msg.workerSeq !== state.workerDrawSeq) return;
    if (error || !data) {
      console.warn(`[Worker] mesh result error for ${id}:`, error);
    } else {
      applyWorkerMeshResult(id, data);
    }
    state.workerPending = Math.max(0, state.workerPending - 1);
    if (state.workerPending === 0) onAllWorkerResultsIn(state.workerDrawSeq);
    return;
  }

  // --- batch fetch complete signal ---
  if (msg.type === "fetchAllDone") {
    // We track completion via the pending counter instead.
    return;
  }

  // --- unexpected message ---
  if (msg.type === "error") {
    console.error("[Worker] unexpected error:", msg.message);
  }
}

/** Apply one mesh result received from the worker (out-of-order safe). */
function applyWorkerMeshResult(eqId, data) {
  const eq = state.equations.find((e) => e.id === eqId);
  if (!eq || !equationStillExists(eqId)) return;
  const mesh = forceExactPlaneMeta(data.mesh || {}, eq.text);
  if (!meshHasRenderableGeometry(mesh)) return;
  if (replaceEquationMesh(eq, mesh)) {
    const obj = state.meshById.get(eqId);
    if (obj) obj.userData.isPreview = true;
  }
}

/** Called when all worker results for a drawSeq have arrived. */
function onAllWorkerResultsIn(seq) {
  if (seq !== state.drawSeq) return;
  for (const eq of state.equations) {
    if (isFrontendExactPlaneEquation(eq)) continue;
    const obj = state.meshById.get(eq.id);
    if (obj) obj.userData.isPreview = false;
  }
  
  // Load LOD for all equations in background if enabled
  if (state.lodEnabled && state.quality >= 2) {
    setTimeout(() => {
      if (seq !== state.drawSeq) return;
      for (const eq of state.equations) {
        if (!isFrontendExactPlaneEquation(eq)) {
          loadLODForEquation(eq, seq);
        }
      }
    }, 800);
  } else if (state.quality >= 2) {
    // Fallback to high-quality version if LOD disabled
    clearTimeout(state.highQualityTimer);
    state.highQualityTimer = setTimeout(() => {
      if (seq !== state.drawSeq) return;
      loadHighQualityVersion(seq);
    }, 500);
  } else {
    setStatus(`绘制完成 (${state.equations.length}/${state.equations.length})`);
  }
}

/**
 * Send mesh fetch tasks to the Web Worker.
 * Falls back to main-thread sequential fetch if Worker is unavailable.
 */
function scheduleWorkerMeshFetch(equations, seq, opts) {
  const { lod, quality, fetchParams } = opts;

  if (state.workerFallback || !state.meshWorker) {
    scheduleFallbackMeshFetch(equations, seq, opts);
    return;
  }

  state.workerDrawSeq = seq;
  state.workerPending = equations.length;

  const tasks = equations.map((eq) => ({
    id: eq.id,
    equation: eq.text,
    params: fetchParams,
    port: state.meshWorkerPort,
    view_radius: currentRequestViewRadius(),
    lod: !!lod,
    quality: Number(quality) || 1,
  }));

  state.meshWorker.postMessage({ type: "fetchAll", tasks });
}

/** Main-thread fallback when Web Worker is not available. */
async function scheduleFallbackMeshFetch(equations, seq, opts) {
  const { lod, quality, fetchParams } = opts;
  for (const eq of equations) {
    if (seq !== state.drawSeq) return;
    try {
      const resp = await fetch(`${apiBase()}/api/mesh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          equation: eq.text,
          params: fetchParams,
          view_radius: currentRequestViewRadius(),
          lod: !!lod,
          quality: Number(quality) || 1,
        }),
      });
      if (!resp.ok) continue;
      const data = await resp.json();
      if (seq !== state.drawSeq) return;
      if (!equationStillExists(eq.id)) continue;
      const mesh = forceExactPlaneMeta(data.mesh || {}, eq.text);
      if (!meshHasRenderableGeometry(mesh)) continue;
      if (replaceEquationMesh(eq, mesh)) {
        const obj = state.meshById.get(eq.id);
        if (obj) obj.userData.isPreview = true;
      }
    } catch (err) {
      console.warn("[Fallback] mesh fetch error:", err);
    }
  }
  if (seq !== state.drawSeq) return;
  for (const eq of equations) {
    const obj = state.meshById.get(eq.id);
    if (obj) obj.userData.isPreview = false;
  }
  if (seq !== state.drawSeq) return;
  if (state.quality >= 2) {
    clearTimeout(state.highQualityTimer);
    state.highQualityTimer = setTimeout(() => {
      if (seq !== state.drawSeq) return;
      loadHighQualityVersion(seq);
    }, 500);
  } else {
    setStatus(`绘制完成 (${equations.length}/${equations.length})`);
  }
}

/**
 * Send HIGH-QUALITY mesh fetch tasks to the Web Worker.
 * Falls back to main-thread sequential fetch if Worker is unavailable.
 */
function scheduleWorkerHighQualityFetch(equations, seq) {
  if (state.workerFallback || !state.meshWorker) {
    scheduleFallbackHighQualityFetch(equations, seq);
    return;
  }

  state.workerDrawSeq = seq;
  state.workerPending = equations.length;

  const tasks = equations.map((eq) => ({
    id: eq.id,
    equation: eq.text,
    params: paramsObject(),
    port: state.meshWorkerPort,
    view_radius: currentRequestViewRadius(),
    lod: false,
    quality: state.quality,
  }));

  state.meshWorker.postMessage({ type: "fetchAll", tasks });
}

/** Main-thread fallback for high-quality fetch. */
async function scheduleFallbackHighQualityFetch(equations, seq) {
  for (const eq of equations) {
    if (seq !== state.drawSeq) return;
    if (isFrontendExactPlaneEquation(eq)) continue;
    try {
      const resp = await fetch(`${apiBase()}/api/mesh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          equation: eq.text,
          params: paramsObject(),
          view_radius: currentRequestViewRadius(),
          lod: false,
          quality: state.quality,
        }),
      });
      if (!resp.ok) continue;
      const data = await resp.json();
      if (seq !== state.drawSeq) return;
      if (!equationStillExists(eq.id)) continue;
      const mesh = forceExactPlaneMeta(data.mesh || {}, eq.text);
      if (!meshHasRenderableGeometry(mesh)) continue;
      if (replaceEquationMesh(eq, mesh)) {
        const obj = state.meshById.get(eq.id);
        if (obj) obj.userData.isPreview = false;
      }
    } catch (err) {
      console.warn("[Fallback] high-quality mesh fetch error:", err);
    }
  }
  if (seq !== state.drawSeq) return;
  setStatus(`高质量优化完成 (${equations.length}/${equations.length})`);
}

function currentRequestViewRadius() {
  return Math.max(0.25, Number(state.viewRadius) || DEFAULT_VIEW_RADIUS);
}

function scheduleDraw(delay = 80) {  // 减少绘制延迟，提升响应速度
  if (!state.hasDrawn) return;
  clearTimeout(state.drawTimer);
  state.drawTimer = setTimeout(() => {
    drawAll("auto");
  }, delay);
}

function cancelPendingAsyncDraws() {
  state.drawSeq += 1;
  clearTimeout(state.drawTimer);
  clearTimeout(state.highQualityTimer);
  clearTimeout(state.intersectionRefreshTimer);
  state.highQualityTimer = null;
  state.intersectionRefreshTimer = null;
}

function isFrontendExactPlaneEquation(eq) {
  return !!eq?.frontendExactPlane;
}

function makeAxisLabelSprite(text, colorHex, THREE, fontSize = 36, scaleX = 0.9, scaleY = 0.45, glow = false) {
  const c = document.createElement("canvas");
  c.width = 320;
  c.height = 160;
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.font = `700 ${fontSize}px "Segoe UI", Arial, sans-serif`;
  if (glow) {
    ctx.shadowColor = colorHex;
    ctx.shadowBlur = Math.max(8, fontSize * 0.35);
  }
  ctx.fillStyle = colorHex;
  ctx.strokeStyle = "rgba(0,0,0,0.85)";
  ctx.lineWidth = Math.max(3, Math.round(fontSize * 0.2));
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.strokeText(text, c.width / 2, c.height / 2);
  ctx.fillText(text, c.width / 2, c.height / 2);
  const tex = new THREE.CanvasTexture(c);
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.generateMipmaps = false;
  tex.needsUpdate = true;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
  const s = new THREE.Sprite(mat);
  s.scale.set(scaleX, scaleY, 1);
  return s;
}

function createAxisGroup(THREE, length = 10, opts = {}) {
  const group = new THREE.Group();
  const xLength = opts.xLength ?? length;
  const yLength = opts.yLength ?? length;
  const zLength = opts.zLength ?? length;
  const lineOpacity = opts.lineOpacity ?? 1.0;
  const arrowHeadLength = opts.arrowHeadLength ?? 0.6;
  const arrowHeadWidth = opts.arrowHeadWidth ?? 0.34;
  const labelOffset = opts.labelOffset ?? 0.8;
  const labelScale = opts.labelScale ?? 0.9;
  const labelFontSize = opts.labelFontSize ?? 36;
  const showNumbers = opts.showNumbers ?? true;
  const tickInterval = opts.tickInterval ?? 1.0;
  const numberScale = opts.numberScale ?? (labelScale * 0.8);
  const numberFontSize = opts.numberFontSize ?? Math.floor(labelFontSize * 0.8);
  const numberDistance = opts.numberDistance ?? 0.3;
  const decimals = opts.decimals ?? 0;
  const visibleLength = opts.visibleLength ?? length;

  const makeLine = (p1, p2, color, opacity = 1) => {
    const g = new THREE.BufferGeometry().setFromPoints([p1, p2]);
    const m = new THREE.LineBasicMaterial({ color, transparent: true, opacity, linewidth: 1 });
    return new THREE.Line(g, m);
  };

  const o = new THREE.Vector3(0, 0, 0);
  const xPos = new THREE.Vector3(xLength, 0, 0);
  const yPos = new THREE.Vector3(0, yLength, 0);
  const zPos = new THREE.Vector3(0, 0, zLength);
  const xNeg = new THREE.Vector3(-xLength, 0, 0);
  const yNeg = new THREE.Vector3(0, -yLength, 0);
  const zNeg = new THREE.Vector3(0, 0, -zLength);

  group.add(makeLine(xNeg, xPos, 0xe53935, lineOpacity));
  group.add(makeLine(yNeg, yPos, 0x22a945, lineOpacity));
  group.add(makeLine(zNeg, zPos, 0x2a54f5, lineOpacity));

  group.add(new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), o, xLength, 0xe53935, arrowHeadLength, arrowHeadWidth));
  group.add(new THREE.ArrowHelper(new THREE.Vector3(0, 1, 0), o, yLength, 0x22a945, arrowHeadLength, arrowHeadWidth));
  group.add(new THREE.ArrowHelper(new THREE.Vector3(0, 0, 1), o, zLength, 0x2a54f5, arrowHeadLength, arrowHeadWidth));

  const lx = makeAxisLabelSprite("x", "#e53935", THREE, labelFontSize, labelScale, labelScale * 0.5);
  lx.position.set(xLength + labelOffset, 0, 0);
  group.add(lx);

  const ly = makeAxisLabelSprite("y", "#22a945", THREE, labelFontSize, labelScale, labelScale * 0.5);
  ly.position.set(0, yLength + labelOffset, 0);
  group.add(ly);

  const lz = makeAxisLabelSprite("z", "#2a54f5", THREE, labelFontSize, labelScale, labelScale * 0.5);
  lz.position.set(0, 0, zLength + labelOffset);
  group.add(lz);

  const tickSize = opts.tickSize ?? Math.max(0.08, Math.min(0.35, tickInterval * 0.24));

  const formatNumber = (value) => {
    if (decimals <= 0) return String(Math.round(value));
    const txt = value.toFixed(decimals);
    return txt.replace(/\.?0+$/, "");
  };

  if (showNumbers) {
    const numberColor = "#e8ecf3";
    const nTicks = Math.floor((visibleLength + 1e-9) / tickInterval);
    // 刻度线段合并优化：每轴用一个 LineSegments（单 BufferGeometry 承载全部刻度顶点），
    // 代替每个刻度一个独立 Line 对象，将 3×N 次 draw call 降为 3 次。
    // （对 2 顶点线段而言，顶点合并比 InstancedBufferGeometry 更简单且收益等价）
    const xTickPts = [];
    const yTickPts = [];
    const zTickPts = [];
    for (let k = -nTicks; k <= nTicks; k += 1) {
      const i = k * tickInterval;
      if (Math.abs(i) > visibleLength + tickInterval * 0.05) continue;
      if (Math.abs(i) < tickInterval * 0.2) continue;
      const numberText = formatNumber(i);

      // X轴刻度
      xTickPts.push(i, -tickSize / 2, 0, i, tickSize / 2, 0);
      const xNum = makeAxisLabelSprite(numberText, numberColor, THREE, numberFontSize, numberScale, numberScale * 0.5);
      xNum.position.set(i, -numberDistance, 0);
      group.add(xNum);

      // Y轴刻度
      yTickPts.push(-tickSize / 2, i, 0, tickSize / 2, i, 0);
      const yNum = makeAxisLabelSprite(numberText, numberColor, THREE, numberFontSize, numberScale, numberScale * 0.5);
      yNum.position.set(-numberDistance, i, 0);
      group.add(yNum);

      // Z轴刻度
      zTickPts.push(0, -tickSize / 2, i, 0, tickSize / 2, i);
      const zNum = makeAxisLabelSprite(numberText, numberColor, THREE, numberFontSize, numberScale, numberScale * 0.5);
      zNum.position.set(0, -numberDistance, i);
      group.add(zNum);
    }

    const addTickSegments = (pts, color) => {
      if (!pts.length) return;
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(pts), 3));
      const m = new THREE.LineBasicMaterial({ color, transparent: true, opacity: lineOpacity * 0.7 });
      group.add(new THREE.LineSegments(g, m));
    };
    addTickSegments(xTickPts, 0xe53935);
    addTickSegments(yTickPts, 0x22a945);
    addTickSegments(zTickPts, 0x2a54f5);
  }

  return group;
}

function niceStep(target) {
  const v = Math.max(1e-6, Number(target) || 1);
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / p;
  let base = 1;
  if (n <= 1) base = 1;
  else if (n <= 2) base = 2;
  else if (n <= 2.5) base = 2.5;
  else if (n <= 5) base = 5;
  else base = 10;
  return base * p;
}

function buildTickStepCandidates() {
  const multipliers = [1, 1.25, 2, 2.5, 5, 10];
  const out = [];
  for (let power = -6; power <= 6; power += 1) {
    const scale = Math.pow(10, power);
    for (const mult of multipliers) out.push(mult * scale);
  }
  return out.sort((a, b) => a - b);
}

const TICK_STEP_CANDIDATES = buildTickStepCandidates();

function nearestTickStep(target) {
  const t = Math.max(1e-6, Number(target) || 1);
  let best = TICK_STEP_CANDIDATES[0];
  let bestScore = Infinity;
  for (const step of TICK_STEP_CANDIDATES) {
    const score = Math.abs(Math.log(step / t));
    if (score < bestScore) {
      best = step;
      bestScore = score;
    }
  }
  return best;
}

function nextLargerTickStep(step) {
  const base = Math.max(1e-6, Number(step) || 1);
  for (const candidate of TICK_STEP_CANDIDATES) {
    if (candidate > base * 1.0001) return candidate;
  }
  return base;
}

function nextSmallerTickStep(step) {
  const base = Math.max(1e-6, Number(step) || 1);
  for (let i = TICK_STEP_CANDIDATES.length - 1; i >= 0; i -= 1) {
    const candidate = TICK_STEP_CANDIDATES[i];
    if (candidate < base / 1.0001) return candidate;
  }
  return base;
}

function clampViewRadius(v) {
  const num = Number(v);
  if (!Number.isFinite(num)) return state.viewRadius;
  return Math.max(MIN_VIEW_RADIUS, Math.min(MAX_VIEW_RADIUS, num));
}

function cameraDistanceToViewRadius(distance) {
  const fovDeg = Number(view.camera?.fov || 47);
  const vFov = (fovDeg * Math.PI) / 180.0;
  const halfVisible = Math.tan(vFov * 0.5) * Math.max(0.1, Number(distance) || 0.1);
  return clampViewRadius(halfVisible);
}

function viewRadiusToCameraDistance(radius) {
  const fovDeg = Number(view.camera?.fov || 47);
  const vFov = (fovDeg * Math.PI) / 180.0;
  const denom = Math.max(1e-6, Math.tan(vFov * 0.5));
  return clampViewRadius(radius) / denom;
}

function syncViewRadiusFromCamera(redraw = false) {
  if (!view.camera || !view.controls) return false;
  const distance = view.camera.position.distanceTo(view.controls.target);
  const nextRadius = cameraDistanceToViewRadius(distance);
  const prevRadius = state.viewRadius;
  const changed = Math.abs(nextRadius - prevRadius) > Math.max(0.04, prevRadius * 0.01);
  state.viewRadius = nextRadius;
  if (changed) {
    view.zoomDirty = true;
    requestAxisRefresh(false);
    if (redraw) scheduleDraw(120);
  }
  return changed;
}

let axisRefreshQueued = false;
function requestAxisRefresh(force = false) {
  if (force) {
    view.axisMetaKey = "";
    updateAxisLength();
    return;
  }
  if (axisRefreshQueued) return;
  axisRefreshQueued = true;
  requestAnimationFrame(() => {
    axisRefreshQueued = false;
    updateAxisLength();
  });
}

function computeAxisVisual() {
  const fallbackVisibleLength = Math.max(4, state.viewRadius);
  const fallbackAxisLength = Math.max(8, fallbackVisibleLength * 2.35);
  if (!view.camera || !view.controls || !viewerEl) {
    return {
      axisLength: fallbackAxisLength,
      xLength: fallbackAxisLength,
      yLength: fallbackAxisLength,
      zLength: Math.max(6, fallbackVisibleLength * 1.8),
      visibleLength: fallbackVisibleLength,
      tickInterval: 1.0,
      decimals: 0,
      numberScale: 0.32,
      numberFontSize: 42,
      numberDistance: 0.28,
      tickSize: 0.16,
    };
  }

  const distance = view.camera.position.distanceTo(view.controls.target);
  const vFov = (view.camera.fov * Math.PI) / 180.0;
  const halfVisible = Math.max(0.12, Math.tan(vFov * 0.5) * distance);
  const targetVisibleLength = Math.max(0.18, state.viewRadius);
  const prevVisibleLength = Number(state.axisVisibleLength);
  const visibleLength = Number.isFinite(prevVisibleLength)
    ? (prevVisibleLength * 0.82 + targetVisibleLength * 0.18)
    : targetVisibleLength;
  state.axisVisibleLength = visibleLength;
  const axisLength = Math.max(8.0, visibleLength * 2.35);
  const zLength = Math.max(6.0, visibleLength * 1.8);

  const pxH = Math.max(1, viewerEl.clientHeight);
  const worldPerPixel = (2.0 * halfVisible) / pxH;
  const desiredPx = 92;
  const minTickPx = 66;
  const maxTickPx = 126;
  let tickInterval = Number(state.axisTickInterval);
  if (!Number.isFinite(tickInterval) || tickInterval <= 0) {
    tickInterval = nearestTickStep(worldPerPixel * desiredPx);
  }
  let tickPx = tickInterval / Math.max(worldPerPixel, 1e-6);
  while (tickPx < minTickPx) {
    const next = nextLargerTickStep(tickInterval);
    if (next === tickInterval) break;
    tickInterval = next;
    tickPx = tickInterval / Math.max(worldPerPixel, 1e-6);
  }
  while (tickPx > maxTickPx) {
    const next = nextSmallerTickStep(tickInterval);
    if (next === tickInterval) break;
    tickInterval = next;
    tickPx = tickInterval / Math.max(worldPerPixel, 1e-6);
  }
  tickInterval = Math.max(0.05, tickInterval);
  state.axisTickInterval = tickInterval;

  let decimals = 0;
  let probe = tickInterval;
  while (decimals < 3 && Math.abs(Math.round(probe) - probe) > 1e-6) {
    probe *= 10.0;
    decimals += 1;
  }

  const settledTickPx = tickPx;
  const zoomRatio = Math.max(0.25, visibleLength / DEFAULT_VIEW_RADIUS);
  const zoomReadableBoost = Math.max(1.0, Math.min(1.55, Math.pow(zoomRatio, 0.38)));
  const targetLabelPx = Math.max(32, Math.min(56, (18 + settledTickPx * 0.18) * zoomReadableBoost));
  const labelHeightWorld = targetLabelPx * worldPerPixel;
  const numberScale = Math.max(0.16, Math.min(1.22, labelHeightWorld * 2.35));
  const numberFontSize = Math.round(Math.max(64, Math.min(140, targetLabelPx * 2.55)));
  const numberDistance = Math.max(0.12, Math.min(0.36, tickInterval * 0.14 + labelHeightWorld * 0.28));
  const tickSize = Math.max(0.08, Math.min(0.35, tickInterval * 0.24));

  return {
    axisLength,
    xLength: axisLength,
    yLength: axisLength,
    zLength,
    visibleLength,
    tickInterval,
    decimals,
    numberScale,
    numberFontSize,
    numberDistance,
    tickSize,
  };
}

function _disposeGridHelper(grid) {
  if (!grid) return;
  if (Array.isArray(grid.material)) {
    for (const mat of grid.material) mat?.dispose?.();
  } else {
    grid.material?.dispose?.();
  }
  grid.geometry?.dispose?.();
}

function _createGroundPlane(size) {
  const THREE = view.THREE;
  // 复用共享单位平面顶点数据，通过 scale 达到目标尺寸，
  // 避免每次缩放重建网格时重新分配 PlaneGeometry 顶点缓冲。
  const mesh = new THREE.Mesh(
    getSharedUnitPlane(),
    new THREE.MeshBasicMaterial({
      color: 0x1a2030,
      transparent: true,
      opacity: 0.22,
      side: THREE.DoubleSide,
      depthWrite: false,
    })
  );
  mesh.scale.set(size, size, 1);
  mesh.userData.sharedGeometry = true;
  return mesh;
}

function _createGridHelper(size, divisions) {
  const THREE = view.THREE;
  const grid = new THREE.GridHelper(size, divisions, 0x3a4560, 0x252b3d);
  grid.rotation.x = Math.PI / 2;
  if (Array.isArray(grid.material)) {
    for (const mat of grid.material) {
      mat.depthWrite = false;
      mat.transparent = true;
      mat.opacity = 0.55;
    }
  } else if (grid.material) {
    grid.material.depthWrite = false;
    grid.material.transparent = true;
    grid.material.opacity = 0.55;
  }
  return grid;
}

function updateGroundGrid(cfg) {
  if (!view.scene || !view.THREE) return;
  const size = Math.max(20, niceStep(cfg.axisLength * 2.8));
  let divisions = Math.round(size / Math.max(0.25, cfg.tickInterval));
  divisions = Math.max(24, Math.min(360, divisions));
  if (divisions % 2 !== 0) divisions += 1;

  const key = `${size.toFixed(2)}|${divisions}`;
  if (view.gridMetaKey === key) return;

  if (view.groundPlane) {
    view.scene.remove(view.groundPlane);
    if (!view.groundPlane.userData?.sharedGeometry) view.groundPlane.geometry?.dispose?.();
    view.groundPlane.material?.dispose?.();
  }
  const groundPlane = _createGroundPlane(size);
  groundPlane.visible = !!state.settings.showPlane;
  view.scene.add(groundPlane);
  view.groundPlane = groundPlane;

  if (view.gridHelper) {
    view.scene.remove(view.gridHelper);
    _disposeGridHelper(view.gridHelper);
  }
  const grid = _createGridHelper(size, divisions);
  grid.visible = !!state.settings.showGrid;
  view.scene.add(grid);
  view.gridHelper = grid;

  view.gridMetaKey = key;
}

function updateAxisLength() {
  if (!view.axisGroup || !view.scene) return;
  const cfg = computeAxisVisual();
  updateGroundGrid(cfg);
  const key = [
    cfg.axisLength.toFixed(2),
    cfg.xLength.toFixed(2),
    cfg.yLength.toFixed(2),
    cfg.zLength.toFixed(2),
    cfg.visibleLength.toFixed(2),
    cfg.tickInterval.toFixed(3),
    cfg.decimals,
    cfg.numberScale.toFixed(3),
    cfg.numberDistance.toFixed(3),
    cfg.numberFontSize,
    cfg.tickSize.toFixed(3),
  ].join("|");

  if (view.axisMetaKey === key) return;

  view.scene.remove(view.axisGroup);
  const newAxisGroup = createAxisGroup(view.THREE, cfg.axisLength, {
    xLength: cfg.xLength,
    yLength: cfg.yLength,
    zLength: cfg.zLength,
    visibleLength: cfg.visibleLength,
    tickInterval: cfg.tickInterval,
    decimals: cfg.decimals,
    numberScale: cfg.numberScale,
    numberFontSize: cfg.numberFontSize,
    numberDistance: cfg.numberDistance,
    tickSize: cfg.tickSize,
  });
  view.scene.add(newAxisGroup);
  view.axisGroup = newAxisGroup;
  view.axisMetaKey = key;
}

function renderMiniAxisOverlay() {
  if (!view.miniRenderer || !view.miniScene || !view.miniCamera || !view.miniAxisGroup || !miniAxisEl) return;
  view.miniAxisGroup.quaternion.copy(view.camera.quaternion).invert();
  view.miniRenderer.render(view.miniScene, view.miniCamera);
}

function centerCamera(force = false) {
  if (!view.camera || !view.controls) return;
  if (!force && state.userInteracted) return;

  view.camera.up.set(0, 0, 1);
  view.controls.target.set(0, 0, 0);
  const desiredRadius = DEFAULT_VIEW_RADIUS;
  const desiredDistance = viewRadiusToCameraDistance(desiredRadius);
  const dir = new view.THREE.Vector3(0.88, 0.88, 0.78).normalize();
  view.camera.position.copy(view.controls.target.clone().add(dir.multiplyScalar(desiredDistance)));
  view.camera.lookAt(view.controls.target);
  view.camera.updateMatrixWorld(true);
  view.controls.update();
  state.viewRadius = desiredRadius;
  state.axisVisibleLength = desiredRadius;
  state.axisTickInterval = null;
  view.zoomDirty = false;
  requestAxisRefresh(true);
}

async function loadThreeBundle() {
  const sources = [
    {
      three: "https://esm.sh/three@0.167.1",
      controls: "https://esm.sh/three@0.167.1/examples/jsm/controls/OrbitControls.js",
      postprocessing: {
        EffectComposer: "https://esm.sh/three@0.167.1/examples/jsm/postprocessing/EffectComposer.js",
        RenderPass: "https://esm.sh/three@0.167.1/examples/jsm/postprocessing/RenderPass.js",
        UnrealBloomPass: "https://esm.sh/three@0.167.1/examples/jsm/postprocessing/UnrealBloomPass.js",
      },
    },
    {
      three: "https://cdn.jsdelivr.net/npm/three@0.167.1/+esm",
      controls: "https://cdn.jsdelivr.net/npm/three@0.167.1/examples/jsm/controls/OrbitControls.js/+esm",
      postprocessing: {
        EffectComposer: "https://cdn.jsdelivr.net/npm/three@0.167.1/examples/jsm/postprocessing/EffectComposer.js/+esm",
        RenderPass: "https://cdn.jsdelivr.net/npm/three@0.167.1/examples/jsm/postprocessing/RenderPass.js/+esm",
        UnrealBloomPass: "https://cdn.jsdelivr.net/npm/three@0.167.1/examples/jsm/postprocessing/UnrealBloomPass.js/+esm",
      },
    },
  ];
  let lastError = null;
  for (const src of sources) {
    try {
      const THREE = await import(src.three);
      const controlsMod = await import(src.controls);
      const composerMod = await import(src.postprocessing.EffectComposer);
      const renderPassMod = await import(src.postprocessing.RenderPass);
      const bloomPassMod = await import(src.postprocessing.UnrealBloomPass);
      return {
        THREE,
        OrbitControls: controlsMod.OrbitControls,
        EffectComposer: composerMod.EffectComposer,
        RenderPass: renderPassMod.RenderPass,
        UnrealBloomPass: bloomPassMod.UnrealBloomPass,
      };
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError || new Error("three.js load failed");
}

function applyDisplaySettings() {
  if (view.gridHelper) view.gridHelper.visible = !!state.settings.showGrid;
  if (view.groundPlane) view.groundPlane.visible = !!state.settings.showPlane;
  if (legendEl) legendEl.style.display = state.settings.showLegend ? "block" : "none";
}

async function ensure3D() {
  if (view.started) return true;
  try {
    const { THREE, OrbitControls, EffectComposer, RenderPass, UnrealBloomPass } = await loadThreeBundle();
    view.THREE = THREE;
    view.EffectComposer = EffectComposer;
    view.RenderPass = RenderPass;
    view.UnrealBloomPass = UnrealBloomPass;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b0d12);

    const camera = new THREE.PerspectiveCamera(47, 1, 0.02, 2600);
    camera.up.set(0, 0, 1);
    camera.position.set(-11, 13, 9);

    const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, rendererPixelRatioCap()));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    viewerEl.innerHTML = "";
    viewerEl.appendChild(renderer.domElement);
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    renderer.domElement.style.display = "block";

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.target.set(0, 0, 0);
    // 鼠标滚轮缩放时，以指针所在位置作为缩放中心
    controls.zoomToCursor = true;
    controls.minDistance = viewRadiusToCameraDistance(MIN_VIEW_RADIUS);
    controls.maxDistance = viewRadiusToCameraDistance(MAX_VIEW_RADIUS);
    controls.mouseButtons.RIGHT = THREE.MOUSE.PAN;
    controls.mouseButtons.LEFT = THREE.MOUSE.ROTATE;
    controls.mouseButtons.MIDDLE = THREE.MOUSE.DOLLY;
    controls.addEventListener("start", () => {
      state.userInteracted = true;
    });
    let lastCameraDist = camera.position.distanceTo(controls.target);
    controls.addEventListener("change", () => {
      const d = camera.position.distanceTo(controls.target);
      const rel = Math.abs(d - lastCameraDist) / Math.max(1e-6, lastCameraDist);
      if (rel > 0.01) {
        lastCameraDist = d;
        syncViewRadiusFromCamera(false);
      }
    });
    controls.addEventListener("end", () => {
      if (!view.zoomDirty) return;
      view.zoomDirty = false;
      if (!state.hasDrawn) return;
      if (refreshLocalPlaneMeshesOnViewChange()) {
        scheduleIntersectionRefresh(40, { lod: false });
        setStatus("视野已更新");
        return;
      }
      scheduleDraw(80);
    });

    const ambient = new THREE.AmbientLight(0xffffff, 0.35);
    scene.add(ambient);

    const key = new THREE.DirectionalLight(0xdce6ff, 0.85);
    key.position.set(8, 12, 10);
    scene.add(key);

    const fill = new THREE.DirectionalLight(0x6c7bff, 0.35);
    fill.position.set(-8, -6, 6);
    scene.add(fill);

    const rim = new THREE.DirectionalLight(0x00e5ff, 0.25);
    rim.position.set(-6, 8, -8);
    scene.add(rim);

    const groundPlane = _createGroundPlane(48);
    scene.add(groundPlane);

    const grid = _createGridHelper(48, 48);
    scene.add(grid);

    const axisGroup = createAxisGroup(THREE, 10);
    scene.add(axisGroup);

    const miniScene = new THREE.Scene();
    miniScene.add(new THREE.AmbientLight(0xffffff, 0.95));
    const miniAxisGroup = createAxisGroup(THREE, 1.28, {
      labelOffset: 0.98,
      labelScale: 1.35,
      labelFontSize: 112,
      arrowHeadLength: 0.3,
      arrowHeadWidth: 0.18,
      lineOpacity: 1.0,
      showNumbers: false,
    });
    miniScene.add(miniAxisGroup);
    const miniCamera = new THREE.PerspectiveCamera(58, 1, 0.1, 30);
    miniCamera.position.set(0, 0, 5.8);
    miniCamera.lookAt(0, 0, 0);

    let miniRenderer = null;
    if (miniAxisEl) {
      miniRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
      miniRenderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      miniRenderer.setClearColor(0x000000, 0);
      miniRenderer.autoClear = true;
      miniAxisEl.innerHTML = "";
      miniAxisEl.appendChild(miniRenderer.domElement);
      miniRenderer.domElement.style.width = "100%";
      miniRenderer.domElement.style.height = "100%";
      miniRenderer.domElement.style.display = "block";
    }

    const meshGroup = new THREE.Group();
    const intersectionGroup = new THREE.Group();
    scene.add(meshGroup);
    scene.add(intersectionGroup);

    // 后处理辉光
    const composer = new EffectComposer(renderer);
    const renderPass = new RenderPass(scene, camera);
    composer.addPass(renderPass);
    const bloomPass = new UnrealBloomPass(
      new THREE.Vector2(1, 1),
      0.55,
      0.25,
      0.82
    );
    composer.addPass(bloomPass);

    view.scene = scene;
    view.camera = camera;
    view.renderer = renderer;
    view.controls = controls;
    view.meshGroup = meshGroup;
    view.intersectionGroup = intersectionGroup;
    view.gridHelper = grid;
    view.groundPlane = groundPlane;
    view.axisGroup = axisGroup;
    view.miniRenderer = miniRenderer;
    view.miniScene = miniScene;
    view.miniCamera = miniCamera;
    view.miniAxisGroup = miniAxisGroup;
    view.composer = composer;

    const resize = () => {
      const parent = viewerEl.parentElement;
      if (!parent) return;

      const w = Math.max(1, parent.clientWidth);
      const h = Math.max(1, parent.clientHeight);

      renderer.setSize(w, h, false);
      composer.setSize(w, h);
      bloomPass.resolution.set(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      centerCamera(false);
      requestAxisRefresh(false);
      if (miniRenderer && miniAxisEl) {
        const mw = Math.max(1, miniAxisEl.clientWidth);
        const mh = Math.max(1, miniAxisEl.clientHeight);
        miniRenderer.setSize(mw, mh, false);
        miniCamera.aspect = mw / mh;
        miniCamera.updateProjectionMatrix();
      }
    };
    view.resize = resize;
    window.addEventListener("resize", resize);
    resize();

    const animate = () => {
      if (!state.userInteracted && state.autoCenterFrames > 0) {
        centerCamera(true);
        state.autoCenterFrames -= 1;
      }
      controls.update();
      renderer.setScissorTest(false);
      
      // 直接使用父容器的尺寸
      const parent = viewerEl.parentElement;
      const w = Math.max(1, parent ? parent.clientWidth : 1);
      const h = Math.max(1, parent ? parent.clientHeight : 1);
      
      renderer.setViewport(0, 0, w, h);
      composer.render();
      renderMiniAxisOverlay();
      
      // Update LOD objects based on camera distance
      updateAllLODs();
      
      requestAnimationFrame(animate);
    };
    centerCamera(true);
    setTimeout(() => centerCamera(true), 30);
    animate();

    applyDisplaySettings();
    requestAxisRefresh(true);
    view.started = true;
    return true;
  } catch (err) {
    setStatus(`3D 引擎加载失败: ${String(err)}`, "error");
    return false;
  }
}

async function fetchLabel(eqText) {
  const cached = state.labelCache.get(eqText);
  if (cached) return cached;
  try {
    const resp = await fetch(`${apiBase()}/api/label`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ equation: eqText }),
    });
    if (!resp.ok) return eqText;
    const data = await resp.json();
    const label = data.label || eqText;
    state.labelCache.set(eqText, label);
    return label;
  } catch {
    return eqText;
  }
}

function renderFormulaInto(container, eqText) {
  container.textContent = eqText;
  fetchLabel(eqText).then((label) => {
    if (label && container.textContent !== label) container.textContent = label;
  });
}

async function renderEqList() {
  eqList.innerHTML = "";
  const emptyEl = document.getElementById("emptyState");
  const countEl = document.getElementById("eqCount");
  if (emptyEl) emptyEl.classList.toggle("hidden", state.equations.length > 0);
  if (countEl) countEl.textContent = String(state.equations.length);

  for (const [idx, eq] of state.equations.entries()) {
    const li = document.createElement("li");
    li.className = "eq-item";
    li.innerHTML = `
      <span class="badge" style="background:${eq.color};box-shadow:0 0 8px ${eq.color}"></span>
      <div class="eq-text"></div>
      <button class="icon-del" title="删除">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    `;
    const txt = li.querySelector(".eq-text");
    txt.textContent = eq.label || eq.text;
    li.querySelector(".icon-del").onclick = (e) => {
      e.stopPropagation();
      removeEquation(eq.id);
    };
    eqList.appendChild(li);
  }
  for (const [idx, eq] of state.equations.entries()) {
    const li = eqList.children[idx];
    if (!li) continue;
    const txt = li.querySelector(".eq-text");
    const label = await fetchLabel(eq.text);
    eq.label = label;
    if (txt) txt.textContent = label;
  }
  renderLegend();
}

async function renderLegend() {
  legendEl.innerHTML = `
    <div class="legend-header">
      <span class="legend-id">EQ</span>
      <h3>方程列表</h3>
    </div>
  `;
  for (const [idx, eq] of state.equations.entries()) {
    const row = document.createElement("div");
    row.className = "legend-row";
    row.innerHTML = `<span class="badge" style="background:${eq.color};box-shadow:0 0 8px ${eq.color}"></span><span class="legend-text"></span>`;
    const txt = row.querySelector(".legend-text");
    txt.textContent = eq.label || eq.text;
    legendEl.appendChild(row);
  }
  for (const [idx, eq] of state.equations.entries()) {
    const row = legendEl.children[idx + 1];
    if (!row) continue;
    const txt = row.querySelector(".legend-text");
    const label = await fetchLabel(eq.text);
    eq.label = label;
    if (txt) txt.textContent = label;
  }
}

async function renderParamList() {
  paramList.innerHTML = "";
  if (!state.params.size) {
    const empty = document.createElement("div");
    empty.style.color = "#8795a8";
    empty.style.fontSize = "13px";
    empty.textContent = "当前没有参数";
    paramList.appendChild(empty);
    return;
  }

  for (const [name, item] of state.params.entries()) {
    const card = document.createElement("div");
    card.className = "param-item";
    card.innerHTML = `
      <div class="param-top">
        <span class="param-name">${esc(name)}</span>
        <span></span>
        <input type="number" step="0.1" value="${item.value.toFixed(2)}" />
        <button class="icon-del" title="删除参数">✕</button>
      </div>
      <div class="param-row">
        <span>${item.min.toFixed(1)}</span>
        <input type="range" min="0" max="1000" value="${Math.round(((item.value - item.min) / (item.max - item.min)) * 1000)}" />
        <span>${item.max.toFixed(1)}</span>
      </div>
    `;

    const inputNum = card.querySelector('input[type="number"]');
    const inputRange = card.querySelector('input[type="range"]');
    const delBtn = card.querySelector(".icon-del");

    inputRange.addEventListener("input", () => {
      const t = Number(inputRange.value) / 1000;
      const value = item.min + t * (item.max - item.min);
      item.value = value;
      inputNum.value = value.toFixed(2);
      scheduleDraw(80);
    });

    inputNum.addEventListener("change", () => {
      const next = Number(inputNum.value);
      if (!Number.isFinite(next)) {
        inputNum.value = item.value.toFixed(2);
        return;
      }
      const clamped = Math.max(item.min, Math.min(item.max, next));
      item.value = clamped;
      const t = (clamped - item.min) / (item.max - item.min);
      inputRange.value = String(Math.round(t * 1000));
      inputNum.value = clamped.toFixed(2);
      scheduleDraw(80);
    });

    delBtn.onclick = () => {
      state.params.delete(name);
      renderParamList();
      scheduleDraw(80);
    };

    paramList.appendChild(card);
  }
}

function pruneUnusedParams() {
  const needed = new Set();
  for (const eq of state.equations) {
    for (const p of eq.paramNames) needed.add(p);
  }
  for (const name of [...state.params.keys()]) {
    if (!needed.has(name)) state.params.delete(name);
  }
}

function ensureParam(name, val = 1) {
  if (state.params.has(name)) return;
  const min = Math.min(-10, val - 5);
  const max = Math.max(10, val + 5);
  state.params.set(name, { value: val, min, max });
}

async function parseEquation(eqText) {
  const resp = await fetch(`${apiBase()}/api/parse`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ equation: eqText, params: {} }),
  });
  if (!resp.ok) throw new Error(await resp.text());
  return await resp.json();
}

async function fetchMesh(eq) {
  const resp = await fetch(`${apiBase()}/api/mesh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      equation: eq.text,
      params: paramsObject(),
      view_radius: currentRequestViewRadius(),
      lod: false,
      quality: state.quality,
    }),
  });
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(txt || `HTTP ${resp.status}`);
  }
  return await resp.json();
}

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
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(txt || `HTTP ${resp.status}`);
  }
  return await resp.json();
}

function removeMeshById(id) {
  const obj = state.meshById.get(id);
  if (!obj || !view.meshGroup) return;
  if (obj.userData?.planeInstance) removePlaneInstance(id);
  view.meshGroup.remove(obj);
  obj.traverse?.((child) => {
    if (child.geometry && !child.userData?.sharedGeometry) child.geometry.dispose();
    if (child.material) child.material.dispose();
  });
  state.meshById.delete(id);
  // Also remove LOD object if exists
  const lodObj = state.lodObjects.get(id);
  if (lodObj && view.meshGroup) {
    view.meshGroup.remove(lodObj);
    lodObj.traverse?.((child) => {
      if (child.geometry && !child.userData?.sharedGeometry) child.geometry.dispose();
      if (child.material) child.material.dispose();
    });
    state.lodObjects.delete(id);
  }
  // Cancel any pending LOD fetch
  if (state.lodPending.has(id)) {
    state.lodPending.delete(id);
  }
}

function clearAllMeshes() {
  for (const id of [...state.meshById.keys()]) removeMeshById(id);
  // Clear LOD state
  state.lodObjects.clear();
  state.lodPending.clear();
}

function clearIntersections() {
  replaceIntersectionObjects([]);
}

function buildExactPlaneObject(meshData, color) {
  const THREE = view.THREE;
  const quad = meshData.meta?.display_quad;
  if (!Array.isArray(quad) || quad.length !== 4) return null;

  const p0 = new THREE.Vector3(...quad[0]);
  const p1 = new THREE.Vector3(...quad[1]);
  const p2 = new THREE.Vector3(...quad[2]);
  const p3 = new THREE.Vector3(...quad[3]);

  const uVec = p1.clone().sub(p0);
  const vVec = p2.clone().sub(p0);
  const width = uVec.length();
  const height = vVec.length();
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 1e-6 || height <= 1e-6) return null;

  const uDir = uVec.clone().normalize();
  const vDir = vVec.clone().normalize();
  const nDir = new THREE.Vector3().crossVectors(uDir, vDir).normalize();
  if (!Number.isFinite(nDir.x) || !Number.isFinite(nDir.y) || !Number.isFinite(nDir.z) || nDir.lengthSq() <= 1e-8) return null;

  const center = new THREE.Vector3()
    .add(p0)
    .add(p1)
    .add(p2)
    .add(p3)
    .multiplyScalar(0.25);

  // InstancedMesh 优化：不再为每个平面创建独立 PlaneGeometry(width, height)。
  // 所有精确平面共用 1x1 单位平面顶点数据，宽高烘焙进实例矩阵的基向量缩放，
  // 颜色通过 InstancedMesh.setColorAt 逐实例设置，N 个平面 = 1 次 draw call。
  // 返回一个轻量占位 Group（不含子对象），实例注册在 replaceEquationMesh 中完成。
  const basis = new THREE.Matrix4().makeBasis(
    uDir.clone().multiplyScalar(width),
    vDir.clone().multiplyScalar(height),
    nDir
  );
  basis.setPosition(center);

  const group = new THREE.Group();
  group.userData.planeInstance = true;
  group.userData.planeMatrix = basis;
  group.userData.planeColor = new THREE.Color(color);
  return group;
}

function buildMeshObject(meshData, color, useLOD = false) {
  const THREE = view.THREE;
  const verts = meshData.vertices || [];
  const faces = meshData.faces || [];
  const geometryType = meshData.meta?.geometry_type || "";
  if (geometryType === "plane") {
    const exactPlane = buildExactPlaneObject(meshData, color);
    if (exactPlane) return exactPlane;
  }
  const singleSurfaceMode = geometryType === "hemisphere" || state.equations.length <= 1;

  // Plane display uses a dedicated quad so the rendered surface is visually a plane
  // instead of inheriting a clipped polygon silhouette from the dense computational mesh.
  let renderVerts = verts;
  let renderFaces = faces;
  const displayQuad = meshData.meta?.display_quad;
  if (geometryType === "plane" && Array.isArray(displayQuad) && displayQuad.length === 4) {
    renderVerts = displayQuad;
    renderFaces = [
      [0, 1, 2],
      [1, 3, 2],
    ];
  }

  const positions = new Float32Array(renderVerts.length * 3);
  for (let i = 0; i < renderVerts.length; i += 1) {
    positions[i * 3] = renderVerts[i][0];
    positions[i * 3 + 1] = renderVerts[i][1];
    positions[i * 3 + 2] = renderVerts[i][2];
  }
  const maxIndex = renderVerts.length > 0 ? renderVerts.length - 1 : 0;
  const IndexArray = maxIndex <= 65535 ? Uint16Array : Uint32Array;
  const indices = new IndexArray(renderFaces.length * 3);
  for (let i = 0; i < renderFaces.length; i += 1) {
    indices[i * 3] = renderFaces[i][0];
    indices[i * 3 + 1] = renderFaces[i][1];
    indices[i * 3 + 2] = renderFaces[i][2];
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setIndex(new THREE.BufferAttribute(indices, 1));
  geometry.computeVertexNormals();
  geometry.normalizeNormals();

  const createMaterial = (side, opacity, emissiveIntensity) => {
    return new THREE.MeshPhysicalMaterial({
      color,
      transparent: true,
      opacity,
      side,
      roughness: 0.35,
      metalness: 0.35,
      clearcoat: 0.55,
      emissive: color,
      emissiveIntensity,
      depthWrite: false,
      depthTest: true,
      blending: THREE.NormalBlending,
      polygonOffset: true,
      polygonOffsetFactor: -1,
      polygonOffsetUnits: -2,
    });
  };

  const group = new THREE.Group();
  
  if (singleSurfaceMode) {
    const singleMat = createMaterial(THREE.DoubleSide, 0.22, 0.1);
    const singleMesh = new THREE.Mesh(geometry, singleMat);
    singleMesh.renderOrder = 10;
    group.add(singleMesh);
    return group;
  }

  const backMat = createMaterial(THREE.BackSide, 0.14, 0.06);
  const frontMat = createMaterial(THREE.FrontSide, 0.24, 0.1);
  const backMesh = new THREE.Mesh(geometry, backMat);
  const frontMesh = new THREE.Mesh(geometry, frontMat);
  backMesh.renderOrder = 10;
  frontMesh.renderOrder = 11;
  group.add(backMesh);
  group.add(frontMesh);
  return group;
}

function buildIntersectionObject(polyline, opts = {}) {
  const THREE = view.THREE;
  if (!Array.isArray(polyline) || polyline.length < 2) return null;
  let raw = polyline;
  if (opts.preview && polyline.length > 240) {
    const stride = Math.max(1, Math.ceil(polyline.length / 240));
    raw = polyline.filter((_, idx) => idx === polyline.length - 1 || idx % stride === 0);
  }
  const points = raw.map((p) => new THREE.Vector3(p[0], p[1], p[2]));
  if (points.length < 2) return null;
  const geometry = new THREE.BufferGeometry().setFromPoints(points);
  const material = new THREE.LineBasicMaterial({
    color: 0x00e5ff,
    transparent: true,
    opacity: 1.0,
    depthWrite: false,
    depthTest: false,
    blending: THREE.AdditiveBlending,
  });
  const line = new THREE.Line(geometry, material);
  line.renderOrder = 999;
  return line;
}

async function loadIntersectionCurves(seq, opts = {}) {
  if (!view.intersectionGroup) return;
  if (state.equations.length < 2) {
    clearIntersections();
    return;
  }

  const useLod = !!opts.lod;
  const useQuality = Number(opts.quality ?? state.quality) || state.quality;

  const payload = await fetch(`${apiBase()}/api/intersections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      equations: state.equations.map((eq) => eq.text),
      params: paramsObject(),
      view_radius: currentRequestViewRadius(),
      lod: useLod,
      quality: useQuality,
    }),
  });
  if (!payload.ok) {
    const txt = await payload.text();
    throw new Error(txt || `HTTP ${payload.status}`);
  }
  const data = await payload.json();
  if (seq !== state.drawSeq) return;

  const curves = Array.isArray(data.curves) ? data.curves : [];
  const created = [];
  for (const c of curves) {
    const polylines = Array.isArray(c.polylines) ? c.polylines : [];
    for (const polyline of polylines) {
      const obj = buildIntersectionObject(polyline, { preview: useLod });
      if (!obj) continue;
      created.push(obj);
    }
  }
  if (seq !== state.drawSeq) {
    disposeSceneObjects(created, null);
    return;
  }
  replaceIntersectionObjects(created);
}

async function drawAll(mode = "manual") {
  if (!state.equations.length) {
    clearAllMeshes();
    state.hasDrawn = false;
    setStatus("请先添加方程", "error");
    return;
  }

  const ok3d = await ensure3D();
  if (!ok3d) return;

  // Ensure mesh worker is ready (non-blocking; falls back gracefully if unavailable)
  initMeshWorker();

  // 更新坐标轴长度以匹配视野范围
  updateAxisLength();

  clearTimeout(state.highQualityTimer);
  const seq = ++state.drawSeq;
  setStatus(mode === "manual" ? "正在绘制..." : "参数更新中...", "working");
  clearAllMeshes();
  clearIntersections();

  // Partition equations: frontend exact planes go straight to scene,
  // the rest go to the Web Worker (or fallback main-thread fetch).
  const frontendEqs = [];
  const workerEqs = [];
  for (const eq of state.equations) {
    if (isFrontendExactPlaneEquation(eq)) {
      frontendEqs.push(eq);
    } else {
      workerEqs.push(eq);
    }
  }

  // Render local exact planes immediately (no backend needed)
  let localDrawn = 0;
  for (const eq of frontendEqs) {
    if (seq !== state.drawSeq) return;
    const localMesh = buildLocalExactPlaneMeshData(eq);
    if (!meshHasRenderableGeometry(localMesh)) continue;
    if (!equationStillExists(eq.id)) continue;
    if (replaceEquationMesh(eq, localMesh)) {
      const obj = state.meshById.get(eq.id);
      if (obj) obj.userData.isPreview = false;
      localDrawn += 1;
    }
  }

  if (seq !== state.drawSeq) return;
  state.hasDrawn = true;

  // Kick off intersection preview in parallel with mesh fetches
  const previewIntersectionsPromise = state.equations.length >= 2
    ? loadIntersectionCurves(seq, { lod: true, quality: 1 })
    .catch((err) => console.warn("交线绘制失败:", err))
    : Promise.resolve();

  if (workerEqs.length === 0) {
    // All equations are frontend exact planes
    try { await previewIntersectionsPromise; } catch (_) {}
    if (seq !== state.drawSeq) return;
    setStatus(`绘制完成 (${state.equations.length}/${state.equations.length})`);
    return;
  }

  // Kick off mesh fetches via worker (or fallback)
  // Results arrive out-of-order and are applied incrementally.
  // The worker calls onAllWorkerResultsIn(seq) when all tasks are done.
  scheduleWorkerMeshFetch(workerEqs, seq, {
    lod: true,
    quality: 1,
    fetchParams: paramsObject(),
  });

  setStatus(`快速预览中 (0/${workerEqs.length})…`);

  // Await intersection preview for status message
  try { await previewIntersectionsPromise; } catch (_) {}
  if (seq !== state.drawSeq) return;

  if (localDrawn === 0 && workerEqs.length === 0) {
    setStatus("当前视图内没有可绘制几何", "error");
  } else if (localDrawn > 0 && workerEqs.length === 0) {
    setStatus(`绘制完成 (${localDrawn}/${localDrawn})`);
  }
  // Otherwise status will be updated by onAllWorkerResultsIn / onWorkerMessage
}

async function loadHighQualityVersion(seq) {
  try {
    if (seq !== state.drawSeq) return;
    const equations = state.equations.map(eq => eq.text);
    if (equations.length === 0) return;

    const highQualityIntersectionsPromise = equations.length >= 2
      ? loadIntersectionCurves(seq, { lod: false, quality: state.quality })
      .catch((err) => console.warn("交线更新失败:", err))
      : Promise.resolve();

    // Collect non-frontend equations for high-quality worker fetch
    const hqEqs = state.equations.filter((eq) => !isFrontendExactPlaneEquation(eq));
    if (hqEqs.length === 0) {
      try { await highQualityIntersectionsPromise; } catch (_) {}
      if (seq !== state.drawSeq) return;
      setStatus(`高质量优化完成 (${state.equations.length}/${state.equations.length})`);
      return;
    }

    // Kick off high-quality mesh fetches via worker (or fallback)
    scheduleWorkerHighQualityFetch(hqEqs, seq);

    // High-quality intersections run in parallel; they don't block mesh upgrade.
    // onAllWorkerResultsIn will set the final status when all meshes are in.
    try { await highQualityIntersectionsPromise; } catch (_) {}

  } catch (err) {
    console.warn("高质量加载失败:", err);
  } finally {
    if (seq === state.drawSeq) state.highQualityTimer = null;
  }
}

async function addEquationFromInput() {
  const text = (eqInput.value || "").trim();
  if (!text) return;

  try {
    setStatus("正在解析方程...", "working");
    const data = await parseEquation(text);
    if (!data.ok || !data.parsed) {
      setStatus("该输入不是可绘制几何方程", "error");
      return;
    }

    const paramNames = Array.isArray(data.params_needed) ? data.params_needed : [];
    for (const p of paramNames) ensureParam(p, 1.0);

    const exactPlaneCoeffs = extractLinearPlaneCoefficients(text);
    const label = await fetchLabel(text);
    state.equations.push({
      id: uid(),
      text,
      label,
      color: palette[state.equations.length % palette.length],
      paramNames,
      exactPlaneCoeffs,
      frontendExactPlane: !!exactPlaneCoeffs,
    });

    // 清空输入框，但保持输入面板显示
    eqInput.value = "";
    renderEqList();
    renderLegend();
    renderParamList();
    setStatus(`已添加 ${label || text}，点击“绘制”显示图形`);
  } catch (err) {
    setStatus(`解析失败: ${String(err)}`, "error");
  }
}

function removeEquation(id) {
  cancelPendingAsyncDraws();
  state.equations = state.equations.filter((e) => e.id !== id);
  removeMeshById(id);
  clearIntersections();
  pruneUnusedParams();
  renderEqList();
  renderLegend();
  renderParamList();
  if (!state.equations.length) {
    clearAllMeshes();
    clearIntersections();
    state.hasDrawn = false;
    setStatus("已删除方程");
    return;
  }
  if (state.hasDrawn) scheduleDraw(30);
}

function clearAll() {
  cancelPendingAsyncDraws();
  state.equations = [];
  state.params.clear();
  clearAllMeshes();
  clearIntersections();
  state.hasDrawn = false;
  renderEqList();
  renderLegend();
  renderParamList();
  setStatus("已清空");
}

function dolly(factor) {
  if (!view.camera || !view.controls) return;
  const target = view.controls.target.clone();
  const dir = view.camera.position.clone().sub(target).multiplyScalar(factor);
  const next = target.clone().add(dir);
  const nextDistance = next.distanceTo(target);
  if (nextDistance < view.controls.minDistance || nextDistance > view.controls.maxDistance) return;
  view.camera.position.copy(next);
  view.controls.update();
  const changed = syncViewRadiusFromCamera(false);
  if (changed && refreshLocalPlaneMeshesOnViewChange()) {
    scheduleIntersectionRefresh(40, { lod: false });
    setStatus("视野已更新");
    return;
  }
  if (!changed) requestAxisRefresh(false);
  else if (state.hasDrawn) scheduleDraw(80);
}

function resetView() {
  state.userInteracted = false;
  state.autoCenterFrames = 30;
  centerCamera(true);
}

function insertToken(token) {
  const text = token === "pi" ? "pi" : token;
  const start = eqInput.selectionStart ?? eqInput.value.length;
  const end = eqInput.selectionEnd ?? eqInput.value.length;
  eqInput.value = eqInput.value.slice(0, start) + text + eqInput.value.slice(end);
  const cursor = text === "sqrt()" ? start + 5 : start + text.length;
  eqInput.focus();
  eqInput.setSelectionRange(cursor, cursor);
}

function showInputPad() {
  if (!inputPad) return;
  inputPad.classList.remove("hidden");
}

function hideInputPad() {
  if (!inputPad) return;
  inputPad.classList.add("hidden");
}

function moveCursor(delta) {
  const base = eqInput.selectionStart ?? eqInput.value.length;
  const next = Math.max(0, Math.min(eqInput.value.length, base + delta));
  eqInput.focus();
  eqInput.setSelectionRange(next, next);
}

function backspaceToken() {
  const start = eqInput.selectionStart ?? eqInput.value.length;
  const end = eqInput.selectionEnd ?? eqInput.value.length;
  if (start !== end) {
    eqInput.value = eqInput.value.slice(0, start) + eqInput.value.slice(end);
    eqInput.focus();
    eqInput.setSelectionRange(start, start);
    return;
  }
  if (start <= 0) return;
  const next = start - 1;
  eqInput.value = eqInput.value.slice(0, next) + eqInput.value.slice(start);
  eqInput.focus();
  eqInput.setSelectionRange(next, next);
}

function bindEvents() {
  addBtn.onclick = addEquationFromInput;
  drawBtn.onclick = () => drawAll("manual");
  clearBtn.onclick = clearAll;

  eqInput.addEventListener("focus", showInputPad);
  eqInput.addEventListener("click", showInputPad);

  eqInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") addEquationFromInput();
    if (e.key === "Escape") hideInputPad();
  });

  if (qualitySel) {
    qualitySel.value = String(state.quality);
    qualitySel.addEventListener("change", () => {
      state.quality = Number(qualitySel.value) || 1;
      if (view.renderer) {
        view.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, rendererPixelRatioCap()));
        if (view.resize) view.resize();
      }
      scheduleDraw(40);
    });
  }

  zoomInBtn.onclick = () => dolly(0.84);
  zoomOutBtn.onclick = () => dolly(1.2);
  resetViewBtn.onclick = resetView;
  resetViewBtnTop.onclick = resetView;

  if (inputPad) {
    inputPad.querySelectorAll(".math-key").forEach((btn) => {
      btn.addEventListener("click", () => {
        const action = btn.dataset.action || "";
        if (action === "left") {
          moveCursor(-1);
          return;
        }
        if (action === "right") {
          moveCursor(1);
          return;
        }
        if (action === "backspace") {
          backspaceToken();
          return;
        }
        if (action === "clear") {
          eqInput.value = "";
          eqInput.focus();
          return;
        }
        insertToken(btn.dataset.token || btn.textContent || "");
      });
    });
  }

  if (closeInputPadBtn) closeInputPadBtn.onclick = hideInputPad;

  if (toggleKeyboardBtn && inputPad) {
    toggleKeyboardBtn.onclick = () => {
      inputPad.classList.toggle("hidden");
      toggleKeyboardBtn.classList.toggle("active", !inputPad.classList.contains("hidden"));
    };
  }

  document.addEventListener("pointerdown", (evt) => {
    if (!inputPad || inputPad.classList.contains("hidden")) return;
    const t = evt.target;
    // 点击输入面板、输入框、添加按钮时都不关闭输入面板
    if (inputPad.contains(t) || eqInput.contains(t) || t.id === "addBtn") return;
    hideInputPad();
  });

  if (navToggleBtn) {
    navToggleBtn.onclick = () => {
      const collapsed = document.body.classList.toggle("panel-collapsed");
      navToggleBtn.classList.toggle("active", collapsed);
      setTimeout(() => {
        if (view.resize) view.resize();
        requestAxisRefresh(true);
      }, 180);
    };
  }

  if (toggleGrid) {
    toggleGrid.checked = state.settings.showGrid;
    toggleGrid.onchange = () => {
      state.settings.showGrid = !!toggleGrid.checked;
      applyDisplaySettings();
    };
  }

  if (togglePlane) {
    togglePlane.checked = state.settings.showPlane;
    togglePlane.onchange = () => {
      state.settings.showPlane = !!togglePlane.checked;
      applyDisplaySettings();
    };
  }

  if (toggleLegend) {
    toggleLegend.checked = state.settings.showLegend;
    toggleLegend.onchange = () => {
      state.settings.showLegend = !!toggleLegend.checked;
      applyDisplaySettings();
    };
  }

}

bindEvents();
renderEqList();
renderLegend();
renderParamList();
ensure3D();
