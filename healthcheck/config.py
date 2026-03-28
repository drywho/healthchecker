import json
from pathlib import Path


DEFAULTS = {
    "poll_interval": 30,
    "server_port": 8080,
}

ENDPOINT_DEFAULTS = {
    "type": "json",
    "expected_status": 200,
    "timeout": 5,
    "headers": {},
}

DEFAULT_CONFIG_TEMPLATE = {
    "poll_interval": 30,
    "server_port": 8080,
    "projects": [
        {
            "name": "Example Project",
            "base_url": "https://jsonplaceholder.typicode.com",
            "endpoints": [
                {"name": "Todos", "path": "/todos/1", "type": "json"},
                {"name": "Posts", "path": "/posts/1", "type": "json"},
            ],
        }
    ],
}

REPO_DIR = Path(__file__).parent.parent
REPO_CONFIG = REPO_DIR / "healthcheck.json"


def resolve_url(base_url: str, path_or_url: str) -> str:
    """Resolve a path against base_url, or return an absolute URL as-is."""
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    base = (base_url or "").rstrip("/")
    path = "/" + path_or_url.lstrip("/")
    return base + path


def _normalize_endpoint(ep_raw: dict, project: dict) -> dict:
    base_url = project.get("base_url", "")
    ep = {**ENDPOINT_DEFAULTS, **ep_raw}

    if "url" not in ep_raw and "path" not in ep_raw:
        raise ValueError(
            f"Endpoint '{ep_raw.get('name', '?')}' in project '{project['name']}' "
            "needs a 'url' or 'path' field."
        )
    if "path" in ep_raw and "url" not in ep_raw:
        ep["url"] = resolve_url(base_url, ep_raw["path"])
        ep["path"] = ep_raw["path"]  # preserved for clean saving
    else:
        ep["url"] = ep_raw["url"]
        ep.pop("path", None)

    return ep


def _normalize_project(proj_raw: dict) -> dict:
    if "name" not in proj_raw:
        raise ValueError("Each project must have a 'name'.")
    proj = {
        "name": proj_raw["name"],
        "base_url": proj_raw.get("base_url", ""),
        "headers": proj_raw.get("headers", {}),
    }
    endpoints = []
    for i, ep_raw in enumerate(proj_raw.get("endpoints", [])):
        if "name" not in ep_raw:
            raise ValueError(
                f"Endpoint #{i} in project '{proj['name']}' is missing 'name'."
            )
        endpoints.append(_normalize_endpoint(ep_raw, proj))
    proj["endpoints"] = endpoints
    return proj


def resolve_config_path(path: str | None = None) -> Path:
    if path:
        return Path(path).expanduser()

    cwd_path = Path.cwd() / "healthcheck.json"
    home_path = Path.home() / ".healthcheck.json"

    if cwd_path.exists():
        return cwd_path
    if REPO_CONFIG.exists():
        return REPO_CONFIG
    if home_path.exists():
        return home_path

    REPO_CONFIG.write_text(json.dumps(DEFAULT_CONFIG_TEMPLATE, indent=2) + "\n")
    print(f"Created default config at {REPO_CONFIG}")
    print("Edit it to add your own projects and endpoints.\n")
    return REPO_CONFIG


def load_config(path: str | None = None) -> tuple[dict, Path]:
    config_path = resolve_config_path(path)

    with open(config_path) as f:
        raw = json.load(f)

    config = {**DEFAULTS, **raw}

    # Backward compat: old flat "endpoints" at top level → wrap in "Default" project
    if "endpoints" in raw and "projects" not in raw:
        config["projects"] = [
            {"name": "Default", "base_url": "", "endpoints": raw["endpoints"]}
        ]
        config.pop("endpoints", None)

    config["projects"] = [_normalize_project(p) for p in config.get("projects", [])]
    return config, config_path


def save_config(config: dict, config_path: Path) -> None:
    out: dict = {
        "poll_interval": config["poll_interval"],
        "server_port": config["server_port"],
        "projects": [],
    }

    for proj in config["projects"]:
        clean_proj: dict = {"name": proj["name"]}
        if proj.get("base_url"):
            clean_proj["base_url"] = proj["base_url"]
        if proj.get("headers"):
            clean_proj["headers"] = proj["headers"]

        clean_eps = []
        for ep in proj["endpoints"]:
            clean_ep: dict = {"name": ep["name"]}
            # Prefer path over url when the endpoint was path-based
            if "path" in ep:
                clean_ep["path"] = ep["path"]
            else:
                clean_ep["url"] = ep["url"]
            # Persist non-default fields
            for k in ("type", "expected_status", "timeout"):
                if ep.get(k) != ENDPOINT_DEFAULTS.get(k):
                    clean_ep[k] = ep[k]
            if ep.get("headers"):
                clean_ep["headers"] = ep["headers"]
            clean_eps.append(clean_ep)

        clean_proj["endpoints"] = clean_eps
        out["projects"].append(clean_proj)

    config_path.write_text(json.dumps(out, indent=2) + "\n")
