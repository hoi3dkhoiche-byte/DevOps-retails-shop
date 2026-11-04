#!/usr/bin/env python3
"""Render a reviewable GitOps candidate without editing the GitOps checkout.

Only tracked public GitOps files are copied. Terraform outputs are read by the
canonical prepare.py, never copied into the candidate. Existing candidates are
updated only when their ownership manifest and every recorded file still match.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile


MANIFEST = ".retail-candidate.json"
EXCLUDED_DIRECTORIES = {".git", ".aws", ".local", ".ci-tools", ".venv", "venv",
                        "__pycache__", ".terraform", ".pytest_cache", "node_modules"}
PUBLIC_SUFFIXES = {".yaml", ".yml", ".json", ".tpl", ".md", ".py", ".ps1",
                   ".sh", ".toml", ".pem", ".p12", ".txt"}


class CandidateError(ValueError):
    """A safe, operator-readable preparation failure."""


def require(condition, message):
    if not condition:
        raise CandidateError(message)


def no_links(path):
    """Reject links/junctions before resolving paths or writing beneath them."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        require(not part.is_symlink() and not (hasattr(part, "is_junction") and part.is_junction()),
                "Symlinks and junctions are not allowed in candidate paths")
    return path.resolve()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv, cwd, phase, environment=None):
    result = subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, timeout=120, check=False)
    # Canonical scripts may read configuration; never echo their output on error.
    require(result.returncode == 0, phase + " failed; inspect the public GitOps configuration")
    return result.stdout


def excluded(relative):
    name = relative.name.lower()
    return (any(part.lower() in EXCLUDED_DIRECTORIES for part in relative.parts)
            or name == MANIFEST or name.startswith(".env")
            or name in {"credentials", "credentials.json", "credentials.txt", "token", "token.txt", "token.json",
                        "secrets.json", "password.json", "passwords.json"}
            or name.endswith((".key", ".tfvars", ".tfvars.json")) or ".tfstate" in name
            or name.startswith(("terraform-output", "terraform_output", "platform-secret-arns"))
            or ("token" in name and relative.suffix.lower() in {".txt", ".json"}))


def tracked_files(gitops):
    raw = run(["git", "-C", str(gitops), "ls-files", "-z"], gitops, "Reading tracked GitOps files")
    selected = {}
    for value in raw.decode("utf-8").split("\0"):
        if not value:
            continue
        relative = PurePosixPath(value)
        require(not relative.is_absolute() and ".." not in relative.parts and "\\" not in value,
                "Unsafe tracked GitOps path")
        if excluded(relative):
            continue
        if relative.suffix.lower() not in PUBLIC_SUFFIXES and value not in {".gitattributes", ".gitignore"}:
            continue
        # The checked-in Java truststore contains only public RDS CAs.
        require(relative.suffix.lower() != ".p12" or value == "applications/files/rds-truststore.p12",
                "Only the public RDS truststore may be copied as binary material")
        source = no_links(gitops.joinpath(*relative.parts))
        require(source.is_relative_to(gitops) and source.is_file(), "Tracked GitOps file is missing or unsafe")
        if relative.suffix.lower() != ".p12":
            payload = source.read_bytes()
            require(b"PRIVATE KEY-----" not in payload, "Private key material cannot enter a GitOps candidate")
        selected[value] = source
    require({"config.yaml", "scripts/prepare.py", "scripts/check-release.py"}.issubset(selected),
            "GitOps checkout must contain its tracked canonical preparation and release scripts")
    return selected


def file_hashes(root):
    files = {}
    for directory, names, leaves in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in names:
            no_links(parent / name)
        for name in leaves:
            path = no_links(parent / name)
            if path.name == MANIFEST:
                continue
            require(path.is_relative_to(root) and path.is_file(), "Unsafe candidate file")
            files[path.relative_to(root).as_posix()] = digest(path)
    return dict(sorted(files.items()))


def existing_manifest(candidate, gitops):
    if not candidate.exists():
        return None
    require(candidate.is_dir(), "Candidate path is not a directory")
    owner_path = no_links(candidate / MANIFEST)
    require(owner_path.is_file(), "Existing candidate is not owned by this helper; choose a new empty path")
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise CandidateError("Candidate ownership manifest is invalid") from error
    require(isinstance(owner, dict) and owner.get("schema_version") == 1
            and owner.get("source_dir") == str(gitops) and owner.get("candidate_dir") == str(candidate)
            and isinstance(owner.get("files"), dict), "Candidate ownership does not match this checkout/path")
    require(file_hashes(candidate) == owner["files"],
            "Candidate has local edits or unowned files; review them and select a fresh candidate directory")
    return owner


def write_atomic(path, payload, local_dir):
    parent = no_links(path.parent)
    require(parent.is_relative_to(local_dir), "Candidate write escaped its ignored local directory")
    parent.mkdir(parents=True, exist_ok=True)
    no_links(path)
    descriptor, temporary = tempfile.mkstemp(dir=parent, prefix=".candidate-write-")
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_candidate(source_dir, gitops_dir, local_dir, candidate_dir, outputs, activate=False):
    source = no_links(source_dir)
    gitops = no_links(gitops_dir)
    local = no_links(local_dir)
    candidate = no_links(candidate_dir)
    outputs = no_links(outputs)
    require(source.is_dir() and gitops.is_dir(), "Source and GitOps checkout directories are required")
    require(local != source and local.is_relative_to(source), "Local directory must be inside the source checkout")
    require(candidate != local and candidate.is_relative_to(local), "Candidate must be below the ignored local directory")
    require(not candidate.is_relative_to(gitops) and not gitops.is_relative_to(candidate),
            "Candidate cannot alias or contain the active GitOps checkout")
    require(outputs.is_file() and not outputs.is_relative_to(candidate), "Terraform outputs must be outside the candidate")
    ignored = subprocess.run(["git", "-C", str(source), "check-ignore", "--quiet", "--",
                              local.relative_to(source).as_posix() + "/"], capture_output=True, check=False)
    require(ignored.returncode == 0, "Local candidate directory must be ignored by the source repository")
    previous = existing_manifest(candidate, gitops)
    selected = tracked_files(gitops)
    source_commit = run(["git", "-C", str(gitops), "rev-parse", "HEAD"], gitops,
                        "Reading GitOps source revision").decode("ascii").strip()
    local.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="candidate-build-", dir=local)).resolve()
    try:
        for relative, original in selected.items():
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
        environment = os.environ.copy()
        environment.pop("GITHUB_OUTPUT", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [sys.executable, str(stage / "scripts/prepare.py"), "--terraform-output", str(outputs)]
        if activate:
            command.append("--activate")
        run(command, stage, "Canonical GitOps preparation", environment)
        run([sys.executable, str(stage / "scripts/check-release.py")], stage,
            "Five-image release verification", environment)
        files = file_hashes(stage)
        if previous:
            require(not (set(previous["files"]) - set(files)),
                    "Candidate source removed files; choose a fresh candidate directory")
        changed = previous is None or files != previous["files"]
        owner = {"schema_version": 1, "source_dir": str(gitops), "source_commit": source_commit,
                 "candidate_dir": str(candidate), "terraform_output_sha256": digest(outputs),
                 "deployment_enabled": bool(activate), "files": files}
        # Recheck ownership after rendering before making any bounded replacements.
        existing_manifest(candidate, gitops)
        candidate.mkdir(parents=True, exist_ok=True)
        for relative in sorted(files, key=lambda item: (item == "config.yaml", item)):
            if previous is None or previous["files"].get(relative) != files[relative]:
                write_atomic(candidate / relative, (stage / relative).read_bytes(), local)
        write_atomic(candidate / MANIFEST, (json.dumps(owner, indent=2, sort_keys=True) + "\n").encode(), local)
        return {"schema_version": 1, "status": "prepared", "changed": changed, "candidate_dir": str(candidate),
                "deployment_enabled": bool(activate), "gitops_source_commit": source_commit,
                "file_set_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    finally:
        # Resolve and bound the absolute path before recursively cleaning only our staging copy.
        require(stage != local and no_links(stage).is_relative_to(local), "Unsafe staging cleanup path")
        shutil.rmtree(stage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--gitops-dir", type=Path, required=True)
    parser.add_argument("--local-dir", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--activate", action="store_true", help="Activate only the local candidate for review")
    args = parser.parse_args(argv)
    try:
        report = prepare_candidate(args.source_dir, args.gitops_dir, args.local_dir, args.candidate_dir,
                                   args.terraform_output, args.activate)
        print(json.dumps(report, sort_keys=True))
        return 0
    except (CandidateError, OSError, UnicodeError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, CandidateError) else "Local GitOps preparation failed"
        print(json.dumps({"schema_version": 1, "status": "failed", "changed": False, "error": message}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
