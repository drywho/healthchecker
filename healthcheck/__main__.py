import argparse
import sys
import threading
import time
import webbrowser

from .checker import HealthChecker, check_endpoint
from .config import (
    ENDPOINT_DEFAULTS,
    _normalize_endpoint,
    _normalize_project,
    load_config,
    resolve_url,
    save_config,
)
from .server import create_app

VALID_TYPES = ("json", "html", "text", "image", "file", "redirect", "ping")


def _load(args):
    try:
        return load_config(getattr(args, "config", None))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _parse_headers(header_list: list[str] | None) -> dict:
    headers = {}
    for h in (header_list or []):
        if ":" not in h:
            print(f"Error: Invalid header '{h}'. Use 'Key: Value' format.", file=sys.stderr)
            sys.exit(1)
        k, _, v = h.partition(":")
        headers[k.strip()] = v.strip()
    return headers


# ── serve ──────────────────────────────────────────────────────────────────────

def cmd_serve(args):
    config, config_path = _load(args)

    total_eps = sum(len(p["endpoints"]) for p in config["projects"])
    port = args.port or config["server_port"]
    url = f"http://localhost:{port}"

    checker = HealthChecker(config)
    checker.start()
    app = create_app(checker, config_path)

    print(f"  healthcheck  running at {url}")
    print(f"  {len(config['projects'])} project(s), {total_eps} endpoint(s) — "
          f"poll every {config['poll_interval']}s")
    print(f"  Config: {config_path}")
    print(f"  Press Ctrl+C to stop.\n")

    if not args.no_browser:
        def _open():
            time.sleep(0.8)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    try:
        app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\nStopped.")
        checker.stop()
        sys.exit(0)


# ── list ───────────────────────────────────────────────────────────────────────

def cmd_list(args):
    config, config_path = _load(args)
    projects = config["projects"]

    print(f"Config: {config_path}\n")
    if not projects:
        print("  No projects configured.")
        return

    for proj in projects:
        base = f"  ({proj['base_url']})" if proj.get("base_url") else ""
        print(f"  ▸ {proj['name']}{base}")
        eps = proj["endpoints"]
        if not eps:
            print("      (no endpoints)")
            continue
        col = max(len(ep["name"]) for ep in eps)
        for ep in eps:
            print(
                f"      {ep['name']:<{col}}  {ep['url']}  "
                f"[{ep['type']}] {ep['expected_status']} {ep['timeout']}s"
            )
        print()


# ── project add / remove ───────────────────────────────────────────────────────

def cmd_project_add(args):
    config, config_path = _load(args)
    existing = [p["name"] for p in config["projects"]]
    if args.name in existing:
        print(f"Error: Project '{args.name}' already exists.", file=sys.stderr)
        sys.exit(1)

    proj = _normalize_project({
        "name": args.name,
        "base_url": args.base_url or "",
        "headers": _parse_headers(getattr(args, "header", None)),
        "endpoints": [],
    })
    config["projects"].append(proj)
    save_config(config, config_path)
    base_note = f" ({args.base_url})" if args.base_url else ""
    print(f"Added project '{args.name}'{base_note}.")


def cmd_project_remove(args):
    config, config_path = _load(args)
    before = len(config["projects"])
    config["projects"] = [p for p in config["projects"] if p["name"] != args.name]
    if len(config["projects"]) == before:
        print(f"Error: No project named '{args.name}'.", file=sys.stderr)
        sys.exit(1)
    save_config(config, config_path)
    print(f"Removed project '{args.name}'.")


# ── endpoint add / remove ──────────────────────────────────────────────────────

def cmd_add(args):
    config, config_path = _load(args)

    proj = next((p for p in config["projects"] if p["name"] == args.project), None)
    if proj is None:
        # Auto-create project if base_url provided, or single unnamed project exists
        if len(config["projects"]) == 1 and not args.project:
            proj = config["projects"][0]
        else:
            print(
                f"Error: Project '{args.project}' not found. "
                "Use 'healthcheck project add' first.",
                file=sys.stderr,
            )
            sys.exit(1)

    existing = [ep["name"] for ep in proj["endpoints"]]
    if args.name in existing:
        print(
            f"Error: Endpoint '{args.name}' already exists in '{proj['name']}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve url/path
    raw_url = args.url
    if not raw_url.startswith(("http://", "https://")) and proj.get("base_url"):
        resolved = resolve_url(proj["base_url"], raw_url)
    else:
        resolved = raw_url

    raw_ep = {
        **ENDPOINT_DEFAULTS,
        "name": args.name,
        "url": resolved,
        "type": args.type,
        "expected_status": args.expected_status,
        "timeout": args.timeout,
    }
    if args.header:
        raw_ep["headers"] = _parse_headers(args.header)

    ep = _normalize_endpoint(raw_ep, proj)
    proj["endpoints"].append(ep)
    save_config(config, config_path)
    print(f"Added '{args.name}' → {resolved}  [{args.type}]  (project: {proj['name']})")


def cmd_remove(args):
    config, config_path = _load(args)

    proj = next((p for p in config["projects"] if p["name"] == args.project), None)
    if proj is None:
        print(f"Error: Project '{args.project}' not found.", file=sys.stderr)
        sys.exit(1)

    before = len(proj["endpoints"])
    proj["endpoints"] = [ep for ep in proj["endpoints"] if ep["name"] != args.name]
    if len(proj["endpoints"]) == before:
        print(
            f"Error: No endpoint '{args.name}' in project '{args.project}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    save_config(config, config_path)
    print(f"Removed '{args.name}' from '{args.project}'.")


# ── check ──────────────────────────────────────────────────────────────────────

def cmd_check(args):
    config, _ = _load(args)
    projects = config["projects"]

    # Collect endpoints to check
    flat = [
        (proj["name"], ep)
        for proj in projects
        for ep in proj["endpoints"]
    ]
    if args.name:
        flat = [(pname, ep) for pname, ep in flat if ep["name"] == args.name]
        if not flat:
            print(f"Error: No endpoint named '{args.name}'.", file=sys.stderr)
            sys.exit(1)
    if not flat:
        print("No endpoints to check.")
        return

    for proj_name, ep in flat:
        result = check_endpoint(ep)
        icon = "✓" if result.status == "up" else "✗"
        code = result.http_code or "—"
        lat  = f"{result.latency_ms:.0f}ms" if result.latency_ms is not None else "—"
        err  = f"  {result.error}" if result.error else ""
        print(f"  {icon}  [{proj_name}] {ep['name']:<28}  {str(code):<5}  {lat:<8}{err}")


# ── argument parser ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="healthcheck",
        description="Lightweight API/webpage health checker.",
    )
    parser.add_argument("--config", "-c", metavar="PATH", help="Config JSON file path")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # serve
    p_serve = sub.add_parser("serve", help="Start the dashboard (default)")
    p_serve.add_argument("--port", "-p", type=int)
    p_serve.add_argument("--no-browser", action="store_true")

    # list
    sub.add_parser("list", help="List all projects and endpoints")

    # project add / remove
    p_proj = sub.add_parser("project", help="Manage projects")
    proj_sub = p_proj.add_subparsers(dest="project_command", metavar="ACTION")

    p_proj_add = proj_sub.add_parser("add", help="Add a project")
    p_proj_add.add_argument("name", help="Project name")
    p_proj_add.add_argument("--base-url", metavar="URL",
                            help="Default base URL for endpoints in this project")
    p_proj_add.add_argument("--header", action="append", metavar="Key: Value",
                            help="Default request header for all endpoints (repeatable)")

    p_proj_rm = proj_sub.add_parser("remove", aliases=["rm"], help="Remove a project")
    p_proj_rm.add_argument("name", help="Project name")

    # add endpoint
    p_add = sub.add_parser("add", help="Add an endpoint to a project")
    p_add.add_argument("name", help="Endpoint display name")
    p_add.add_argument("url",  help="Full URL or path relative to project base_url")
    p_add.add_argument("--project", "-P", metavar="PROJECT", default=None,
                       help="Target project name")
    p_add.add_argument("--type", "-t", choices=VALID_TYPES, default="json")
    p_add.add_argument("--expected-status", "-s", type=int, default=200, metavar="CODE")
    p_add.add_argument("--timeout", type=int, default=5, metavar="SECS")
    p_add.add_argument("--header", action="append", metavar="Key: Value")

    # remove endpoint
    p_rm = sub.add_parser("remove", aliases=["rm"], help="Remove an endpoint")
    p_rm.add_argument("name", help="Endpoint name")
    p_rm.add_argument("--project", "-P", metavar="PROJECT", required=True)

    # check
    p_check = sub.add_parser("check", help="One-shot check, print results")
    p_check.add_argument("name", nargs="?", help="Endpoint name (omit for all)")

    args = parser.parse_args()

    if args.command is None:
        args.command = "serve"
        args.port = None
        args.no_browser = False

    if args.command == "project":
        if not getattr(args, "project_command", None):
            p_proj.print_help()
            sys.exit(0)
        {"add": cmd_project_add, "remove": cmd_project_remove, "rm": cmd_project_remove}[
            args.project_command
        ](args)
        return

    {
        "serve":  cmd_serve,
        "list":   cmd_list,
        "add":    cmd_add,
        "remove": cmd_remove,
        "rm":     cmd_remove,
        "check":  cmd_check,
    }[args.command](args)


if __name__ == "__main__":
    main()
