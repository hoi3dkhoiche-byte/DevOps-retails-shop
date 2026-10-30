#!/usr/bin/env python3
"""Static CI/path/LF checks and existing formatter plus Helm digest validation."""
import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import tomllib

APP = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    report = APP / "reports/style"
    report.mkdir(parents=True, exist_ok=True)

    def run(command, input_text=None, env=None):
        result = subprocess.run(command, cwd=APP, input=input_text, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=900, env=env, check=False)
        print(result.stdout)
        with (report / "commands.log").open("a", encoding="utf-8") as output:
            output.write("+ " + " ".join(command) + "\n" + result.stdout)
        if result.returncode:
            raise RuntimeError("Static check failed: " + " ".join(command))
        return result.stdout

    for file in (APP / "scripts/ci").glob("*.py"):
        ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    for file in [APP / "nx.json", *(APP / "src" / s / "project.json" for s in ("ui", "catalog", "cart", "checkout", "orders"))]:
        json.loads(file.read_text(encoding="utf-8"))
    tomllib.loads((APP / ".mise.toml").read_text(encoding="utf-8"))
    tracked = run(["git", "ls-files"]).splitlines()
    for relative in tracked:
        if relative.endswith(".sh") or Path(relative).name == "mvnw":
            file = APP / relative
            if file.is_file() and b"\r" in file.read_bytes():
                raise ValueError("Non-LF shell file in Linux checkout: " + relative)
    for service in ("ui", "cart", "orders"):
        wrapper = APP / "src" / service / "mvnw"
        if not os.access(wrapper, os.X_OK):
            raise ValueError("mvnw must be executable in a clean Linux checkout")
    run(["actionlint", "-shellcheck=", *[str(path) for path in sorted((APP.parent / ".github/workflows").glob("*.yml"))]])
    if plan["event"] == "pull_request" and plan.get("base"):
        names = run(["git", "diff", "--relative", "--name-only", "--diff-filter=d",
                     plan["base"], plan["sha"], "--", "."]).splitlines()
    else:
        names = tracked
    names = [name for name in names if (APP / name).is_file()]
    # Terraform fmt is run recursively below. The old root-level TFLint invocation
    # has no initialized module/config and is not represented as an effective gate.
    environment = dict(os.environ, LEFTHOOK_EXCLUDE="tflint,terraform-fmt")
    run(["yarn", "lefthook", "run", "pre-commit-ci", "--files-from-stdin"],
        input_text="\n".join(names) + "\n", env=environment)
    if plan["run_iac"]:
        run(["terraform", "fmt", "-check", "-recursive", "terraform"])
    for service in ("ui", "catalog", "cart", "checkout", "orders"):
        chart = "src/" + service + "/chart"
        run(["helm", "lint", chart])
        reference = "docker.io/hmannnguye/devops-retails-shop" + ("" if service == "ui" else "-" + service)
        digest = "sha256:" + "a" * 64
        rendered = run(["helm", "template", service, chart, "--set-string", "image.repository=" + reference,
                        "--set-string", "image.digest=" + digest])
        if reference + "@" + digest not in rendered:
            raise ValueError("Chart cannot render a digest reference: " + service)
        (report / (service + ".yaml")).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
