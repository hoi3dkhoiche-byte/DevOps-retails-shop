#!/usr/bin/env python3
"""Select services from Git and cross-check the real nested Nx project graph."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

APP = Path(__file__).resolve().parents[2]
SERVICES = ("ui", "catalog", "cart", "checkout", "orders")
DOCS = {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "DEVELOPER_GUIDE.md", "LICENSE"}


def select_paths(paths, event, prefix="retail-store-sample-app/"):
    selected, reasons = set(), []
    full, iac = event != "pull_request", False
    for path in paths:
        parts = PurePosixPath(path).parts
        if path.startswith("/") or ".." in parts:
            full = True
            reasons.append("unrecognized path")
        elif prefix and not path.startswith(prefix):
            if path != "architecture.platform.png" and not path.startswith("docs/"):
                full = True
                reasons.append("repository configuration changed")
        else:
            relative = path[len(prefix):]
            items = PurePosixPath(relative).parts
            if relative in DOCS or relative.startswith("docs/"):
                continue
            if relative.startswith("terraform/"):
                iac = True
            elif len(items) > 2 and items[0] == "src" and items[1] in SERVICES:
                selected.add(items[1])
            else:
                full = True
                reasons.append("shared or unclassified application change")
    if full:
        selected, iac = set(SERVICES), True
    return {"schema": 1, "event": event, "services": [s for s in SERVICES if s in selected],
            "run_iac": iac, "full_check": full,
            "reason": "; ".join(dict.fromkeys(reasons)) or ("full main/manual validation" if full else "classified diff")}


def command(*args):
    return subprocess.check_output(args, cwd=APP, text=True).strip()


def nx_projects(*args):
    output = command("yarn", "nx", "show", "projects", *args, "--json")
    projects = json.loads(output)
    if not isinstance(projects, list) or any(not isinstance(item, str) for item in projects):
        raise ValueError("Nx did not return a project list")
    return projects


def affected_projects(base, head, event):
    # Main/manual intentionally validate everything, including initial or force
    # pushes whose previous commit cannot be fetched from the current history.
    if event == "pull_request" and base:
        return nx_projects("--affected", "--base=" + base, "--head=" + head, "--with-target", "build")
    return list(SERVICES)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--head", required=True)
    parser.add_argument("--event", choices=("pull_request", "push", "workflow_dispatch"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sha = command("git", "rev-parse", "HEAD")
    if not re.fullmatch("[0-9a-f]{40}", sha) or args.head != sha:
        raise ValueError("Plan head does not match the immutable checkout")
    if args.event == "workflow_dispatch" and os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Manual publication workflow must run from main")
    root = Path(command("git", "rev-parse", "--show-toplevel")).resolve()
    prefix = APP.relative_to(root).as_posix() + "/"
    paths, base, fallback = [], args.base, None
    if args.event == "pull_request":
        try:
            if not base or not re.fullmatch("[0-9a-f]{40}", base):
                raise ValueError("missing base")
            command("git", "cat-file", "-e", base + "^{commit}")
            raw = subprocess.check_output(["git", "diff", "--name-only", "--no-renames", "-z", base, sha, "--"], cwd=root)
            paths = [item.decode("utf-8") for item in raw.split(b"\0") if item]
        except (ValueError, subprocess.CalledProcessError, UnicodeDecodeError):
            fallback, base = "unavailable diff: full validation", None
    else:
        base = None
    plan = select_paths(paths, "push" if fallback else args.event, prefix)
    known = nx_projects("--with-target", "build")
    if not set(SERVICES) <= set(known):
        raise ValueError("Nx project graph is missing application build targets")
    affected = affected_projects(base, sha, args.event)
    nx_services = set(affected) & set(SERVICES)
    if not plan["full_check"] and not set(plan["services"]) <= nx_services:
        raise ValueError("Nx affected omitted a service selected by its changed files")
    selected = set(plan["services"]) | nx_services
    plan.update(event=args.event, sha=sha, base=base, paths=paths,
                services=[s for s in SERVICES if s in selected],
                nx_projects=known, nx_affected=affected,
                backend_profile="upstream", reason=fallback or plan["reason"])
    plan["any_source"] = bool(selected)
    plan["run_e2e"] = bool(selected)
    plan["source_matrix"] = {"include": [{"service": s} for s in plan["services"]]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            for key in ("sha", "services", "source_matrix", "any_source", "run_e2e", "run_iac"):
                value = plan[key] if isinstance(plan[key], str) else json.dumps(plan[key], separators=(",", ":"))
                output.write(key + "=" + value + "\n")
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
