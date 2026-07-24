"""网格生成服务。

保留原有 API 兼容，内部逐步迁移到 services/ 下的解析几何优先实现。
"""
from __future__ import annotations

import re
from itertools import combinations
from typing import Optional

import numpy as np
import sympy as sp
from skimage.measure import marching_cubes

from services import cache as mesh_cache
from services import mesh_generator as analytic_generator
from services.equation_parser import parse_for_api as _enhanced_parse


XYZ_COORDS = (sp.Symbol("x"), sp.Symbol("y"), sp.Symbol("z"))
XYZ_SET = set(XYZ_COORDS)
TRI_EDGES = ((0, 1), (1, 2), (2, 0))

# 是否启用解析几何优先路径（来自 EMath3DVisualizer）
_USE_ANALYTIC = True
# 是否启用网格缓存
_USE_CACHE = True


def normalize_and_build_expr(eq_text: str):
    """兼容旧版：将输入文本标准化并转换为 SymPy 表达式，返回 (expr, normalized_text)。"""
    text = eq_text.replace("√", "sqrt").replace("²", "**2").replace("³", "**3")
    text = re.sub(r'\|([^|]+?)\|', r'abs(\1)', text)
    text = text.replace("ln(", "log(")

    # 保护含坐标字母的函数名 exp（同 equation_parser.normalize_equation_text）
    text = text.replace("exp", "\x00")

    # 函数名后加空格，避免 "sin(" 被误判为 sin*(...)
    funcs = [
        "sin", "cos", "tan", "sqrt", "log", "abs",
        "asin", "acos", "atan", "sinh", "cosh", "tanh", "floor", "ceil",
    ]
    for f_name in funcs:
        text = text.replace(f"{f_name}(", f"{f_name} (")

    text = re.sub(r"(\d)([a-zA-Z])", r"\1*\2", text)
    text = re.sub(r"([a-zA-Z])([xyz])", r"\1*\2", text, flags=re.IGNORECASE)
    text = re.sub(r"([xyz])([a-zA-Z])", r"\1*\2", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[a-zA-Z0-9])(?=\()", "*", text)
    text = re.sub(r"(?<=\))(?=[a-zA-Z0-9])", "*", text)
    text = re.sub(r"(\d)\x00", lambda m: m.group(1) + "*" + "\x00", text)
    text = re.sub(r"([a-zA-Z])\x00", lambda m: m.group(1) + "*" + "\x00", text)
    text = text.replace("\x00", "exp")

    normalized = text.replace("^", "**")
    ops = ["==", "!=", ">=", "<=", ">", "<", "="]
    found_op = None
    for op in ops:
        if op in normalized:
            found_op = op
            break

    if found_op:
        lhs, rhs = normalized.split(found_op, 1)
        expr = sp.sympify(f"({lhs})-({rhs})")
    else:
        expr = sp.sympify(normalized)
    return expr, normalized


def parse_equation(eq_text: str, params: dict, add_param_callback=None):
    """兼容旧版：解析方程并返回绘制所需字典。"""
    parsed, _ = _enhanced_parse(eq_text, params)
    return parsed


def parse_for_api(eq_text: str, params: Optional[dict] = None):
    """API 场景解析：返回 parsed 结构与缺失参数列表。"""
    return _enhanced_parse(eq_text, params)


def build_isosurface(canvas_ctx, parsed):
    """兼容旧入口。"""
    return canvas_ctx._build_isosurface_core(parsed)


def _substitute_params(expr: sp.Expr, params: dict) -> sp.Expr:
    if not params:
        return expr
    subs = {sp.Symbol(k): float(v) for k, v in params.items()}
    return expr.subs(subs)


def _build_xyz_lambdify(expr: sp.Expr):
    x_sym, y_sym, z_sym = XYZ_COORDS
    return sp.lambdify((x_sym, y_sym, z_sym), expr, modules=["numpy"])


def _safe_eval_field(fn, x, y, z):
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        values = np.asarray(fn(x, y, z), dtype=float)
    if values.shape != x.shape:
        values = np.full(x.shape, float(values.flat[0]), dtype=float)
    return values


def _grid_xyz(bounds_min, bounds_max, shape):
    x_lin = np.linspace(bounds_min[0], bounds_max[0], int(shape[0]), dtype=float)
    y_lin = np.linspace(bounds_min[1], bounds_max[1], int(shape[1]), dtype=float)
    z_lin = np.linspace(bounds_min[2], bounds_max[2], int(shape[2]), dtype=float)
    x, y, z = np.meshgrid(x_lin, y_lin, z_lin, indexing="ij")
    return x, y, z


def _finite_zero_crossing(values):
    finite_mask = np.isfinite(values)
    if not np.any(finite_mask):
        return False, finite_mask, 0.0, 0.0
    finite_values = values[finite_mask]
    vmin = float(np.min(finite_values))
    vmax = float(np.max(finite_values))
    return (vmin < 0.0 < vmax), finite_mask, vmin, vmax


def _fill_non_finite(values, finite_mask, vmin, vmax):
    fill_value = max(abs(vmin), abs(vmax), 1.0) + 1.0
    return np.where(finite_mask, values, fill_value)


def _marching(values, bounds_min, bounds_max):
    shape = values.shape
    spacing = (
        (bounds_max[0] - bounds_min[0]) / max(1, shape[0] - 1),
        (bounds_max[1] - bounds_min[1]) / max(1, shape[1] - 1),
        (bounds_max[2] - bounds_min[2]) / max(1, shape[2] - 1),
    )
    verts, faces, _, _ = marching_cubes(values, level=0.0, spacing=spacing)
    verts[:, 0] += bounds_min[0]
    verts[:, 1] += bounds_min[1]
    verts[:, 2] += bounds_min[2]
    return verts.astype(float), faces.astype(int)


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
    
    quality = int(max(1, min(3, int(quality))))
    coarse_res = {1: 36, 2: 48, 3: 60}[quality]
    fine_res = {1: 72, 2: 96, 3: 120}[quality]
    max_voxels = {1: 1_000_000, 2: 2_000_000, 3: 3_000_000}[quality]
    radius_factor = max(0.75, min(1.25, 10.0 / max(6.0, float(view_radius))))
    fine_res = int(round(fine_res * radius_factor))
    fine_res = max(64, min(160, fine_res))
    return coarse_res, fine_res, max_voxels


def _focused_bbox_from_coarse(coarse_verts, global_limit):
    mins = np.min(coarse_verts, axis=0)
    maxs = np.max(coarse_verts, axis=0)
    span = np.maximum(maxs - mins, 1e-6)
    margin = max(0.25, 0.12 * float(np.max(span)))
    bmin = np.maximum(mins - margin, -global_limit)
    bmax = np.minimum(maxs + margin, global_limit)
    if np.any(bmax <= bmin):
        bmin = np.array([-global_limit, -global_limit, -global_limit], dtype=float)
        bmax = np.array([global_limit, global_limit, global_limit], dtype=float)
    return bmin, bmax


def _anisotropic_shape(bounds_min, bounds_max, base_res, max_voxels):
    ext = np.maximum(bounds_max - bounds_min, 1e-6)
    longest = float(np.max(ext))
    shape = np.maximum(48, np.round(base_res * ext / longest)).astype(int)
    voxels = int(shape[0] * shape[1] * shape[2])
    if voxels > max_voxels:
        scale = (max_voxels / float(voxels)) ** (1.0 / 3.0)
        shape = np.maximum(40, np.floor(shape * scale)).astype(int)
    return int(shape[0]), int(shape[1]), int(shape[2])


def _build_vertex_neighbors(faces: np.ndarray, n_vertices: int):
    neighbors = [set() for _ in range(n_vertices)]
    for a, b, c in faces:
        a_i = int(a)
        b_i = int(b)
        c_i = int(c)
        neighbors[a_i].update((b_i, c_i))
        neighbors[b_i].update((a_i, c_i))
        neighbors[c_i].update((a_i, b_i))
    return [np.fromiter(nb, dtype=np.int32) for nb in neighbors]


def _laplacian_step(vertices: np.ndarray, neighbor_idx, alpha: float):
    out = vertices.copy()
    for i, nb in enumerate(neighbor_idx):
        if nb.size == 0:
            continue
        avg = np.mean(vertices[nb], axis=0)
        out[i] = vertices[i] + alpha * (avg - vertices[i])
    return out


def _taubin_smooth(vertices: np.ndarray, faces: np.ndarray, iterations: int):
    if iterations <= 0 or len(vertices) == 0:
        return vertices
    if len(vertices) > 500_000:
        return vertices
    neighbor_idx = _build_vertex_neighbors(faces, len(vertices))
    smoothed = vertices.copy()
    for _ in range(iterations):
        smoothed = _laplacian_step(smoothed, neighbor_idx, alpha=0.8)
        smoothed = _laplacian_step(smoothed, neighbor_idx, alpha=-0.81)
    return smoothed


def _filter_vertices_by_mask(verts: np.ndarray, faces: np.ndarray, valid_mask: np.ndarray):
    if len(verts) == 0 or len(faces) == 0:
        return verts, faces
    valid_indices = np.where(valid_mask)[0]
    if len(valid_indices) == 0:
        return np.array([]), np.array([])
    new_verts = verts[valid_indices]
    index_map = np.full(len(verts), -1, dtype=int)
    index_map[valid_indices] = np.arange(len(valid_indices))
    new_faces = []
    for face in faces:
        if len(face) < 3:
            continue
        if any(index_map[v] == -1 for v in face):
            continue
        new_faces.append([index_map[v] for v in face])
    return new_verts, np.array(new_faces, dtype=np.int32)


def _smooth_hemisphere_edge(verts: np.ndarray, faces: np.ndarray, hemisphere_type: str):
    if len(verts) == 0 or len(faces) == 0:
        return verts
    edge_threshold = 0.15 if hemisphere_type == "upper" else -0.15
    if hemisphere_type == "upper":
        edge_mask = verts[:, 2] <= edge_threshold
    else:
        edge_mask = verts[:, 2] >= edge_threshold
    edge_indices = np.where(edge_mask)[0]
    if len(edge_indices) == 0:
        return verts
    neighbor_idx = _build_vertex_neighbors(faces, len(verts))
    smoothed_verts = verts.copy()
    for _ in range(3):
        for idx in edge_indices:
            neighbors = neighbor_idx[idx]
            if len(neighbors) > 0:
                neighbor_avg = np.mean(smoothed_verts[neighbors], axis=0)
                smoothed_verts[idx] = 0.5 * smoothed_verts[idx] + 0.5 * neighbor_avg
    return smoothed_verts


def _recognize_geometry_type(expr: sp.Expr, params: dict):
    """识别几何类型，返回类型和参数"""
    expr_str = str(expr).replace(" ", "").lower()
    expr_str = expr_str.replace("√", "sqrt").replace("²", "**2")

    if "z=" in expr_str:
        rhs = expr_str.split("z=")[1]
        if "sqrt(" in rhs and "x**2" in rhs and "y**2" in rhs:
            import re as _re
            match = _re.search(r'sqrt\((\d+(?:\.\d+)?)-x\*\*2-y\*\*2\)', rhs)
            if match:
                radius = float(match.group(1))
                if radius > 0:
                    return "hemisphere", {"radius": radius, "hemisphere": "upper"}
        if "-sqrt(" in rhs and "x**2" in rhs and "y**2" in rhs:
            import re as _re
            match = _re.search(r'-sqrt\((\d+(?:\.\d+)?)-x\*\*2-y\*\*2\)', rhs)
            if match:
                radius = float(match.group(1))
                if radius > 0:
                    return "hemisphere", {"radius": radius, "hemisphere": "lower"}
        return "generic", {}

    expanded = sp.expand(expr)
    x_sym, y_sym, z_sym = XYZ_COORDS
    try:
        poly = sp.Poly(expanded, x_sym, y_sym, z_sym)
    except sp.PolynomialError:
        return "generic", {}

    coeffs = poly.as_dict()

    linear_terms = {(1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, 0)}
    if set(coeffs.keys()).issubset(linear_terms):
        try:
            a = float(sp.N(coeffs.get((1, 0, 0), 0)))
            b = float(sp.N(coeffs.get((0, 1, 0), 0)))
            c = float(sp.N(coeffs.get((0, 0, 1), 0)))
            d = float(sp.N(coeffs.get((0, 0, 0), 0)))
        except Exception:
            a = b = c = d = np.nan
        nrm = np.sqrt(a * a + b * b + c * c)
        if np.isfinite(nrm) and nrm > 1e-12 and np.isfinite(d):
            return "plane", {"a": a, "b": b, "c": c, "d": d}

    sphere_terms = {(2, 0, 0), (0, 2, 0), (0, 0, 2), (0, 0, 0)}
    if set(coeffs.keys()).issubset(sphere_terms):
        c_x2 = coeffs.get((2, 0, 0), 0)
        c_y2 = coeffs.get((0, 2, 0), 0)
        c_z2 = coeffs.get((0, 0, 2), 0)
        if c_x2 != 0 and sp.simplify(c_x2 - c_y2) == 0 and sp.simplify(c_x2 - c_z2) == 0:
            c0 = coeffs.get((0, 0, 0), 0)
            r2 = -sp.N(c0 / c_x2)
            if r2.is_real:
                r2f = float(r2)
                if np.isfinite(r2f) and r2f > 1e-12:
                    return "sphere", {"radius": float(np.sqrt(r2f))}

    cyl_terms = {(2, 0, 0), (0, 2, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0)}
    if set(coeffs.keys()).issubset(cyl_terms):
        c_x2 = coeffs.get((2, 0, 0), 0)
        c_y2 = coeffs.get((0, 2, 0), 0)
        if c_x2 != 0 and sp.simplify(c_x2 - c_y2) == 0:
            k = float(sp.N(c_x2))
            bx = float(sp.N(coeffs.get((1, 0, 0), 0)))
            by = float(sp.N(coeffs.get((0, 1, 0), 0)))
            c0 = float(sp.N(coeffs.get((0, 0, 0), 0)))
            if np.isfinite(k) and abs(k) > 1e-12 and np.isfinite(bx) and np.isfinite(by) and np.isfinite(c0):
                center_x = -bx / (2.0 * k)
                center_y = -by / (2.0 * k)
                r2 = (bx * bx + by * by) / (4.0 * k * k) - (c0 / k)
                if np.isfinite(r2) and r2 > 1e-12:
                    return "cylinder_z", {
                        "center_x": float(center_x),
                        "center_y": float(center_y),
                        "radius": float(np.sqrt(r2)),
                    }

    return "generic", {}


def _generate_parametric_mesh(geom_type: str, geom_data: dict, view_radius: float, quality: int):
    quality = int(max(1, min(3, int(quality))))

    if geom_type == "plane":
        a = float(geom_data.get("a", 0.0))
        b = float(geom_data.get("b", 0.0))
        c = float(geom_data.get("c", 0.0))
        d = float(geom_data.get("d", 0.0))
        normal = np.array([a, b, c], dtype=float)
        norm = float(np.linalg.norm(normal))
        if not np.isfinite(norm) or norm <= 1e-12:
            return None, None
        normal /= norm
        p0 = (-d / max(1e-12, (a * a + b * b + c * c))) * np.array([a, b, c], dtype=float)
        ref = np.array([0.0, 0.0, 1.0], dtype=float)
        if abs(float(np.dot(normal, ref))) > 0.95:
            ref = np.array([1.0, 0.0, 0.0], dtype=float)
        u = np.cross(normal, ref)
        u_norm = float(np.linalg.norm(u))
        if u_norm <= 1e-12:
            return None, None
        u /= u_norm
        v = np.cross(normal, u)
        v_norm = float(np.linalg.norm(v))
        if v_norm <= 1e-12:
            return None, None
        v /= v_norm
        half = max(2.0, float(view_radius) * 1.6)
        res = {1: 96, 2: 144, 3: 192}[quality]
        s = np.linspace(-half, half, res + 1, dtype=float)
        t = np.linspace(-half, half, res + 1, dtype=float)
        ss, tt = np.meshgrid(s, t, indexing="xy")
        pts = p0[None, None, :] + ss[..., None] * u[None, None, :] + tt[..., None] * v[None, None, :]
        verts = pts.reshape(-1, 3)
        faces = []
        row_n = res + 1
        for i in range(res):
            row = i * row_n
            next_row = (i + 1) * row_n
            for j in range(res):
                a0 = row + j
                b0 = row + j + 1
                c0 = next_row + j
                d0 = next_row + j + 1
                faces.append([a0, b0, c0])
                faces.append([b0, d0, c0])
        return verts, np.asarray(faces, dtype=np.int32)

    if geom_type == "sphere":
        radius = float(geom_data.get("radius", 1.0))
        if not np.isfinite(radius) or radius <= 0:
            return None, None
        res_u, res_v = {1: (128, 64), 2: (192, 96), 3: (256, 128)}[quality]
        u = np.linspace(0.0, 2.0 * np.pi, res_u, endpoint=False, dtype=float)
        v = np.linspace(0.0, np.pi, res_v + 1, dtype=float)
        uu, vv = np.meshgrid(u, v, indexing="xy")
        x = radius * np.sin(vv) * np.cos(uu)
        y = radius * np.sin(vv) * np.sin(uu)
        z = radius * np.cos(vv)
        verts = np.column_stack((x.ravel(), y.ravel(), z.ravel()))
        faces = []
        for i in range(res_v):
            row = i * res_u
            next_row = (i + 1) * res_u
            for j in range(res_u):
                jn = (j + 1) % res_u
                a = row + j
                b = row + jn
                c = next_row + j
                d = next_row + jn
                if i > 0:
                    faces.append([a, b, c])
                if i < res_v - 1:
                    faces.append([b, d, c])
        return verts, np.asarray(faces, dtype=np.int32)

    if geom_type == "cylinder_z":
        radius = float(geom_data.get("radius", 0.0))
        center_x = float(geom_data.get("center_x", 0.0))
        center_y = float(geom_data.get("center_y", 0.0))
        if not np.isfinite(radius) or radius <= 0:
            return None, None
        res_theta, res_h = {1: (160, 80), 2: (224, 112), 3: (320, 160)}[quality]
        half_h = max(2.0, float(view_radius) * 0.6)
        theta = np.linspace(0.0, 2.0 * np.pi, res_theta, endpoint=False, dtype=float)
        z = np.linspace(-half_h, half_h, res_h + 1, dtype=float)
        tt, zz = np.meshgrid(theta, z, indexing="xy")
        x = center_x + radius * np.cos(tt)
        y = center_y + radius * np.sin(tt)
        verts = np.column_stack((x.ravel(), y.ravel(), zz.ravel()))
        faces = []
        for i in range(res_h):
            row = i * res_theta
            next_row = (i + 1) * res_theta
            for j in range(res_theta):
                jn = (j + 1) % res_theta
                a = row + j
                b = row + jn
                c = next_row + j
                d = next_row + jn
                faces.append([a, b, c])
                faces.append([b, d, c])
        return verts, np.asarray(faces, dtype=np.int32)

    if geom_type == "hemisphere":
        radius = float(geom_data.get("radius", 1.0))
        hemisphere_type = geom_data.get("hemisphere", "upper")
        if not np.isfinite(radius) or radius <= 0:
            return None, None
        res_u, res_v = {1: (512, 160), 2: (768, 240), 3: (1024, 320)}[quality]
        u = np.linspace(0.0, 2.0 * np.pi, res_u, endpoint=False, dtype=float)
        if hemisphere_type == "upper":
            v = np.linspace(0.0, np.pi / 2.0, res_v + 1, dtype=float)
        else:
            v = np.linspace(np.pi / 2.0, np.pi, res_v + 1, dtype=float)
        uu, vv = np.meshgrid(u, v, indexing="xy")
        x = radius * np.sin(vv) * np.cos(uu)
        y = radius * np.sin(vv) * np.sin(uu)
        z = radius * np.cos(vv)
        verts = np.column_stack((x.ravel(), y.ravel(), z.ravel()))
        faces = []
        for i in range(res_v):
            row = i * res_u
            next_row = (i + 1) * res_u
            for j in range(res_u):
                jn = (j + 1) % res_u
                a = row + j
                b = row + jn
                c = next_row + j
                d = next_row + jn
                if i > 0:
                    faces.append([a, b, c])
                if i < res_v - 1:
                    faces.append([b, d, c])
        return verts, np.asarray(faces, dtype=np.int32)

    return None, None


def _detect_explicit_hemisphere(raw_eq: str, params: dict):
    normalized = str(raw_eq or "").replace(" ", "").lower()
    if "=" not in normalized:
        return None
    lhs, rhs = normalized.split("=", 1)
    if lhs != "z":
        return None
    try:
        rhs_expr = sp.sympify(rhs)
    except Exception:
        return None
    rhs_expr = _substitute_params(rhs_expr, params or {})
    rhs_expr = sp.simplify(rhs_expr)
    sqrt_expr = None
    sign = 1.0
    for factor in (1, -1):
        candidate = sp.simplify(rhs_expr * factor)
        if candidate.is_Pow and sp.simplify(candidate.exp - sp.Rational(1, 2)) == 0:
            sqrt_expr = candidate
            sign = float(factor)
            break
    if sqrt_expr is None:
        return None
    x_sym, y_sym, _ = XYZ_COORDS
    radicand = sp.expand(sqrt_expr.base)
    r2_expr = sp.simplify(radicand + x_sym**2 + y_sym**2)
    if r2_expr.free_symbols & XYZ_SET:
        return None
    try:
        r2 = float(sp.N(r2_expr))
    except Exception:
        return None
    if not np.isfinite(r2) or r2 <= 1e-12:
        return None
    return {"radius": float(np.sqrt(r2)), "hemisphere": "upper" if sign > 0 else "lower"}


def _fallback_marching_cubes(parsed, params, view_radius, lod, quality):
    """原 Marching Cubes 通用回退路径。"""
    expr = _substitute_params(parsed["expr"], params)
    field_fn = _build_xyz_lambdify(expr)
    limit = float(view_radius) * 0.6
    coarse_res, fine_base_res, max_voxels = _calc_resolutions(view_radius, quality, lod)
    global_min = np.array([-limit, -limit, -limit], dtype=float)
    global_max = np.array([limit, limit, limit], dtype=float)

    cx, cy, cz = _grid_xyz(global_min, global_max, (coarse_res, coarse_res, coarse_res))
    coarse_values = _safe_eval_field(field_fn, cx, cy, cz)
    crossed, finite_mask, vmin, vmax = _finite_zero_crossing(coarse_values)
    if not crossed:
        reason = "no_finite_values" if not np.any(finite_mask) else "no_zero_crossing"
        return {"vertices": [], "faces": [], "meta": {"empty": True, "reason": reason}}

    coarse_values = _fill_non_finite(coarse_values, finite_mask, vmin, vmax)
    try:
        coarse_verts, _ = _marching(coarse_values, global_min, global_max)
    except Exception:
        return {"vertices": [], "faces": [], "meta": {"empty": True, "reason": "coarse_marching_failed"}}

    if len(coarse_verts) == 0:
        return {"vertices": [], "faces": [], "meta": {"empty": True, "reason": "empty_mesh"}}

    fine_min, fine_max = _focused_bbox_from_coarse(coarse_verts, limit)
    fine_shape = _anisotropic_shape(fine_min, fine_max, fine_base_res, max_voxels)
    fx, fy, fz = _grid_xyz(fine_min, fine_max, fine_shape)
    fine_values = _safe_eval_field(field_fn, fx, fy, fz)
    crossed, finite_mask, vmin, vmax = _finite_zero_crossing(fine_values)
    if not crossed:
        reason = "no_finite_values" if not np.any(finite_mask) else "no_zero_crossing"
        return {"vertices": [], "faces": [], "meta": {"empty": True, "reason": reason}}

    fine_values = _fill_non_finite(fine_values, finite_mask, vmin, vmax)
    try:
        verts, faces = _marching(fine_values, fine_min, fine_max)
    except Exception:
        return {"vertices": [], "faces": [], "meta": {"empty": True, "reason": "fine_marching_failed"}}

    if len(verts) == 0 or len(faces) == 0:
        return {"vertices": [], "faces": [], "meta": {"empty": True, "reason": "empty_mesh"}}

    smooth_iters = {1: 5, 2: 10, 3: 15}[int(max(1, min(3, int(quality))))]
    verts = _taubin_smooth(verts, faces, iterations=smooth_iters)

    raw_eq = parsed.get("raw", "").replace(" ", "").lower()
    if "z=" in raw_eq:
        rhs = raw_eq.split("z=")[1]
        if "sqrt(" in rhs and "x**2" in rhs and "y**2" in rhs:
            valid_mask = verts[:, 2] >= -1e-6
            verts, faces = _filter_vertices_by_mask(verts, faces, valid_mask)
            if len(verts) > 0:
                verts = _smooth_hemisphere_edge(verts, faces, "upper")
        elif "-sqrt(" in rhs and "x**2" in rhs and "y**2" in rhs:
            valid_mask = verts[:, 2] <= 1e-6
            verts, faces = _filter_vertices_by_mask(verts, faces, valid_mask)
            if len(verts) > 0:
                verts = _smooth_hemisphere_edge(verts, faces, "lower")

    if len(verts) == 0 or len(faces) == 0:
        return {"vertices": [], "faces": [], "meta": {"empty": True, "reason": "domain_filtered_empty"}}

    return {
        "vertices": verts.astype(np.float32).tolist(),
        "faces": faces.astype(np.int32).tolist(),
        "meta": {
            "empty": False,
            "view_radius": float(view_radius),
            "lod": bool(lod),
            "quality": int(max(1, min(3, int(quality)))),
            "resolution": [int(fine_shape[0]), int(fine_shape[1]), int(fine_shape[2])],
            "bbox": [fine_min.tolist(), fine_max.tolist()],
        },
    }


def _result_from_analytic(result: dict):
    """将解析几何生成器的结果转换为 API 输出格式。"""
    return {
        "vertices": result["vertices"].astype(np.float32).tolist(),
        "faces": result["faces"].astype(np.int32).tolist(),
        "meta": {
            "empty": False,
            "view_radius": None,
            "geometry_type": result["geom_type"],
            "fast_path": True,
            "bounds": result["bounds"],
        },
    }


def build_isosurface_data(
    parsed: dict,
    params: Optional[dict] = None,
    view_radius: float = 10.0,
    lod: bool = False,
    quality: int = 1,
):
    """纯服务网格生成。

    优先走解析几何快速路径（球/圆柱/平面/圆锥/抛物面/显式曲面），
    识别失败则回退到 Marching Cubes。
    """
    params = params or {}

    # 防御：表达式里仍含未在 params 中提供的符号（除 x/y/z）时，
    # 用 1.0 填充，避免下游出现 "Cannot convert expression to float" 类崩溃。
    if parsed is not None:
        for _s in parsed["expr"].free_symbols:
            _name = str(_s)
            if _name not in ("x", "y", "z") and _name not in params:
                params = dict(params)
                params[_name] = 1.0

    # 1. 尝试缓存命中
    if _USE_CACHE:
        cached = mesh_cache.get([parsed.get("raw", "")], params, view_radius, lod)
        if cached is not None:
            return _result_from_analytic(cached)

    # 2. 解析几何优先路径
    if _USE_ANALYTIC:
        try:
            analytic_result = analytic_generator.build_mesh(
                parsed, params=params, view_radius=view_radius, lod=lod
            )
            if analytic_result is not None:
                if _USE_CACHE:
                    mesh_cache.put([parsed.get("raw", "")], params, view_radius, lod, analytic_result)
                return _result_from_analytic(analytic_result)
        except Exception:
            pass

    # 3. 原 Marching Cubes 回退
    result = _fallback_marching_cubes(parsed, params, view_radius, lod, quality)
    return result


# 保持 API 兼容：前端仍可能通过 `build_mesh` 调用旧入口
def build_mesh(parsed: dict, params: Optional[dict] = None, view_radius: float = 10.0, lod: bool = False, quality: int = 1):
    """兼容旧版命名。"""
    return build_isosurface_data(parsed, params, view_radius, lod, quality)


def _safe_eval_on_points(field_fn, points: np.ndarray):
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        values = np.asarray(field_fn(points[:, 0], points[:, 1], points[:, 2]), dtype=float)
    if values.shape != (points.shape[0],):
        values = np.full(points.shape[0], float(values.flat[0]), dtype=float)
    return values


def _extract_segments_from_mesh(verts: np.ndarray, faces: np.ndarray, other_field_fn, view_radius: float):
    if len(verts) == 0 or len(faces) == 0:
        return []

    g_vals = _safe_eval_on_points(other_field_fn, verts)
    finite = np.isfinite(g_vals)
    if not np.any(finite):
        return []

    finite_abs = np.abs(g_vals[finite])
    value_scale = float(np.median(finite_abs)) if finite_abs.size else 1.0
    zero_eps = max(1e-7, value_scale * 1e-6)
    pt_eps = max(1e-5, float(view_radius) * 1.5e-3)

    segments = []
    for tri in faces:
        ids = tri.astype(int)
        if not (finite[ids[0]] and finite[ids[1]] and finite[ids[2]]):
            continue
        vals = g_vals[ids]
        pts = verts[ids]

        cross_pts = []
        for a, b in TRI_EDGES:
            va = float(vals[a])
            vb = float(vals[b])
            pa = pts[a]
            pb = pts[b]

            if abs(va) <= zero_eps and abs(vb) <= zero_eps:
                continue
            if abs(va) <= zero_eps:
                cross_pts.append(pa)
                continue
            if abs(vb) <= zero_eps:
                cross_pts.append(pb)
                continue
            if va * vb < 0.0:
                t = va / (va - vb)
                t = max(0.0, min(1.0, t))
                cross_pts.append(pa + t * (pb - pa))

        unique_pts = []
        for p in cross_pts:
            if not any(np.linalg.norm(p - q) <= pt_eps * 1.2 for q in unique_pts):
                unique_pts.append(p)

        if len(unique_pts) < 2:
            continue
        if len(unique_pts) == 2:
            p1, p2 = unique_pts
        else:
            center = np.mean(pts, axis=0)
            best_pair = (unique_pts[0], unique_pts[1])
            best_score = -1.0
            for i in range(len(unique_pts)):
                for j in range(i + 1, len(unique_pts)):
                    p1 = unique_pts[i]
                    p2 = unique_pts[j]
                    dist = np.linalg.norm(np.cross(p2 - p1, p1 - center)) / (np.linalg.norm(p2 - p1) + 1e-9)
                    length = np.linalg.norm(p2 - p1)
                    score = length - dist * 2.0
                    if score > best_score:
                        best_score = score
                        best_pair = (p1, p2)
            p1, p2 = best_pair

        if np.linalg.norm(p1 - p2) > pt_eps * 0.1:
            segments.append((p1, p2))
    return segments


def _polyline_length(points: np.ndarray):
    if len(points) < 2:
        return 0.0
    return float(np.sum(np.linalg.norm(np.diff(points, axis=0), axis=1)))


def _build_polylines_from_segments(segments, snap_tol: float):
    if not segments:
        return []

    points = []
    edges = set()

    def _key(p):
        return tuple(np.round(np.asarray(p) / snap_tol).astype(np.int64).tolist())

    def _find_nearest_point(p):
        min_dist = float('inf')
        nearest_idx = None
        for i, existing_p in enumerate(points):
            dist = np.linalg.norm(p - existing_p)
            if dist <= snap_tol * 3.0 and dist < min_dist:
                min_dist = dist
                nearest_idx = i
        return nearest_idx

    def _idx_for_point(p):
        p_arr = np.asarray(p, dtype=float)
        key = _key(p_arr)
        idx = key_to_idx.get(key)
        if idx is not None:
            return idx
        nearest_idx = _find_nearest_point(p_arr)
        if nearest_idx is not None:
            return nearest_idx
        idx = len(points)
        points.append(p_arr)
        key_to_idx[key] = idx
        return idx

    key_to_idx = {}
    for p1, p2 in segments:
        i = _idx_for_point(p1)
        j = _idx_for_point(p2)
        if i == j:
            continue
        edge = (i, j) if i < j else (j, i)
        edges.add(edge)

    if not edges:
        return []

    adj = [set() for _ in range(len(points))]
    for i, j in edges:
        adj[i].add(j)
        adj[j].add(i)

    used = set()

    def _walk(start_idx):
        path = [start_idx]
        prev = -1
        cur = start_idx
        while True:
            next_idx = None
            for nb in adj[cur]:
                edge = (cur, nb) if cur < nb else (nb, cur)
                if edge in used:
                    continue
                if nb == prev and len(adj[cur]) > 1:
                    continue
                next_idx = nb
                break
            if next_idx is None:
                break
            edge = (cur, next_idx) if cur < next_idx else (next_idx, cur)
            used.add(edge)
            path.append(next_idx)
            prev, cur = cur, next_idx
            if cur == start_idx:
                break
        return path

    chains = []
    for node, neighbors in enumerate(adj):
        if len(neighbors) == 2:
            continue
        while True:
            has_unused = False
            for nb in neighbors:
                edge = (node, nb) if node < nb else (nb, node)
                if edge not in used:
                    has_unused = True
                    break
            if not has_unused:
                break
            idx_path = _walk(node)
            if len(idx_path) >= 2:
                chains.append(idx_path)

    for edge in edges:
        if edge in used:
            continue
        idx_path = _walk(edge[0])
        if len(idx_path) >= 2:
            chains.append(idx_path)

    polylines = []
    for idx_path in chains:
        pts = np.array([points[i] for i in idx_path], dtype=float)
        if len(pts) >= 2:
            polylines.append(pts)

    if len(polylines) > 1:
        polylines = _connect_broken_polylines(polylines, snap_tol * 2.5)
    return polylines


def _connect_broken_polylines(polylines: list, connect_tol: float):
    if len(polylines) <= 1:
        return polylines
    current_polylines = polylines.copy()
    max_iterations = 5

    for iteration in range(max_iterations):
        endpoints = []
        for i, poly in enumerate(current_polylines):
            if len(poly) >= 2:
                endpoints.append((i, 0, poly[0]))
                endpoints.append((i, -1, poly[-1]))

        potential_connections = []
        for i, (poly_i, end_i, pt_i) in enumerate(endpoints):
            for j, (poly_j, end_j, pt_j) in enumerate(endpoints[i + 1:], i + 1):
                if poly_i != poly_j:
                    dist = np.linalg.norm(pt_i - pt_j)
                    tolerance_multiplier = 1.5 + iteration * 0.5
                    if dist <= connect_tol * tolerance_multiplier:
                        potential_connections.append((dist, i, poly_i, end_i, pt_i, j, poly_j, end_j, pt_j))

        potential_connections.sort(key=lambda x: x[0])
        connections = []
        used = set()
        for conn in potential_connections:
            dist, i, poly_i, end_i, pt_i, j, poly_j, end_j, pt_j = conn
            if i not in used and j not in used:
                connections.append((poly_i, end_i, poly_j, end_j))
                used.add(i)
                used.add(j)

        if not connections:
            break

        merged = []
        merged_indices = set()
        for poly_i, end_i, poly_j, end_j in connections:
            if poly_i in merged_indices or poly_j in merged_indices:
                continue
            poly1 = current_polylines[poly_i]
            poly2 = current_polylines[poly_j]
            if end_i == 0 and end_j == 0:
                merged_poly = np.vstack([poly2[::-1], poly1])
            elif end_i == 0 and end_j == -1:
                merged_poly = np.vstack([poly2, poly1])
            elif end_i == -1 and end_j == 0:
                merged_poly = np.vstack([poly1, poly2])
            else:
                merged_poly = np.vstack([poly1, poly2[::-1]])
            if len(merged_poly) >= 3:
                merged.append(merged_poly)
                merged_indices.add(poly_i)
                merged_indices.add(poly_j)

        for i, poly in enumerate(current_polylines):
            if i not in merged_indices:
                merged.append(poly)

        if len(merged) >= len(current_polylines):
            break
        current_polylines = merged

    return current_polylines


def _smooth_polyline(points: np.ndarray, closed: bool, iterations: int, alpha: float = 0.45):
    if iterations <= 0 or len(points) < 3:
        return points
    sm = points.copy()
    n = len(sm)
    for _ in range(iterations):
        nxt = sm.copy()
        if closed:
            for i in range(n):
                prev_i = (i - 1) % n
                next_i = (i + 1) % n
                nxt[i] = sm[i] + alpha * ((sm[prev_i] + sm[next_i]) * 0.5 - sm[i])
        else:
            for i in range(1, n - 1):
                nxt[i] = sm[i] + alpha * ((sm[i - 1] + sm[i + 1]) * 0.5 - sm[i])
        sm = nxt
    return sm


def _resample_polyline(points: np.ndarray, closed: bool, target_step: float):
    if len(points) < 3:
        return points
    pts = points.copy()
    if closed and np.linalg.norm(pts[0] - pts[-1]) > 1e-10:
        pts = np.vstack([pts, pts[0]])
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    total = float(np.sum(seg))
    if total <= target_step * 2.0:
        return points
    samples = int(np.clip(round(total / target_step), 60, 900))
    d = np.concatenate(([0.0], np.cumsum(seg)))
    query = np.linspace(0.0, total, samples + (1 if closed else 0))
    out = np.empty((len(query), 3), dtype=float)
    for axis in range(3):
        out[:, axis] = np.interp(query, d, pts[:, axis])
    if closed and len(out) > 1:
        out = out[:-1]
    return out


def build_intersections_data(
    equations: list[str],
    params: Optional[dict] = None,
    view_radius: float = 12.0,
    lod: bool = False,
    quality: int = 1,
):
    params = params or {}
    clean_eqs = [str(e).strip() for e in (equations or []) if str(e).strip()]
    if len(clean_eqs) < 2:
        return []

    parsed_items = []
    for eq in clean_eqs:
        parsed, _ = parse_for_api(eq, params)
        parsed_items.append(parsed)

    meshes = []
    fields = []
    for parsed in parsed_items:
        if parsed is None:
            meshes.append(None)
            fields.append(None)
            continue
        expr = _substitute_params(parsed["expr"], params)
        fields.append(_build_xyz_lambdify(expr))
        mesh_data = build_isosurface_data(parsed, params, view_radius=view_radius, lod=lod, quality=quality)
        if mesh_data.get("meta", {}).get("empty", True):
            meshes.append(None)
            continue
        verts = np.asarray(mesh_data["vertices"], dtype=float)
        faces = np.asarray(mesh_data["faces"], dtype=np.int32)
        meshes.append((verts, faces))

    curves = []
    snap_tol = max(1e-5, float(view_radius) * 8e-4)
    smooth_iters = {1: 2, 2: 4, 3: 6}[int(max(1, min(3, int(quality))))]
    target_step = max(0.01, float(view_radius) * 0.004)
    min_length = max(0.04, float(view_radius) * 0.01)

    for i, j in combinations(range(len(clean_eqs)), 2):
        mesh_i = meshes[i]
        mesh_j = meshes[j]
        f_i = fields[i]
        f_j = fields[j]
        if mesh_i is None or mesh_j is None or f_i is None or f_j is None:
            continue

        segs = _extract_segments_from_mesh(mesh_i[0], mesh_i[1], f_j, view_radius)
        if not segs:
            segs = _extract_segments_from_mesh(mesh_j[0], mesh_j[1], f_i, view_radius)
        if not segs:
            continue

        poly_arrays = _build_polylines_from_segments(segs, snap_tol=snap_tol)
        polylines = []
        for poly in poly_arrays:
            if len(poly) < 3:
                continue
            closed = np.linalg.norm(poly[0] - poly[-1]) <= snap_tol * 2.5
            sm = _smooth_polyline(poly, closed=closed, iterations=smooth_iters)
            sm = _resample_polyline(sm, closed=closed, target_step=target_step)
            if _polyline_length(sm) < min_length:
                continue
            polylines.append(sm.astype(float).tolist())

        if polylines:
            curves.append({"a": int(i), "b": int(j), "polylines": polylines})

    return curves
