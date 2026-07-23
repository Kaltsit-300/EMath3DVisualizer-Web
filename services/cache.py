"""网格计算结果缓存。

简单 SQLite 持久化缓存，避免重复计算相同方程+参数+视图半径+LOD 的网格。
"""
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

CACHE_DIR = Path(__file__).parent.parent / "cache"
CACHE_DB = CACHE_DIR / "mesh_cache.db"


def _ensure_db():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mesh_cache (
            key TEXT PRIMARY KEY,
            geom_type TEXT,
            vertices BLOB,
            faces BLOB,
            normals BLOB,
            bounds TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def _make_key(equations: list, params: dict, view_radius: float, lod: bool) -> str:
    data = {
        "equations": sorted(str(e).strip() for e in equations),
        "params": dict(sorted(params.items())),
        "view_radius": round(view_radius, 4),
        "lod": lod,
    }
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(equations: list, params: dict, view_radius: float, lod: bool) -> Optional[dict]:
    _ensure_db()
    key = _make_key(equations, params, view_radius, lod)
    conn = sqlite3.connect(str(CACHE_DB))
    row = conn.execute(
        "SELECT geom_type, vertices, faces, normals, bounds FROM mesh_cache WHERE key=?",
        (key,),
    ).fetchone()
    conn.close()
    if not row:
        return None

    geom_type, v_blob, f_blob, n_blob, bounds_json = row
    return {
        "vertices": np.frombuffer(v_blob, dtype=np.float32).reshape(-1, 3),
        "faces": np.frombuffer(f_blob, dtype=np.int32).reshape(-1, 3),
        "normals": np.frombuffer(n_blob, dtype=np.float32).reshape(-1, 3),
        "geom_type": geom_type,
        "bounds": json.loads(bounds_json),
    }


def put(
    equations: list,
    params: dict,
    view_radius: float,
    lod: bool,
    result: dict,
):
    _ensure_db()
    key = _make_key(equations, params, view_radius, lod)
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute(
        """
        INSERT OR REPLACE INTO mesh_cache
        (key, geom_type, vertices, faces, normals, bounds)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            result["geom_type"],
            result["vertices"].astype(np.float32).tobytes(),
            result["faces"].astype(np.int32).tobytes(),
            result["normals"].astype(np.float32).tobytes(),
            json.dumps(result["bounds"], ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()
