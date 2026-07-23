from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

import mesh_service
from services.color_utils import generate_random_color, PALETTE_3D
from services.formula_formatter import sympy_to_label, sympy_to_rich_label


BASE_DIR = Path(__file__).resolve().parent
UI_INDEX = BASE_DIR / "index.html"
UI_JS = BASE_DIR / "app.js"
UI_CSS = BASE_DIR / "styles.css"


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


@app.get("/styles.css")
def page_css() -> FileResponse:
    if not UI_CSS.exists():
        raise HTTPException(status_code=404, detail=f"Missing UI file: {UI_CSS.name}")
    return FileResponse(UI_CSS, media_type="text/css", headers=NO_CACHE_HEADERS)


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

    try:
        mesh = mesh_service.build_isosurface_data(
            parsed=parsed,
            params=params,
            view_radius=req.view_radius,
            lod=req.lod,
            quality=req.quality,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Mesh generation failed: {exc}") from exc

    return {"ok": True, "mesh": mesh, "missing_params_filled": missing}


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
