#!/usr/bin/env bash
# Local Linux/WSL controller tools. No AWS API calls, login or global installs.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
export CHECKPOINT_DISABLE=1

MODE=check
case "${1:---check}" in
  --check) ;;
  --install) MODE=install ;;
  --install-tools) MODE=tools ;;
  --help|-h)
    echo 'Usage: bash scripts/setup-controller.sh [--check|--install|--install-tools]'
    echo 'Default --check only reads local versions; --install writes workspace-local tools/venv.'
    exit 0 ;;
  *) echo 'Use --check, --install or --install-tools.' >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo 'Too many arguments.' >&2; exit 2; }
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
LOCAL="$ROOT/.ci-tools/controller"
BIN="$LOCAL/bin"
VENV="$ROOT/ansible/.venv"
COLLECTIONS="$ROOT/ansible/.collections"
export PATH="$BIN:$VENV/bin:$PATH"
export ANSIBLE_CONFIG="$ROOT/ansible/ansible.cfg"
export ANSIBLE_COLLECTIONS_PATH="$COLLECTIONS"
export ANSIBLE_HOME="$LOCAL/ansible-home"
export ANSIBLE_LOCAL_TEMP="$LOCAL/tmp"
export PIP_CACHE_DIR="$LOCAL/pip-cache"
export PIP_DISABLE_PIP_VERSION_CHECK=1
AWS_VERSION=2.39.3
TERRAFORM_VERSION=1.17.1
HELM_VERSION=3.19.0
KUBECTL_VERSION=1.36.0
AWS_FINGERPRINT=FB5DB77FD5C118B80511ADA8A6310ACC4672475C
KUBECTL_SHA256=123d8c8844f46b1244c547fffb3c17180c0c26dac9890589fe7e67763298748e
HELM_SHA256=a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496
TERRAFORM_SHA256=4638628058e71a55212b1c563712eac03b902128f40740f3dc8174df12135061

fail() { echo "Controller setup: $*" >&2; exit 1; }
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail 'Use Linux/WSL x86_64; native Windows and ARM are not supported by this lock.'
[[ -f "$ROOT/ansible/requirements.txt" && -f "$ROOT/ansible/requirements.yml" ]] || fail 'Run this script from the maintained source checkout.'
for tool in python3.12 git curl sha256sum gpg getconf awk cut install mktemp; do
  command -v "$tool" >/dev/null || fail "Missing prerequisite $tool; see docs/CONTROLLER_SETUP.md."
done
python3.12 -B -c 'import sys,venv,zipfile,tarfile; assert sys.version_info[:2] == (3,12)' || fail 'Python 3.12 with venv support is required.'
getconf GNU_LIBC_VERSION >/dev/null 2>&1 || fail 'AWS CLI requires a glibc Linux controller (for example Ubuntu), not Alpine.'

version_ok() {
  case "$1" in
    aws) command -v aws >/dev/null && [[ $(aws --version 2>&1) == aws-cli/"$AWS_VERSION"\ * ]] ;;
    terraform) command -v terraform >/dev/null && [[ $(terraform version -json | python3.12 -c 'import json,sys;print(json.load(sys.stdin)["terraform_version"])') == "$TERRAFORM_VERSION" ]] ;;
    helm) command -v helm >/dev/null && [[ $(helm version --template '{{.Version}}') == "v$HELM_VERSION" ]] ;;
    kubectl) command -v kubectl >/dev/null && [[ $(kubectl version --client -o json | python3.12 -c 'import json,sys;print(json.load(sys.stdin)["clientVersion"]["gitVersion"])') == "v$KUBECTL_VERSION" ]] ;;
  esac
}

venv_ok() {
  [[ -x "$VENV/bin/python" && -x "$VENV/bin/ansible-playbook" ]] || return 1
  "$VENV/bin/python" -m pip check >/dev/null 2>&1 || return 1
  "$VENV/bin/python" - "$ROOT/ansible/requirements.txt" <<'PY'
import importlib.metadata as metadata,re,sys
assert sys.version_info[:2] == (3,12)
for line in open(sys.argv[1],encoding='utf-8'):
    match=re.match(r'^([A-Za-z0-9_.-]+)==([^\s]+)',line)
    if match:
        assert metadata.version(match[1]) == match[2], 'Local venv differs from the dependency lock'
PY
}

collection_ok() {
  [[ -f "$COLLECTIONS/ansible_collections/kubernetes/core/MANIFEST.json" ]] &&
    python3.12 -c 'import json,sys; assert json.load(open(sys.argv[1]))["collection_info"]["version"] == "6.6.0"' \
      "$COLLECTIONS/ansible_collections/kubernetes/core/MANIFEST.json"
}

if [[ $MODE == check ]]; then
  missing=0
  for tool in aws terraform helm kubectl; do
    if version_ok "$tool"; then echo "OK: $tool pinned version"; else echo "MISSING/DIFFERENT: $tool"; missing=1; fi
  done
  if venv_ok; then
    echo 'OK: local Ansible environment'
  else echo 'MISSING/INVALID: ansible/.venv'; missing=1; fi
  if collection_ok; then
    echo 'OK: kubernetes.core 6.6.0'
  else echo 'MISSING/DIFFERENT: kubernetes.core'; missing=1; fi
  echo 'No AWS identity/API check was performed. Use the deployment preflight after login.'
  exit "$missing"
fi

# Only these dedicated local paths are written. Refuse links before installation.
python3.12 - "$ROOT" "$LOCAL" "$VENV" "$COLLECTIONS" <<'PY'
import os,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
for value in sys.argv[2:]:
    path=Path(os.path.abspath(value))
    assert path != root and path.is_relative_to(root), 'Install path escaped checkout'
    for parent in (path,*path.parents):
        assert not parent.is_symlink(), 'Install path contains a symlink'
PY
mkdir -p "$BIN" "$LOCAL/downloads" "$LOCAL/staging" "$LOCAL/gnupg"
chmod 700 "$LOCAL/gnupg"

fetch() {
  local url=$1 destination=$2
  [[ -s "$destination" ]] || curl --fail --show-error --silent --location --retry 3 --connect-timeout 15 --max-time 600 \
    --proto '=https' --tlsv1.2 "$url" -o "$destination"
}
verify_sha() { printf '%s  %s\n' "$1" "$2" | sha256sum --check --status || fail "Checksum mismatch: $(basename -- "$2"); inspect/remove this single cached file before retrying."; }

if ! version_ok kubectl; then
  archive="$LOCAL/downloads/kubectl-v$KUBECTL_VERSION"
  fetch "https://dl.k8s.io/release/v$KUBECTL_VERSION/bin/linux/amd64/kubectl" "$archive"
  verify_sha "$KUBECTL_SHA256" "$archive"
  install -m 755 "$archive" "$BIN/kubectl"
fi
if ! version_ok helm; then
  archive="$LOCAL/downloads/helm-v$HELM_VERSION-linux-amd64.tar.gz"
  fetch "https://get.helm.sh/helm-v$HELM_VERSION-linux-amd64.tar.gz" "$archive"
  verify_sha "$HELM_SHA256" "$archive"
  python3.12 - "$archive" "$BIN/helm" <<'PY'
from pathlib import Path
import sys,tarfile
with tarfile.open(sys.argv[1],'r:gz') as package:
    member=package.getmember('linux-amd64/helm')
    assert member.isfile() and member.size < 200*1024*1024
    Path(sys.argv[2]).write_bytes(package.extractfile(member).read())
Path(sys.argv[2]).chmod(0o755)
PY
fi
if ! version_ok terraform; then
  archive="$LOCAL/downloads/terraform_${TERRAFORM_VERSION}_linux_amd64.zip"
  fetch "https://releases.hashicorp.com/terraform/$TERRAFORM_VERSION/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" "$archive"
  verify_sha "$TERRAFORM_SHA256" "$archive"
  python3.12 - "$archive" "$BIN/terraform" <<'PY'
from pathlib import Path
import sys,zipfile
with zipfile.ZipFile(sys.argv[1]) as package:
    member=package.getinfo('terraform')
    assert member.file_size < 300*1024*1024
    Path(sys.argv[2]).write_bytes(package.read(member))
Path(sys.argv[2]).chmod(0o755)
PY
fi
if ! version_ok aws; then
  archive="$LOCAL/downloads/awscli-exe-linux-x86_64-$AWS_VERSION.zip"
  fetch "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-$AWS_VERSION.zip" "$archive"
  fetch "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-$AWS_VERSION.zip.sig" "$archive.sig"
  key="$LOCAL/downloads/aws-cli-public.asc"
  cat > "$key" <<'KEY'
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBF2Cr7UBEADJZHcgusOJl7ENSyumXh85z0TRV0xJorM2B/JL0kHOyigQluUG
ZMLhENaG0bYatdrKP+3H91lvK050pXwnO/R7fB/FSTouki4ciIx5OuLlnJZIxSzx
PqGl0mkxImLNbGWoi6Lto0LYxqHN2iQtzlwTVmq9733zd3XfcXrZ3+LblHAgEt5G
TfNxEKJ8soPLyWmwDH6HWCnjZ/aIQRBTIQ05uVeEoYxSh6wOai7ss/KveoSNBbYz
gbdzoqI2Y8cgH2nbfgp3DSasaLZEdCSsIsK1u05CinE7k2qZ7KgKAUIcT/cR/grk
C6VwsnDU0OUCideXcQ8WeHutqvgZH1JgKDbznoIzeQHJD238GEu+eKhRHcz8/jeG
94zkcgJOz3KbZGYMiTh277Fvj9zzvZsbMBCedV1BTg3TqgvdX4bdkhf5cH+7NtWO
lrFj6UwAsGukBTAOxC0l/dnSmZhJ7Z1KmEWilro/gOrjtOxqRQutlIqG22TaqoPG
fYVN+en3Zwbt97kcgZDwqbuykNt64oZWc4XKCa3mprEGC3IbJTBFqglXmZ7l9ywG
EEUJYOlb2XrSuPWml39beWdKM8kzr1OjnlOm6+lpTRCBfo0wa9F8YZRhHPAkwKkX
XDeOGpWRj4ohOx0d2GWkyV5xyN14p2tQOCdOODmz80yUTgRpPVQUtOEhXQARAQAB
tCFBV1MgQ0xJIFRlYW0gPGF3cy1jbGlAYW1hem9uLmNvbT6JAlQEEwEIAD4CGwMF
CwkIBwIGFQoJCAsCBBYCAwECHgECF4AWIQT7Xbd/1cEYuAURraimMQrMRnJHXAUC
akV0ygUJDqP4lQAKCRCmMQrMRnJHXFHjD/9eyZLYcKuQOlLvtqSDtUBiEZf6ZZjM
i3ygYH8rJNtuToUH+HvSpe819urJCquXhDrlK6N+aqW0hCLtNABJG/vsafIgvIYJ
hSGgpgtNnQyMV1jViRWqPjbouw8OkYKBThUfT1i2Y+wn58ifs6ODBCmTexWtXspA
Si+Gt49xDOW0APmbOPnI+a4HJW6tVEo6MWS0WjzpiBayR3d1A4pt4YrPfSdDgpLo
h2SLQqlRqvvVZJaWBjhkErNFpfsBA06sDcPEOb0G8LBUbR4WOcdvhe5LubJbZuxC
AG9kNPCVeQP1ixwjgjXKysaxeQ6rv0VzIQgRp6tLVLWhy6AKDNvLjFSsmXZ1Wl08
Y/RlOHXlzLuQMRE6sR1wOdRxc9TsrNWTGiBK65cvSWOy03JeBkQQ8pesqltiyxI9
U21kkgiXtTSKNGfKK8pO27D81YANhRqPK7iTp6kuFiY2WtOg90KTMNlIT+Ff85Y2
b1rHj6Z0SrCkJujhWk3IBPic/wJgz01LEc/OAdUPlby90RJZcIBhSlWhT7mXnXIO
c0HWlNQrns2s3CTyYwZSiSlYe9ApeLwhjDo8NhbFuCAy61l6O5UsR4AfZxx/rGKv
2wFb1/RN/P4gNe6vmxZAPjR0AQcwD3tc2McimOLr/22kmPz8IH3I0X7WoSFr0Biz
E91G7bb0hOb/cA==
=knv7
-----END PGP PUBLIC KEY BLOCK-----
KEY
  fingerprint=$(gpg --homedir "$LOCAL/gnupg" --batch --with-colons --import-options show-only --import "$key" 2>/dev/null | awk -F: '$1=="fpr" {print $10; exit}')
  [[ $fingerprint == "$AWS_FINGERPRINT" ]] || fail 'AWS CLI public key fingerprint differs from the pinned official key.'
  gpg --homedir "$LOCAL/gnupg" --batch --import "$key" >/dev/null 2>&1
  gpg --homedir "$LOCAL/gnupg" --batch --verify "$archive.sig" "$archive" || fail 'AWS CLI PGP verification failed.'
  stage=$(mktemp -d "$LOCAL/staging/aws-$AWS_VERSION-XXXXXX")
  python3.12 - "$archive" "$stage" <<'PY'
from pathlib import Path,PurePosixPath
import stat,sys,zipfile
root=Path(sys.argv[2]).resolve()
with zipfile.ZipFile(sys.argv[1]) as package:
    for member in package.infolist():
        relative=PurePosixPath(member.filename)
        assert not relative.is_absolute() and '..' not in relative.parts
        assert not stat.S_ISLNK(member.external_attr >> 16), 'Unexpected AWS installer archive symlink'
    package.extractall(root)
    for member in package.infolist():
        path=root/member.filename
        if path.is_file():
            path.chmod((member.external_attr >> 16) & 0o777 or 0o644)
PY
  "$stage/aws/install" --install-dir "$LOCAL/aws-cli" --bin-dir "$BIN" --update
fi

write_environment() {
  printf 'export PATH=%q:%q:$PATH\nexport ANSIBLE_CONFIG=%q\nexport ANSIBLE_COLLECTIONS_PATH=%q\n' \
    "$BIN" "$VENV/bin" "$ROOT/ansible/ansible.cfg" "$COLLECTIONS" > "$LOCAL/env.sh"
  echo "Source this file in the same Linux shell: $LOCAL/env.sh"
}
write_environment
if [[ $MODE == tools ]]; then
  for tool in aws terraform helm kubectl; do version_ok "$tool" || fail "$tool did not report the pinned installed version."; done
  echo 'Tool installation complete. Ansible venv/collection was not changed; no AWS API calls or login were performed.'
  exit 0
fi

if [[ ! -x "$VENV/bin/python" ]] || ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  python3.12 -m venv "$VENV" || fail 'Python venv/ensurepip unavailable. Install the Ubuntu python3.12-venv prerequisite described in the guide.'
fi
lock=$(sha256sum "$ROOT/ansible/requirements.txt" | cut -d ' ' -f 1)
if [[ ! -f "$VENV/retail-lock.sha256" ]] || [[ $(cat "$VENV/retail-lock.sha256") != "$lock" ]] || ! venv_ok; then
  "$VENV/bin/python" -m pip install --require-hashes -r "$ROOT/ansible/requirements.txt"
  printf '%s\n' "$lock" > "$VENV/retail-lock.sha256"
fi
if ! collection_ok; then
  "$VENV/bin/ansible-galaxy" collection install -r "$ROOT/ansible/requirements.yml" -p "$COLLECTIONS"
fi
echo 'Installation made no AWS API calls and did not log in.'
bash "$ROOT/scripts/setup-controller.sh" --check
