# Khôi phục Lease backend bootstrap bị giữ lại

Chỉ dùng khi runner báo `lock_busy`, `lease_release_failed` hoặc report có `lease_retained_for_recovery`. Lease hết hạn không chứng minh Job đã dừng. Quy trình dưới chạy trên controller Linux/WSL bằng operator có quyền quản trị cluster, từ gốc source. Không chạy tự động trong CI và không xóa hàng loạt Lease.

## 1. Dừng launcher cũ và xác minh đích

Dừng tiến trình `run_bootstrap.py`/Ansible cũ, hủy lượt automation cũ nếu có và ngăn người khác khởi chạy bootstrap mới trong lúc phục hồi. Một launcher chỉ bị pause có thể tiếp tục tạo Job sau khi mở khóa; phải dừng nó trước.

```bash
set -euo pipefail
export KUBECONFIG="$PWD/.ci-tools/ansible/kubeconfig.yaml"
CONTEXT=retail-lab-eks
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
python ansible/helpers/check_target.py --phase cluster \
  --source-dir "$PWD" --gitops-dir "$PWD/.ci-tools/gitops-publish" \
  --region ap-southeast-1 --cluster-name retail-lab-eks --account-id "$ACCOUNT_ID" \
  --terraform-output .ci-tools/ansible/terraform-outputs.json \
  --terraform-metadata .ci-tools/ansible/metadata.json \
  --kube-context "$CONTEXT" --kubeconfig "$KUBECONFIG"

EXPECTED_ENDPOINT=$(python -c 'import json; d=json.load(open(".ci-tools/ansible/terraform-outputs.json")); x=d["cluster_endpoint"]; print(x["value"] if isinstance(x,dict) else x)')
ACTUAL_ENDPOINT=$(kubectl --context "$CONTEXT" config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
test "$ACTUAL_ENDPOINT" = "$EXPECTED_ENDPOINT"
```

Sử dụng đúng AWS profile/session của deployment. Nếu đã chủ động dùng outputs thủ công không receipt, áp dụng cùng quy định opt-out như runbook Ansible; không bỏ qua checksum/identity lỗi để mở khóa.

## 2. Chụp Lease và kiểm tra holder

```bash
umask 077
mkdir -p .ci-tools/ansible/recovery
LEASE="backend-bootstrap-$ACCOUNT_ID"
kubectl --context "$CONTEXT" -n retail-ops get lease "$LEASE" -o json \
  > .ci-tools/ansible/recovery/lease.json
HOLDER=$(python -c 'import json,re; d=json.load(open(".ci-tools/ansible/recovery/lease.json")); h=d["spec"]["holderIdentity"]; assert re.fullmatch(r"backend-bootstrap-[0-9]{14}-[0-9a-f]{12}",h); print(h)')
kubectl --context "$CONTEXT" -n retail-ops get job "$HOLDER" --ignore-not-found -o json \
  > .ci-tools/ansible/recovery/job.json
kubectl --context "$CONTEXT" -n retail-ops get pods -l "job-name=$HOLDER" -o wide
kubectl --context "$CONTEXT" -n retail-ops get jobs,pods -l app.kubernetes.io/name=backend-bootstrap -o wide
```

Đối chiếu holder với report. Nếu Job tồn tại, kiểm tra label `retail.aws/run-id` đúng bằng holder; dừng nếu sai. Lưu evidence cần thiết trước khi xóa Job. Logs backend có thể chứa thông tin nhạy cảm từ SDK; không đưa log thô lên Git hoặc chat.

## 3. Chỉ dừng đúng Job của holder

Nếu Job còn tồn tại, xác minh nhãn rồi xóa foreground; không dùng force-delete pod hoặc bỏ qua node mất liên lạc:

```bash
if test -s .ci-tools/ansible/recovery/job.json; then
  python - "$HOLDER" <<'PY'
import json, sys
d = json.load(open('.ci-tools/ansible/recovery/job.json'))
assert d['metadata']['name'] == sys.argv[1]
assert d['metadata']['labels']['retail.aws/run-id'] == sys.argv[1]
PY
  kubectl --context "$CONTEXT" -n retail-ops delete job "$HOLDER" \
    --cascade=foreground --wait=true --timeout=120s
fi
test "$(kubectl --context "$CONTEXT" -n retail-ops get pods -l "job-name=$HOLDER" -o jsonpath='{.items[*].metadata.name}')" = ""
kubectl --context "$CONTEXT" -n retail-ops get jobs,pods -l app.kubernetes.io/name=backend-bootstrap -o wide
```

Chỉ tiếp tục khi xác nhận không còn tiến trình backend bootstrap nào có thể ghi. Nếu pod/Job còn chạy, node mất liên lạc, deletion timeout hoặc vẫn có launcher đang sống, giữ Lease để điều tra. Job đã bị TTL dọn sạch vẫn cần xác nhận launcher cũ đã dừng.

## 4. Mở khóa đúng snapshot bằng compare-and-swap

Sinh JSON Patch kiểm tra đồng thời UID, resourceVersion và holder trước khi xóa nội dung holder:

```bash
python - "$LEASE" "$HOLDER" <<'PY'
import json, sys
from pathlib import Path
d = json.load(open('.ci-tools/ansible/recovery/lease.json'))
assert d['metadata']['name'] == sys.argv[1]
assert d['metadata']['namespace'] == 'retail-ops'
assert d['spec']['holderIdentity'] == sys.argv[2]
patch = [{'op': 'test', 'path': '/metadata/' + key, 'value': d['metadata'][key]}
         for key in ('uid', 'resourceVersion')]
patch += [{'op': 'test', 'path': '/spec/holderIdentity', 'value': sys.argv[2]},
          {'op': 'replace', 'path': '/spec/holderIdentity', 'value': ''}]
Path('.ci-tools/ansible/recovery/unlock.json').write_text(json.dumps(patch), encoding='utf-8')
PY
kubectl --context "$CONTEXT" -n retail-ops patch lease "$LEASE" --type=json \
  --patch-file .ci-tools/ansible/recovery/unlock.json
```

Patch thất bại nghĩa là Lease đã đổi hoặc bị thay thế; dừng, đọc lại và điều tra, không bỏ các phép `test`. Sau mở khóa, chạy lại playbook bootstrap bình thường. Runner sẽ lấy Lease qua cơ chế atomic hiện có. Không thay đổi Lease expiry hoặc tạo tên Lease khác để chạy vòng qua khóa.
