#!/usr/bin/env python3
"""Test the Job's exact pinned Alpine image and hash lock without AWS credentials."""
from pathlib import Path
import subprocess
import sys
import uuid

from run_bootstrap import DEPENDENCY_INSTALL_COMMAND, IMAGE

DIRECTORY = Path(__file__).resolve().parent


def main():
    name = "retail-bootstrap-deps-" + uuid.uuid4().hex[:12]
    # Kubernetes emptyDir permits loading native shared libraries. Docker tmpfs
    # defaults to noexec, so request exec explicitly to reproduce the real Job.
    command = ["docker", "run", "--rm", "--name", name, "--platform", "linux/amd64",
               "--user", "1000:1000", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--memory", "512m", "--cpus", "0.5",
               "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=384m,mode=1777",
               "--mount", "type=bind,source=" + str(DIRECTORY) + ",target=/config,readonly",
               "--mount", "type=bind,source=" + str(DIRECTORY) + ",target=/work/scripts/infra,readonly",
               "--env", "PYTHONPATH=/tmp/dependencies", "--env", "PYTHONDONTWRITEBYTECODE=1",
               IMAGE, "sh", "-ec", DEPENDENCY_INSTALL_COMMAND + "; "
               "python /work/scripts/infra/crypto_smoke.py; "
               "python -c 'import boto3, botocore, pymysql, bcrypt, cryptography; "
               "print(\"Pinned Alpine hash install and native-library imports passed.\")'"]
    try:
        result = subprocess.run(command, check=False, timeout=600)
        return result.returncode
    except subprocess.TimeoutExpired:
        print("Alpine dependency smoke exceeded 600 seconds.", file=sys.stderr)
        return 1
    except OSError:
        print("Docker could not be started for Alpine dependency smoke.", file=sys.stderr)
        return 1
    finally:
        # Bound cleanup to this invocation's unique container; never prune images.
        try:
            subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            print("Could not confirm cleanup of " + name, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
