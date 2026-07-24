from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import threading
import webbrowser
from functools import lru_cache
from pathlib import Path
from typing import Any

import mesh_service
from services.color_utils import generate_random_color, PALETTE_3D
from services.formula_formatter import sympy_to_label, sympy_to_rich_label


BASE_DIR = Path(__file__).resolve().parent
UI_INDEX = BASE_DIR / "webapp_index.html"
UI_JS = BASE_DIR / "webapp_app.js"
UI_CSS = BASE_DIR / "webapp_styles.css"


def _ensure_runtime_deps() -> None:
    missing: list[str] = []
    for module_name, pkg_name in (
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("pydantic", "pydantic"),
    ):
        try:
            __import__(module_name)
        except ModuleNotFoundError:
            missing.append(pkg_name)

    if not missing:
        return

    req_file = BASE_DIR / "requirements_api.txt"
    if req_file.exists():
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req_file)])
    else:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


_ensure_runtime_deps()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


app = FastAPI(title="Math Mesh API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class ParseRequest(BaseModel):
    equation: str = Field(..., description="Equation text, e.g. z=sqrt(4-x^2-y^2)")
    params: dict[str, float] = Field(default_factory=dict)


class MeshRequest(BaseModel):
    equation: str = Field(..., description="Equation text")
    params: dict[str, float] = Field(default_factory=dict)
    view_radius: float = Field(default=10.0, ge=0.05, le=500.0)
    lod: bool = Field(default=False)
    quality: int = Field(default=1, ge=1, le=3)  # 默认标准画质，降低首屏卡顿


# ── LRU 缓存层 ──────────────────────────────────────────────────────────────
#
# 为 /api/mesh 端点添加两层缓存：
#   1. `_parse_cached`  — 缓存 SymPy 解析结果（方程文本 → parsed dict）
#   2. `_mesh_cached`   — 缓存完整网格计算结果，key = (equation, params_json,
#                          view_radius, lod, quality)
#
# 两层分离是因为 parsed dict 包含 SymPy 表达式（不可 JSON 序列化），
# 但可作为 dict 键用于 lru_cache；而完整 mesh 结果必须 JSON 可序列化
# 才能跨请求返回。


@lru_cache(maxsize=256)
def _parse_cached(equation: str, params_json: str):
    """解析方程并缓存结果。params_json 为 json.dumps(sorted(params.items()))。"""
    params = json.loads(params_json) if params_json else {}
    return mesh_service.parse_for_api(equation, params)


@lru_cache(maxsize=256)
def _mesh_cached(
    equation: str,
    params_json: str,
    view_radius: float,
    lod: bool,
    quality: int,
):
    """完整网格计算 + 缓存。params_json 为 json.dumps(sorted(params.items()))。"""
    params = json.loads(params_json) if params_json else {}
    parsed, missing = _parse_cached(equation, params_json)
    if parsed is None:
        return {
            "ok": True,
            "mesh": {"vertices": [], "faces": [], "meta": {"empty": True, "reason": "not_geometric"}},
            "missing_params_filled": [],
        }

    params = dict(params)
    for name in missing:
        params[name] = 1.0

    # 更新 params_json 以反映补全后的参数
    params_json_filled = json.dumps(sorted(params.items()), sort_keys=True)
    # 重新构造 cache key（使用补全后的 params）
    return _mesh_cached_impl(equation, params_json_filled, view_radius, lod, quality)


@lru_cache(maxsize=256)
def _mesh_cached_impl(
    equation: str,
    params_json: str,
    view_radius: float,
    lod: bool,
    quality: int,
):
    """实际执行网格计算（不含参数补全逻辑）。"""
    params = json.loads(params_json) if params_json else {}
    parsed, missing = mesh_service.parse_for_api(equation, params)
    if parsed is None:
        return {
            "ok": True,
            "mesh": {"vertices": [], "faces": [], "meta": {"empty": True, "reason": "not_geometric"}},
            "missing_params_filled": missing,
        }
    mesh = mesh_service.build_isosurface_data(
        parsed=parsed,
        params=params,
        view_radius=view_radius,
        lod=lod,
        quality=quality,
    )
    return {"ok": True, "mesh": mesh, "missing_params_filled": missing}



class MeshLODRequest(BaseModel):
    equation: str = Field(..., description="Equation text")
    params: dict[str, float] = Field(default_factory=dict)
    view_radius: float = Field(default=10.0, ge=0.05, le=500.0)
    quality: int = Field(default=1, ge=1, le=3, description="1=low, 2=medium, 3=high")


class IntersectionsRequest(BaseModel):
    equations: list[str] = Field(default_factory=list, description="Equation list")
    params: dict[str, float] = Field(default_factory=dict)
    view_radius: float = Field(default=12.0, ge=0.05, le=500.0)
    lod: bool = Field(default=False)
    quality: int = Field(default=1, ge=1, le=3)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def page_root() -> FileResponse:
    if not UI_INDEX.exists():
        raise HTTPException(status_code=404, detail=f"Missing UI file: {UI_INDEX.name}")
    return FileResponse(UI_INDEX, headers=NO_CACHE_HEADERS)


@app.get("/app.js")
def page_js() -> FileResponse:
    if not UI_JS.exists():
        raise HTTPException(status_code=404, detail=f"Missing UI file: {UI_JS.name}")
    return FileResponse(UI_JS, media_type="application/javascript", headers=NO_CACHE_HEADERS)


@app.get("/webapp_app.js")
def page_js_v2() -> FileResponse:
    if not UI_JS.exists():
        raise HTTPException(status_code=404, detail=f"Missing UI file: {UI_JS.name}")
    return FileResponse(UI_JS, media_type="application/javascript", headers=NO_CACHE_HEADERS)


@app.get("/styles.css")
def page_css() -> FileResponse:
    if not UI_CSS.exists():
        raise HTTPException(status_code=404, detail=f"Missing UI file: {UI_CSS.name}")
    return FileResponse(UI_CSS, media_type="text/css", headers=NO_CACHE_HEADERS)


@app.get("/webapp_styles.css")
def page_css_v2() -> FileResponse:
    if not UI_CSS.exists():
        raise HTTPException(status_code=404, detail=f"Missing UI file: {UI_CSS.name}")
    return FileResponse(UI_CSS, media_type="text/css", headers=NO_CACHE_HEADERS)


@app.get("/webapp_mesh_worker.js")
def page_worker() -> FileResponse:
    worker_path = BASE_DIR / "webapp_mesh_worker.js"
    if not worker_path.exists():
        raise HTTPException(status_code=404, detail="Missing UI file: webapp_mesh_worker.js")
    return FileResponse(worker_path, media_type="application/javascript", headers=NO_CACHE_HEADERS)


@app.post("/api/parse")
def parse_equation(req: ParseRequest) -> dict[str, Any]:
    try:
        parsed, missing = mesh_service.parse_for_api(req.equation, req.params)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Parse failed: {exc}") from exc

    if parsed is None:
        return {"ok": True, "parsed": None, "missing_params": [], "params_needed": [], "label": ""}

    all_symbols = sorted([str(s) for s in parsed["expr"].free_symbols])
    params_needed = [name for name in all_symbols if name not in ("x", "y", "z")]
    label = sympy_to_label(req.equation)

    return {
        "ok": True,
        "parsed": {
            "raw": parsed["raw"],
            "sym_names": parsed["sym_names"],
            "dims": parsed["dims"],
        },
        "missing_params": missing,
        "params_needed": params_needed,
        "label": label,
    }


@app.post("/api/mesh")
def build_mesh(req: MeshRequest) -> dict[str, Any]:
    # 构建缓存键
    params_json = json.dumps(sorted(req.params.items()), sort_keys=True)

    try:
        result = _mesh_cached(
            equation=req.equation,
            params_json=params_json,
            view_radius=req.view_radius,
            lod=req.lod,
            quality=req.quality,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Mesh generation failed: {exc}") from exc

    return result


@app.post("/api/mesh_lod")
def build_mesh_lod(req: MeshLODRequest) -> dict[str, Any]:
    """Build mesh with LOD (Level of Detail) support.
    
    quality=1: Low resolution (resolution ~30)
    quality=3: High resolution (resolution ~60)
    """
    try:
        parsed, missing = mesh_service.parse_for_api(req.equation, req.params)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Parse failed: {exc}") from exc

    if parsed is None:
        return {
            "ok": True,
            "mesh": {"vertices": [], "faces": [], "meta": {"empty": True, "reason": "not_geometric"}},
            "missing_params_filled": [],
        }

    params = dict(req.params)
    for name in missing:
        params[name] = 1.0

    # 根据质量级别调整分辨率
    # quality=1 -> 低模 (resolution ~30)
    # quality=3 -> 高模 (resolution ~60)
    quality = int(max(1, min(3, req.quality)))
    
    try:
        mesh = mesh_service.build_isosurface_data(
            parsed=parsed,
            params=params,
            view_radius=req.view_radius,
            lod=True,  # Enable LOD optimizations
            quality=quality,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Mesh generation failed: {exc}") from exc

    return {"ok": True, "mesh": mesh, "missing_params_filled": missing, "quality": quality}


@app.post("/api/format")
def format_equation(req: dict[str, str]) -> dict[str, str]:
    """返回 Unicode 美化标签（与 /api/label 等价）。"""
    return {"label": sympy_to_label(req.get("equation", ""))}


@app.post("/api/intersections")
def build_intersections(req: IntersectionsRequest) -> dict[str, Any]:
    try:
        curves = mesh_service.build_intersections_data(
            equations=req.equations,
            params=req.params,
            view_radius=req.view_radius,
            lod=req.lod,
            quality=req.quality,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Intersection generation failed: {exc}") from exc

    return {"ok": True, "curves": curves}


@app.post("/api/color")
def pick_color(req: list[str]) -> dict[str, str]:
    """根据已有颜色列表，生成一个与现有颜色区分度较高的新颜色。"""
    return {"color": generate_random_color(req)}


@app.post("/api/label")
def format_label(req: dict[str, str]) -> dict[str, str]:
    """将方程文本转换为 Unicode 美化标签。"""
    return {"label": sympy_to_label(req.get("equation", ""))}


@app.post("/api/rich_label")
def format_rich_label(req: dict[str, str]) -> dict[str, str]:
    """Unicode 美化标签别名（兼容旧调用）。"""
    return {"label": sympy_to_rich_label(req.get("equation", ""))}


def _pick_port(host: str, preferred: int, span: int = 30) -> int:
    for port in range(preferred, preferred + span):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    raise RuntimeError(f"没有可用端口，尝试范围: {preferred}..{preferred + span - 1}")


if __name__ == "__main__":
    import uvicorn

    host = "127.0.0.1"
    port = _pick_port(host, 8000)
    url = f"http://{host}:{port}"
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"[启动] 服务地址: {url}")
    uvicorn.run(app, host=host, port=port, reload=False)
