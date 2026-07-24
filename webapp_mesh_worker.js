/**
 * webapp_mesh_worker.js
 * Web Worker for offloading mesh API calls from the main thread.
 *
 * Message protocol (main → worker):
 *   { type: 'fetchMesh', id, equation, params, port, view_radius, lod, quality }
 *
 * Message protocol (worker → main):
 *   { type: 'meshResult', id, data, error }
 *   { type: 'fetchAllDone', ids[] }
 *   { type: 'error', message }
 */

(function () {
  "use strict";

  // ---------- port detection ----------
  // The API server auto-selects a free port starting at 8000.
  // We try each port sequentially until one responds to /health.
  async function detectPort(basePort) {
    if (basePort !== undefined && Number.isFinite(basePort)) {
      return basePort;
    }
    const maxAttempts = 30;
    const start = 8000;
    for (let port = start; port < start + maxAttempts; port++) {
      try {
        const resp = await fetch(`http://127.0.0.1:${port}/health`, {
          signal: AbortSignal.timeout(800),
        });
        if (resp.ok) return port;
      } catch {
        // port not available, try next
      }
    }
    // fallback: assume the default
    return 8000;
  }

  // ---------- per-equation fetch ----------
  async function fetchOneMesh(cfg) {
    const port = cfg.port || (await detectPort());
    const url = `http://127.0.0.1:${port}/api/mesh`;
    const body = JSON.stringify({
      equation: cfg.equation,
      params: cfg.params || {},
      view_radius: cfg.view_radius || 10.0,
      lod: cfg.lod || false,
      quality: cfg.quality || 1,
    });
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(txt || `HTTP ${resp.status}`);
    }
    return resp.json();
  }

  // ---------- message handler ----------
  self.addEventListener("message", async function (evt) {
    const msg = evt.data;

    // --- batch fetch (parallel) ---
    if (msg.type === "fetchAll") {
      const tasks = Array.isArray(msg.tasks) ? msg.tasks : [];
      if (!tasks.length) {
        self.postMessage({ type: "fetchAllDone", ids: [] });
        return;
      }

      const ids = tasks.map((t) => t.id);

      try {
        const results = await Promise.all(
          tasks.map(function (task) {
            return fetchOneMesh({
              id: task.id,
              equation: task.equation,
              params: task.params,
              port: task.port,
              view_radius: task.view_radius,
              lod: task.lod,
              quality: task.quality,
            }).then(
              (data) => ({ id: task.id, data, error: null }),
              (err) => ({ id: task.id, data: null, error: String(err) })
            );
          })
        );

        // Post each result individually so the main thread can process
        // them as they arrive (out-of-order).
        for (const r of results) {
          self.postMessage({ type: "meshResult", id: r.id, workerSeq: msg.workerSeq, data: r.data, error: r.error });
        }
      } catch (err) {
        self.postMessage({ type: "error", message: String(err) });
      }

      self.postMessage({ type: "fetchAllDone", ids });
      return;
    }

    // --- single fetch (backward compat / fallback path) ---
    if (msg.type === "fetchMesh") {
      const id = msg.id;
      try {
        const data = await fetchOneMesh({
          id,
          equation: msg.equation,
          params: msg.params,
          port: msg.port,
          view_radius: msg.view_radius,
          lod: msg.lod,
          quality: msg.quality,
        });
        self.postMessage({ type: "meshResult", id, data, error: null });
      } catch (err) {
        self.postMessage({ type: "meshResult", id, data: null, error: String(err) });
      }
      return;
    }
  });
})();
