"""解析几何优先的等值面网格生成器。

从 EMath3DVisualizer 提取核心逻辑并适配为无 Qt/无 PyVista 依赖的实现，
直接输出 Three.js 可用的顶点和面索引数组。

策略优先级（按速度和质量从高到低）：
1. 显式曲面 z=f(x,y) / y=f(x,z) / x=f(y,z)
2. 标准球体参数化生成
3. 圆柱体参数化生成
4. 圆锥/抛物面参数化生成
5. 平面参数化生成
6. Marching Cubes 通用回退
"""
import math
from typing import Optional

import numpy as np
import sympy as sp
from skimage.measure import marching_cubes

from .equation_parser import XYZ_SET


# 默认视图半径，控制网格生成范围
default_view_radius = 12.0


def _sphere_triangulation(cx: float, cy: float, cz: float, radius: float, resolution: int = 80):
    """生成球体网格，返回 (vertices, faces)。"""
    phi = np.linspace(0, np.pi, resolution)
    theta = np.linspace(0, 2 * np.pi, resolution)
    phi, theta = np.meshgrid(phi, theta, indexing='ij')

    x = cx + radius * np.sin(phi) * np.cos(theta)
    y = cy + radius * np.sin(phi) * np.sin(theta)
    z = cz + radius * np.cos(phi)

    vertices = np.stack([x.ravel(), y.ravel(), z.ravel()], axis=-1)

    faces = []
    n = resolution
    for i in range(n - 1):
        for j in range(n - 1):
            p00 = i * n + j
            p01 = i * n + (j + 1)
            p10 = (i + 1) * n + j
            p11 = (i + 1) * n + (j + 1)
            faces.append([p00, p10, p11])
            faces.append([p00, p11, p01])

    return vertices.astype(np.float32), np.array(faces, dtype=np.int32)


def _cylinder_triangulation(
    center, direction, radius: float, height: float, resolution: int = 48
):
    """生成圆柱体侧壁网格（不封口），返回 (vertices, faces)。"""
    center = np.asarray(center, dtype=float)
    direction = np.asarray(direction, dtype=float)
    direction = direction / (np.linalg.norm(direction) + 1e-12)

    # 构造局部坐标系
    if abs(direction[2]) < 0.95:
        ref = np.array([0.0, 0.0, 1.0])
    else:
        ref = np.array([1.0, 0.0, 0.0])
    u = np.cross(direction, ref)
    u = u / (np.linalg.norm(u) + 1e-12)
    v = np.cross(direction, u)

    theta = np.linspace(0, 2 * np.pi, resolution, endpoint=False)
    z_local = np.linspace(-height / 2, height / 2, 2)
    theta_grid, z_grid = np.meshgrid(theta, z_local, indexing='ij')

    r = radius
    local_x = r * np.cos(theta_grid)
    local_y = r * np.sin(theta_grid)
    local_z = z_grid

    vertices = []
    for i in range(theta_grid.shape[0]):
        for j in range(theta_grid.shape[1]):
            p = center + local_x[i, j] * u + local_y[i, j] * v + local_z[i, j] * direction
            vertices.append(p)
    vertices = np.array(vertices, dtype=float)

    n_theta = resolution
    n_z = 2
    faces = []
    for i in range(n_theta):
        i_next = (i + 1) % n_theta
        for j in range(n_z - 1):
            p00 = i * n_z + j
            p01 = i * n_z + (j + 1)
            p10 = i_next * n_z + j
            p11 = i_next * n_z + (j + 1)
            faces.append([p00, p10, p11])
            faces.append([p00, p11, p01])

    return vertices.astype(np.float32), np.array(faces, dtype=np.int32)


def _plane_triangulation(normal, center, size: float, resolution: int = 2):
    """生成平面网格，返回 (vertices, faces)。"""
    normal = np.asarray(normal, dtype=float)
    center = np.asarray(center, dtype=float)
    n = normal / (np.linalg.norm(normal) + 1e-12)

    if abs(n[2]) < 0.95:
        ref = np.array([0.0, 0.0, 1.0])
    else:
        ref = np.array([1.0, 0.0, 0.0])
    u = np.cross(n, ref)
    u = u / (np.linalg.norm(u) + 1e-12)
    v = np.cross(n, u)

    half = size / 2
    steps = np.linspace(-half, half, resolution)
    vertices = []
    for s in steps:
        for t in steps:
            p = center + s * u + t * v
            vertices.append(p)
    vertices = np.array(vertices, dtype=float)

    faces = []
    for i in range(resolution - 1):
        for j in range(resolution - 1):
            p00 = i * resolution + j
            p01 = i * resolution + (j + 1)
            p10 = (i + 1) * resolution + j
            p11 = (i + 1) * resolution + (j + 1)
            faces.append([p00, p10, p11])
            faces.append([p00, p11, p01])

    return vertices.astype(np.float32), np.array(faces, dtype=np.int32)


def _compute_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """计算逐顶点法线（加权平均相邻面法线）。"""
    vertices = vertices.astype(np.float64)
    faces = faces.astype(np.int64)

    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]

    fn = np.cross(v1 - v0, v2 - v0)
    norms = np.linalg.norm(fn, axis=1, keepdims=True)
    norms[norms == 0] = 1
    fn = fn / norms

    normals = np.zeros_like(vertices)
    for i in range(3):
        np.add.at(normals, faces[:, i], fn)

    n_norms = np.linalg.norm(normals, axis=1, keepdims=True)
    n_norms[n_norms == 0] = 1
    return (normals / n_norms).astype(np.float32)


def build_mesh(parsed: dict, params: Optional[dict] = None, view_radius: float = 12.0, lod: bool = False):
    """根据解析结果生成网格。

    Args:
        parsed: parse_equation / parse_for_api 的返回值
        params: 参数字典
        view_radius: 当前视图半径，决定生成范围
        lod: 是否使用低细节层次（快速预览）

    Returns:
        dict: {
            "vertices": float32 array (N, 3),
            "faces": int32 array (M, 3),
            "normals": float32 array (N, 3),
            "geom_type": str,
            "bounds": {"min": [...], "max": [...]},
        }
        生成失败返回 None。
    """
    params = params or {}
    expr = parsed["expr"]
    syms = parsed["syms"]
    sd = {str(s): s for s in syms}

    # 用参数值替换符号
    subs = {sp.Symbol(k): v for k, v in params.items() if k in [str(s) for s in expr.free_symbols]}
    if subs:
        expr = expr.subs(subs)

    # 建立坐标顺序
    args = [None, None, None]
    for nm, i in [("x", 0), ("y", 1), ("z", 2)]:
        if nm in sd:
            args[i] = sd[nm]
    remaining = [s for s in syms if str(s) not in ("x", "y", "z")]
    for i in range(3):
        if args[i] is None:
            args[i] = remaining.pop(0) if remaining else sp.Symbol(f"_d{i}")

    geom_type = "general"

    # 1. 显式曲面 z=f(x,y) / y=f(x,z) / x=f(y,z)
    if all(k in sd for k in ("x", "y", "z")):
        limit = view_radius * 0.6
        res_uv = 64 if lod else int(max(96, min(180, 260 / max(0.5, view_radius))))

        uv = np.linspace(-limit, limit, res_uv)
        U, V = np.meshgrid(uv, uv, indexing="ij")

        coord_order = [sd["z"], sd["y"], sd["x"]]  # 优先 z=f(x,y)
        for dep_sym in coord_order:
            if dep_sym not in expr.free_symbols:
                continue
            try:
                sols = sp.solve(sp.Eq(expr, 0), dep_sym, dict=False)
            except Exception:
                continue
            if len(sols) != 1:
                continue

            sol_expr = sp.simplify(sols[0])
            indep_syms = [s for s in (sd["x"], sd["y"], sd["z"]) if s != dep_sym]

            try:
                f2 = sp.lambdify(indep_syms, sol_expr, modules=["numpy"])
                with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
                    W = np.asarray(f2(U, V), dtype=float)
                if W.shape != U.shape:
                    W = np.full(U.shape, float(np.ravel(W)[0]))

                valid = np.isfinite(W)
                if not np.any(valid):
                    continue

                # 局部重采样优化
                valid_ratio = float(np.count_nonzero(valid)) / float(valid.size)
                if valid_ratio < 0.35:
                    u_valid = U[valid]
                    v_valid = V[valid]
                    umin, umax = float(np.min(u_valid)), float(np.max(u_valid))
                    vmin, vmax = float(np.min(v_valid)), float(np.max(v_valid))
                    span_u = max(umax - umin, 1e-6)
                    span_v = max(vmax - vmin, 1e-6)
                    pad_u = max(span_u * 0.08, (2.0 * limit) / max(8, res_uv - 1))
                    pad_v = max(span_v * 0.08, (2.0 * limit) / max(8, res_uv - 1))
                    umin = max(-limit, umin - pad_u)
                    umax = min(limit, umax + pad_u)
                    vmin = max(-limit, vmin - pad_v)
                    vmax = min(limit, vmax + pad_v)

                    zoom_u = (2.0 * limit) / max(umax - umin, 1e-6)
                    zoom_v = (2.0 * limit) / max(vmax - vmin, 1e-6)
                    res_local = int(min(260, max(res_uv + 24, res_uv * max(zoom_u, zoom_v))))
                    u2 = np.linspace(umin, umax, res_local)
                    v2 = np.linspace(vmin, vmax, res_local)
                    U2, V2 = np.meshgrid(u2, v2, indexing="ij")
                    try:
                        with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
                            W2 = np.asarray(f2(U2, V2), dtype=float)
                        if W2.shape != U2.shape:
                            W2 = np.full(U2.shape, float(np.ravel(W2)[0]))
                        valid2 = np.isfinite(W2)
                        if np.any(valid2):
                            U, V, W, valid = U2, V2, W2, valid2
                    except Exception:
                        pass

                coord_map = {
                    str(indep_syms[0]): U,
                    str(indep_syms[1]): V,
                    str(dep_sym): W,
                }
                X = coord_map["x"]
                Y = coord_map["y"]
                Z = coord_map["z"]

                points = np.column_stack((X.ravel(), Y.ravel(), Z.ravel()))
                valid_flat = valid.ravel()
                valid_count = int(np.count_nonzero(valid_flat))
                if valid_count < 3:
                    continue

                idx_map = np.full(points.shape[0], -1, dtype=np.int_)
                idx_map[valid_flat] = np.arange(valid_count, dtype=np.int_)
                points_valid = points[valid_flat]

                faces = []
                n = U.shape[0]
                for i in range(n - 1):
                    row = i * n
                    next_row = (i + 1) * n
                    for j in range(n - 1):
                        p00 = row + j
                        p01 = row + j + 1
                        p10 = next_row + j
                        p11 = next_row + j + 1

                        i00 = idx_map[p00]
                        i01 = idx_map[p01]
                        i10 = idx_map[p10]
                        i11 = idx_map[p11]

                        if i00 >= 0 and i10 >= 0 and i11 >= 0:
                            faces.extend([i00, i10, i11])
                        if i00 >= 0 and i11 >= 0 and i01 >= 0:
                            faces.extend([i00, i11, i01])

                if not faces:
                    continue

                vertices = points_valid.astype(np.float32)
                faces = np.array(faces, dtype=np.int32).reshape(-1, 3)
                normals = _compute_normals(vertices, faces)
                geom_type = "explicit"
                return _build_result(vertices, faces, normals, geom_type)

            except Exception:
                continue

    if all(a is not None for a in args):
        try:
            x_s, y_s, z_s = args
            poly = sp.Poly(expr, x_s, y_s, z_s)

            if poly.total_degree() == 2:
                coeffs = poly.coeffs()
                monoms = poly.monoms()
                c_map = {m: c for m, c in zip(monoms, coeffs)}

                A = float(c_map.get((2, 0, 0), 0))
                B = float(c_map.get((0, 2, 0), 0))
                C = float(c_map.get((0, 0, 2), 0))
                D = float(c_map.get((1, 0, 0), 0))
                E = float(c_map.get((0, 1, 0), 0))
                F = float(c_map.get((0, 0, 1), 0))
                G = float(c_map.get((0, 0, 0), 0))
                eps = 1e-7

                # 2. 球体检测
                is_sphere = (
                    abs(A) > eps
                    and abs(A - B) < eps
                    and abs(A - C) < eps
                    and abs(float(c_map.get((1, 1, 0), 0))) < eps
                    and abs(float(c_map.get((1, 0, 1), 0))) < eps
                    and abs(float(c_map.get((0, 1, 1), 0))) < eps
                )
                if is_sphere:
                    cx = -D / (2.0 * A)
                    cy = -E / (2.0 * A)
                    cz = -F / (2.0 * A)
                    r_sq = cx ** 2 + cy ** 2 + cz ** 2 - G / A
                    if r_sq > 0:
                        radius = math.sqrt(r_sq)
                        res = 48 if lod else 80
                        vertices, faces = _sphere_triangulation(cx, cy, cz, radius, res)
                        normals = _compute_normals(vertices, faces)
                        geom_type = "sphere"
                        return _build_result(vertices, faces, normals, geom_type)

                # 3. 圆柱体检测（轴线对齐）
                has_x = any(m[0] > 0 for m in monoms)
                has_y = any(m[1] > 0 for m in monoms)
                has_z = any(m[2] > 0 for m in monoms)
                missing_vars = []
                if not has_x:
                    missing_vars.append("x")
                if not has_y:
                    missing_vars.append("y")
                if not has_z:
                    missing_vars.append("z")

                if len(missing_vars) == 1:
                    axis = missing_vars[0]
                    if axis == "z":
                        A2, B2, D2, E2, G2, cross = A, B, D, E, G, float(c_map.get((1, 1, 0), 0))
                    elif axis == "y":
                        A2, B2, D2, E2, G2, cross = A, C, D, F, G, float(c_map.get((1, 0, 1), 0))
                    else:  # x
                        A2, B2, D2, E2, G2, cross = B, C, E, F, G, float(c_map.get((0, 1, 1), 0))

                    if abs(A2) > eps and abs(A2 - B2) < eps and abs(cross) < eps:
                        c1 = -D2 / (2.0 * A2)
                        c2 = -E2 / (2.0 * A2)
                        r_sq = c1 ** 2 + c2 ** 2 - G2 / A2
                        if r_sq > 0:
                            radius = math.sqrt(r_sq)
                            height = min(view_radius * 2.2, 40.0)
                            if axis == "z":
                                center = [c1, c2, 0.0]
                                direction = [0.0, 0.0, 1.0]
                            elif axis == "y":
                                center = [c1, 0.0, c2]
                                direction = [0.0, 1.0, 0.0]
                            else:
                                center = [0.0, c1, c2]
                                direction = [1.0, 0.0, 0.0]
                            res = 32 if lod else 48
                            vertices, faces = _cylinder_triangulation(center, direction, radius, height, res)
                            normals = _compute_normals(vertices, faces)
                            geom_type = "cylinder"
                            return _build_result(vertices, faces, normals, geom_type)

                # 4. 平面检测
                is_plane = not (A == 0 and B == 0 and C == 0) and poly.total_degree() == 1

            # 单独处理一次方程（平面）
            if poly.total_degree() == 1:
                coeffs = poly.coeffs()
                monoms = poly.monoms()
                c_map = {m: c for m, c in zip(monoms, coeffs)}
                A = float(c_map.get((1, 0, 0), 0))
                B = float(c_map.get((0, 1, 0), 0))
                C = float(c_map.get((0, 0, 1), 0))
                D = float(c_map.get((0, 0, 0), 0))

                if not (A == 0 and B == 0 and C == 0):
                    normal = np.array([A, B, C], dtype=float)
                    n_sq = np.dot(normal, normal)
                    if n_sq > 1e-9:
                        origin = np.array([0.0, 0.0, 0.0])
                        val0 = np.dot(normal, origin) + D
                        center = origin - (val0 / n_sq) * normal
                        center += np.random.uniform(-1e-5, 1e-5, 3)  # 避免数值退化
                        size = max(6.0, min(view_radius * 1.8, 40.0))
                        vertices, faces = _plane_triangulation(normal, center, size, resolution=2)
                        normals = _compute_normals(vertices, faces)
                        geom_type = "plane"
                        return _build_result(vertices, faces, normals, geom_type)
        except Exception:
            pass

    # 6. Marching Cubes 通用回退
    try:
        f = sp.lambdify(args, expr, modules=["numpy"])
    except Exception:
        return None

    limit = view_radius * 0.6
    RES = 22 if lod else int(max(48, min(100, 180 / max(0.5, view_radius))))
    lin = np.linspace(-limit, limit, RES)
    X, Y, Z = np.meshgrid(lin, lin, lin, indexing="ij")

    try:
        with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
            V = np.asarray(f(X, Y, Z), dtype=float)
        if V.shape != X.shape:
            V = np.full(X.shape, float(V.flat[0]))
    except Exception:
        return None

    finite_mask = np.isfinite(V)
    if not np.any(finite_mask):
        return None

    finite_values = V[finite_mask]
    vmin = float(np.min(finite_values))
    vmax = float(np.max(finite_values))
    if not (vmin < 0 < vmax):
        return None

    fill_value = max(abs(vmin), abs(vmax), 1.0) + 1.0
    V = np.where(finite_mask, V, fill_value)

    try:
        sp_step = (limit * 2.0) / (RES - 1)
        verts, faces, _, _ = marching_cubes(V, level=0, spacing=(sp_step,) * 3)
        verts += -limit
    except Exception:
        return None

    if len(verts) == 0 or len(faces) == 0:
        return None

    vertices = verts.astype(np.float32)
    faces = faces.astype(np.int32)
    normals = _compute_normals(vertices, faces)
    geom_type = "general"
    return _build_result(vertices, faces, normals, geom_type)


def _build_result(vertices: np.ndarray, faces: np.ndarray, normals: np.ndarray, geom_type: str):
    """统一打包生成结果。"""
    return {
        "vertices": vertices,
        "faces": faces,
        "normals": normals,
        "geom_type": geom_type,
        "bounds": {
            "min": vertices.min(axis=0).tolist(),
            "max": vertices.max(axis=0).tolist(),
        },
    }
