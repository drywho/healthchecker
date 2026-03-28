import base64
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests


@dataclass
class CheckResult:
    name: str
    url: str
    type: str
    status: str           # "up" | "down" | "pending"
    project: str = ""
    http_code: int | None = None
    latency_ms: float | None = None
    error: str | None = None
    checked_at: str | None = None
    response_preview: str | None = None
    image_data: str | None = None

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "name": self.name,
            "url": self.url,
            "type": self.type,
            "status": self.status,
            "http_code": self.http_code,
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms is not None else None,
            "error": self.error,
            "checked_at": self.checked_at,
            "response_preview": self.response_preview,
            "image_data": self.image_data,
        }


def _extract_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()[:120]
    return None


def _extract_filename(content_disposition: str) -> str | None:
    m = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"';\r\n]+)[\"']?",
                  content_disposition, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.1f} MB"


def check_endpoint(endpoint: dict) -> CheckResult:
    name = endpoint["name"]
    url = endpoint["url"]
    ep_type = endpoint["type"]
    expected_status = endpoint["expected_status"]
    timeout = endpoint["timeout"]
    headers = endpoint.get("headers", {})

    # redirect type must not follow redirects so we can inspect the 3xx response
    follow = ep_type != "redirect"

    start = time.monotonic()
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=follow)
        latency_ms = (time.monotonic() - start) * 1000
        http_code = resp.status_code
        is_up = (http_code == expected_status)
        checked_at = datetime.now(timezone.utc).isoformat()

        response_preview = None
        image_data = None
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()

        if ep_type == "json":
            try:
                parsed = resp.json()
                response_preview = json.dumps(parsed, indent=2)[:2000]
            except Exception:
                response_preview = resp.text[:500]

        elif ep_type == "html":
            response_preview = _extract_title(resp.text)

        elif ep_type == "text":
            if is_up and not content_type.startswith("text/"):
                is_up = False
                return CheckResult(
                    name=name, url=url, type=ep_type, status="down",
                    http_code=http_code, latency_ms=latency_ms,
                    checked_at=checked_at,
                    error=f"Expected text/*, got {content_type or 'unknown'}",
                )
            response_preview = resp.text[:2000]

        elif ep_type == "image":
            if is_up:
                if content_type.startswith("image/"):
                    b64 = base64.b64encode(resp.content).decode()
                    image_data = f"data:{content_type};base64,{b64}"
                else:
                    return CheckResult(
                        name=name, url=url, type=ep_type, status="down",
                        http_code=http_code, latency_ms=latency_ms,
                        checked_at=checked_at,
                        error=f"Expected image/*, got {content_type or 'unknown'}",
                    )

        elif ep_type == "file":
            if is_up:
                content_disp = resp.headers.get("Content-Disposition", "")
                filename = _extract_filename(content_disp)
                size = _fmt_size(len(resp.content))
                lines = [f"Content-Type : {content_type or '—'}",
                         f"Filename     : {filename or '(not specified)'}",
                         f"Size         : {size}"]
                if content_disp and "attachment" not in content_disp.lower():
                    lines.append("⚠ Content-Disposition is not 'attachment'")
                response_preview = "\n".join(lines)

        elif ep_type == "redirect":
            location = resp.headers.get("Location", "")
            if not (300 <= http_code < 400):
                is_up = False
            response_preview = f"→ {location}" if location else "(no Location header)"

        return CheckResult(
            name=name, url=url, type=ep_type,
            status="up" if is_up else "down",
            http_code=http_code, latency_ms=latency_ms,
            checked_at=checked_at,
            response_preview=response_preview,
            image_data=image_data,
            error=None if is_up else f"Expected {expected_status}, got {http_code}",
        )

    except requests.exceptions.Timeout:
        return CheckResult(
            name=name, url=url, type=ep_type, status="down",
            latency_ms=(time.monotonic() - start) * 1000,
            error=f"Timed out after {timeout}s",
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
    except requests.exceptions.ConnectionError as e:
        return CheckResult(
            name=name, url=url, type=ep_type, status="down",
            latency_ms=(time.monotonic() - start) * 1000,
            error=f"Connection error: {e}",
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        return CheckResult(
            name=name, url=url, type=ep_type, status="down",
            latency_ms=(time.monotonic() - start) * 1000,
            error=str(e),
            checked_at=datetime.now(timezone.utc).isoformat(),
        )


class HealthChecker:
    def __init__(self, config: dict):
        self._config = config
        # results: {project_name: {endpoint_name: CheckResult}}
        self._results: dict[str, dict[str, CheckResult]] = {
            proj["name"]: {
                ep["name"]: CheckResult(
                    name=ep["name"], url=ep["url"], type=ep["type"],
                    status="pending", project=proj["name"],
                )
                for ep in proj["endpoints"]
            }
            for proj in config["projects"]
        }
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def get_results(self) -> list[dict]:
        with self._lock:
            return [
                r.to_dict()
                for proj_results in self._results.values()
                for r in proj_results.values()
            ]

    def get_config(self) -> dict:
        return {
            "poll_interval": self._config["poll_interval"],
            "server_port": self._config["server_port"],
        }

    def get_projects(self) -> list[dict]:
        with self._lock:
            return list(self._config["projects"])

    # ── project mutations ─────────────────────────────────────────────────────

    def add_project(self, proj: dict) -> None:
        with self._lock:
            self._config["projects"].append(proj)
            self._results[proj["name"]] = {}

    def remove_project(self, name: str) -> bool:
        with self._lock:
            before = len(self._config["projects"])
            self._config["projects"] = [
                p for p in self._config["projects"] if p["name"] != name
            ]
            self._results.pop(name, None)
            return len(self._config["projects"]) < before

    def update_project(self, old_name: str, updates: dict) -> bool:
        """Update project metadata (name, base_url, headers). Handles renaming."""
        with self._lock:
            proj = next((p for p in self._config["projects"] if p["name"] == old_name), None)
            if proj is None:
                return False
            new_name = updates.get("name", old_name)
            proj["name"]     = new_name
            proj["base_url"] = updates.get("base_url", proj.get("base_url", ""))
            proj["headers"]  = updates.get("headers",  proj.get("headers",  {}))
            if new_name != old_name and old_name in self._results:
                self._results[new_name] = self._results.pop(old_name)
                for r in self._results[new_name].values():
                    r.project = new_name
        return True

    # ── endpoint mutations ────────────────────────────────────────────────────

    def add_endpoint(self, project_name: str, ep: dict) -> None:
        with self._lock:
            proj = next(
                (p for p in self._config["projects"] if p["name"] == project_name), None
            )
            if proj is None:
                raise KeyError(f"Project '{project_name}' not found.")
            proj["endpoints"].append(ep)
            self._results.setdefault(project_name, {})[ep["name"]] = CheckResult(
                name=ep["name"], url=ep["url"], type=ep["type"],
                status="pending", project=project_name,
            )
        threading.Thread(
            target=self._check_one, args=(project_name, ep), daemon=True
        ).start()

    def update_endpoint(self, project_name: str, old_ep_name: str, new_ep: dict) -> bool:
        """Replace an endpoint entirely with a fully-normalized dict. Handles renaming."""
        with self._lock:
            proj = next((p for p in self._config["projects"] if p["name"] == project_name), None)
            if proj is None:
                return False
            idx = next((i for i, e in enumerate(proj["endpoints"]) if e["name"] == old_ep_name), None)
            if idx is None:
                return False
            proj["endpoints"][idx] = new_ep
            new_name = new_ep["name"]
            proj_results = self._results.setdefault(project_name, {})
            if new_name != old_ep_name:
                proj_results.pop(old_ep_name, None)
            proj_results[new_name] = CheckResult(
                name=new_name, url=new_ep["url"], type=new_ep["type"],
                status="pending", project=project_name,
            )
        threading.Thread(target=self._check_one, args=(project_name, new_ep), daemon=True).start()
        return True

    def remove_endpoint(self, project_name: str, ep_name: str) -> bool:
        with self._lock:
            proj = next(
                (p for p in self._config["projects"] if p["name"] == project_name), None
            )
            if proj is None:
                return False
            before = len(proj["endpoints"])
            proj["endpoints"] = [ep for ep in proj["endpoints"] if ep["name"] != ep_name]
            self._results.get(project_name, {}).pop(ep_name, None)
            return len(proj["endpoints"]) < before

    # ── internals ─────────────────────────────────────────────────────────────

    def _check_one(self, project_name: str, ep: dict) -> None:
        with self._lock:
            proj = next((p for p in self._config["projects"] if p["name"] == project_name), None)
            proj_headers = proj.get("headers", {}) if proj else {}
        merged = {**ep, "headers": {**proj_headers, **ep.get("headers", {})}}
        result = check_endpoint(merged)
        result.project = project_name
        with self._lock:
            self._results.setdefault(project_name, {})[ep["name"]] = result

    def _poll_loop(self):
        self._run_checks()
        while not self._stop_event.wait(timeout=self._config["poll_interval"]):
            self._run_checks()

    def _run_checks(self):
        with self._lock:
            projects = list(self._config["projects"])
        for proj in projects:
            proj_headers = proj.get("headers", {})
            for ep in list(proj["endpoints"]):
                merged = {**ep, "headers": {**proj_headers, **ep.get("headers", {})}}
                result = check_endpoint(merged)
                result.project = proj["name"]
                with self._lock:
                    self._results.setdefault(proj["name"], {})[ep["name"]] = result
