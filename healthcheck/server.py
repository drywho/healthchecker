import logging
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .checker import HealthChecker
from .config import ENDPOINT_DEFAULTS, _normalize_endpoint, _normalize_project, save_config

VALID_TYPES = ("json", "html", "text", "image", "file", "redirect", "ping")


def create_app(checker: HealthChecker, config_path: Path) -> Flask:
    static_dir = Path(__file__).parent / "static"
    app = Flask(__name__, static_folder=str(static_dir))

    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    def _persist():
        config = checker.get_config()
        config["projects"] = checker.get_projects()
        save_config(config, config_path)

    @app.route("/")
    def index():
        return send_from_directory(str(static_dir), "dashboard.html")

    @app.route("/api/status")
    def status():
        return jsonify({
            "results": checker.get_results(),
            "projects": checker.get_projects(),
            "meta": checker.get_config(),
        })

    # ── projects ──────────────────────────────────────────────────────────────

    @app.route("/api/projects", methods=["POST"])
    def add_project():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400

        existing = [p["name"] for p in checker.get_projects()]
        if name in existing:
            return jsonify({"error": f"Project '{name}' already exists"}), 409

        proj = _normalize_project({
            "name": name,
            "base_url": (body.get("base_url") or "").strip(),
            "headers": body.get("headers") or {},
            "endpoints": [],
        })
        checker.add_project(proj)
        _persist()
        return jsonify({"ok": True, "project": proj}), 201

    @app.route("/api/projects/<path:project_name>", methods=["PUT"])
    def update_project(project_name: str):
        body = request.get_json(silent=True) or {}
        new_name = (body.get("name") or "").strip()
        if not new_name:
            return jsonify({"error": "name is required"}), 400

        if new_name != project_name:
            existing = [p["name"] for p in checker.get_projects()]
            if new_name in existing:
                return jsonify({"error": f"Project '{new_name}' already exists"}), 409

        updates = {
            "name":     new_name,
            "base_url": (body.get("base_url") or "").strip(),
            "headers":  body.get("headers") or {},
        }
        if not checker.update_project(project_name, updates):
            return jsonify({"error": f"Project '{project_name}' not found"}), 404
        _persist()
        return jsonify({"ok": True})

    @app.route("/api/projects/<path:project_name>", methods=["DELETE"])
    def remove_project(project_name: str):
        removed = checker.remove_project(project_name)
        if not removed:
            return jsonify({"error": f"Project '{project_name}' not found"}), 404
        _persist()
        return jsonify({"ok": True})

    # ── endpoints ─────────────────────────────────────────────────────────────

    @app.route("/api/projects/<path:project_name>/endpoints", methods=["POST"])
    def add_endpoint(project_name: str):
        projects = checker.get_projects()
        proj = next((p for p in projects if p["name"] == project_name), None)
        if proj is None:
            return jsonify({"error": f"Project '{project_name}' not found"}), 404

        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        url  = (body.get("url")  or "").strip()
        path = (body.get("path") or "").strip()

        if not name:
            return jsonify({"error": "name is required"}), 400
        if not url and not path:
            return jsonify({"error": "url or path is required"}), 400

        ep_type = body.get("type", "json")
        if ep_type not in VALID_TYPES:
            return jsonify({"error": f"type must be one of {VALID_TYPES}"}), 400

        existing = [ep["name"] for ep in proj["endpoints"]]
        if name in existing:
            return jsonify({"error": f"Endpoint '{name}' already exists in '{project_name}'"}), 409

        raw_ep = {
            **ENDPOINT_DEFAULTS,
            "name": name,
            "type": ep_type,
            "expected_status": int(body.get("expected_status", 200)),
            "timeout": int(body.get("timeout", 5)),
        }
        if url:
            raw_ep["url"] = url
        else:
            raw_ep["path"] = path
        if body.get("headers"):
            raw_ep["headers"] = body["headers"]

        ep = _normalize_endpoint(raw_ep, proj)
        checker.add_endpoint(project_name, ep)
        _persist()
        return jsonify({"ok": True, "endpoint": ep}), 201

    @app.route("/api/projects/<path:project_name>/endpoints/<path:ep_name>", methods=["PUT"])
    def update_endpoint(project_name: str, ep_name: str):
        projects = checker.get_projects()
        proj = next((p for p in projects if p["name"] == project_name), None)
        if proj is None:
            return jsonify({"error": f"Project '{project_name}' not found"}), 404

        body = request.get_json(silent=True) or {}
        new_name = (body.get("name") or "").strip()
        url      = (body.get("url")  or "").strip()
        path     = (body.get("path") or "").strip()

        if not new_name:
            return jsonify({"error": "name is required"}), 400
        if not url and not path:
            return jsonify({"error": "url or path is required"}), 400

        ep_type = body.get("type", "json")
        if ep_type not in VALID_TYPES:
            return jsonify({"error": f"type must be one of {VALID_TYPES}"}), 400

        # Duplicate name check (allow keeping same name)
        if new_name != ep_name:
            existing = [e["name"] for e in proj["endpoints"]]
            if new_name in existing:
                return jsonify({"error": f"Endpoint '{new_name}' already exists"}), 409

        raw_ep = {
            **ENDPOINT_DEFAULTS,
            "name": new_name,
            "type": ep_type,
            "expected_status": int(body.get("expected_status", 200)),
            "timeout": int(body.get("timeout", 5)),
            "headers": body.get("headers") or {},
        }
        if url:
            raw_ep["url"] = url
        else:
            raw_ep["path"] = path

        new_ep = _normalize_endpoint(raw_ep, proj)
        if not checker.update_endpoint(project_name, ep_name, new_ep):
            return jsonify({"error": f"Endpoint '{ep_name}' not found"}), 404
        _persist()
        return jsonify({"ok": True, "endpoint": new_ep})

    @app.route("/api/projects/<path:project_name>/endpoints/<path:ep_name>", methods=["DELETE"])
    def remove_endpoint(project_name: str, ep_name: str):
        removed = checker.remove_endpoint(project_name, ep_name)
        if not removed:
            return jsonify({"error": f"Endpoint '{ep_name}' not found in '{project_name}'"}), 404
        _persist()
        return jsonify({"ok": True})

    return app
