# Dựng, kiểm chứng và xóa lab

Profile mặc định `architecture` bật thiết kế đầy đủ; chọn rõ `lab` nếu cần phiên thử nhỏ. Terraform tạo AWS, Ansible bootstrap/kiểm tra và Argo CD đồng bộ ứng dụng; sau khi lưu bằng chứng, luồng teardown này xóa tài nguyên project và giữ IAM cùng bucket state cho cả hai profile. Architecture giữ deletion protection/final snapshot của MySQL, nên cần xử lý đúng các bước bên dưới trước teardown. Chọn profile và giới hạn tại [DEV_DEPLOYMENT.md](DEV_DEPLOYMENT.md). Việc push code không tự dựng hoặc xóa AWS.

## Trước khi xóa

Lưu báo cáo cần giữ. Dừng reconciliation của toàn bộ Application lab, xóa Ingress/Service LoadBalancer qua controller và chờ ALB/target group được thu dọn. Gỡ workload/platform rồi xử lý PVC/PV và volume gp3-retain có chủ đích. Không xóa cluster trước controller cleanup vì ALB/EBS có thể bị bỏ lại. Helper chỉ kiểm tra, không tự xóa mọi namespace hay volume trong region.

Helper từ chối khi còn Application, Ingress, Service LoadBalancer, PVC/PV hoặc pod ngoài namespace hệ thống. Nó còn kiểm tra ALB của VPC, CSI volume chưa xác định được chủ sở hữu, RDS deletion protection, termination protection và bucket dữ liệu chưa rỗng. Nếu RDS còn protection, review/apply một thay đổi cấu hình thông thường trước; không đổi từ architecture sang lab chỉ để bỏ protection và bỏ final snapshot.

Giữ đúng account, region `ap-southeast-1`, source commit và kubeconfig/context tường minh. `--context` có thể là alias riêng, nhưng endpoint phải khớp EKS được kiểm tra qua AWS. Khi IP quản trị đổi, cập nhật `/32` qua plan bình thường; không mở API `/0` để chạy cleanup.

## Quy trình local trên controller

Commit/reconcile source trước khi ghi receipt local. Chạy từ root source với toolchain đã cài, AWS profile operator đã xác minh và không kế thừa `TF_DATA_DIR` của một checkout khác:

```bash
source .ci-tools/controller/env.sh
export AWS_PROFILE=retail-operator AWS_REGION=ap-southeast-1
unset TF_DATA_DIR
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
OPERATOR_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/retail-lab-operator"
TEARDOWN_DIR="$PWD/.ci-tools/lab-teardown-session"

terraform -chdir=infra/retail-lab init -input=false -lockfile=readonly
python scripts/infra/lab_teardown.py prepare \
  --directory "$TEARDOWN_DIR" --account-id "$ACCOUNT_ID" --confirm-project retail-lab \
  --kubeconfig /absolute/path/to/dedicated-kubeconfig.yaml --context retail-lab-eks-dev
terraform -chdir="$TEARDOWN_DIR" init -input=false -lockfile=readonly
terraform -chdir="$TEARDOWN_DIR" plan -input=false -lock-timeout=300s -out=terraform.tfplan
terraform -chdir="$TEARDOWN_DIR" show -no-color terraform.tfplan > "$TEARDOWN_DIR/terraform-plan.txt"
python scripts/infra/lab_teardown.py record --local \
  --directory "$TEARDOWN_DIR" --account-id "$ACCOUNT_ID" --role-arn "$OPERATOR_ROLE_ARN"
```

Chọn một thư mục mới cho mỗi plan; helper không ghi đè bằng chứng cũ. Khi cluster đã vắng mặt, có thể bỏ hai tham số kubeconfig/context. Đọc `terraform-plan.txt`, lưu riêng `retained-resources.json` và lấy SHA256 được in bởi `record`. Plan chỉ được có **delete** trên tài nguyên lab đã chứng minh sở hữu và **forget** trên IAM/state. Đây là ordinary `terraform plan` với các block `removed { lifecycle { destroy = false } }`; **không thay bằng `terraform plan -destroy`** vì hành vi với tài nguyên giữ lại khác nhau.

Sau review, dùng chính SHA256 đó; không tự lấy hash mới để chấp thuận một file đã thay:

```bash
APPROVED_SHA256=REPLACE_WITH_REVIEWED_PLAN_SHA256
python scripts/infra/lab_teardown.py verify --local \
  --directory "$TEARDOWN_DIR" --account-id "$ACCOUNT_ID" --role-arn "$OPERATOR_ROLE_ARN" \
  --confirm-project retail-lab --approved-sha256 "$APPROVED_SHA256"
python scripts/infra/lab_teardown.py check \
  --directory "$TEARDOWN_DIR" --account-id "$ACCOUNT_ID" --confirm-project retail-lab \
  --kubeconfig /absolute/path/to/dedicated-kubeconfig.yaml --context retail-lab-eks-dev
terraform -chdir="$TEARDOWN_DIR" apply -input=false -lock-timeout=300s terraform.tfplan
python scripts/infra/lab_teardown.py complete --directory "$TEARDOWN_DIR" --account-id "$ACCOUNT_ID"
python scripts/infra/lab_teardown.py finalize-platform --local \
  --directory "$TEARDOWN_DIR" --account-id "$ACCOUNT_ID" --role-arn "$OPERATOR_ROLE_ARN" \
  --confirm-project retail-lab --approved-sha256 "$APPROVED_SHA256"
```

State được cập nhật, còn IAM và bucket state vẫn tồn tại trên AWS. Kiểm kê API sau cleanup vẫn cần thiết: state rỗng không chứng minh không có tài nguyên ngoài Terraform bị bỏ sót. Nếu apply bị ngắt, giữ plan, receipt và manifest, kiểm tra state/API trước khi tiếp tục; không force-unlock khi chưa biết tiến trình giữ lock đã dừng.

## Workflow CI/CD

`Retail Terraform` validate trên PR/push mà không nhận AWS credential. Các operation AWS chỉ chạy manual trên `main`; create dùng `plan` → `apply`, cleanup dùng `destroy-plan` → `destroy-apply`. Cleanup yêu cầu account ID, xác nhận đúng chuỗi `retail-lab`, run ID/attempt và SHA256 của plan đã review. Receipt kiểm source commit, trạng thái job, account/role, nội dung scope/manifest, binary plan và thời hạn 24 giờ. Cleanup không cần nâng account sang Paid.

GitHub hosted runner không có Kubernetes Access Entry và không nằm trong `/32` quản trị. Vì vậy hosted cleanup từ chối khi cluster còn tồn tại; dùng controller operator ở trên cho teardown có cluster sống. Không mở network hoặc cấp cluster-admin cho CI để bỏ gate. Hosted cleanup có thể hoàn tất phần còn lại sau khi operator đã dọn cluster, hoặc dùng runner/network được cấu hình riêng trong một thay đổi khác.

Plan artifact giữ một ngày; manifest IAM giữ 90 ngày. Không upload raw state, SecretString, kubeconfig hoặc credential. Tải manifest xuống nơi lưu bền vững trước apply.

## Credential và lần dựng tiếp theo

Mười bảy secret do Terraform quản lý có recovery window 0 trong lab và 7 ngày trong architecture. Xóa lab là xóa credential/dữ liệu tạm có chủ đích; AWS vẫn xử lý giải phóng tên bất đồng bộ. Bốn platform secret tạo trước Terraform được chụp ARN vào scope; finalizer chỉ xử lý bốn tên đó sau khi EKS và mọi backend đã vắng mặt. Mặc định là lịch xóa 7 ngày. Chỉ khi review chủ đích xóa ngay credential của lab, thêm `--platform-secret-recovery-days 0` lúc **prepare**; helper yêu cầu bằng chứng secret container của phiên lab có recovery 0. Workflow có lựa chọn tương đương `platform_secret_deletion=lab-immediate`. Nó không rút ngắn lịch xóa của secret đã pending trước đó.

Giữ `retained-resources.json`: **forget giữ IAM trên AWS nhưng bỏ chúng khỏi state**, nên phải handoff/re-import trước create mới. Sinh import blocks trong root Terraform:

```bash
python scripts/infra/lab_teardown.py reimport --account-id "$ACCOUNT_ID" \
  --manifest "$TEARDOWN_DIR/retained-resources.json" \
  --import-output infra/retail-lab/retained-imports.local.tf
```

File `.local.tf` không chứa password/token và được ignore. Mặc định helper bỏ import service-linked role dùng chung vì fresh inputs reuse role (`create_opensearch_service_linked_role=false`). Chỉ thêm `--include-service-linked-role` khi đã chủ đích giữ resource này trong Terraform với biến `true`; không import instance có count 0.

Review/apply normal create plan trên controller có file import để re-import IAM cùng phiên dựng mới. Nếu muốn lần create chạy bằng CI, operator phải import các IAM entry vào remote state trước (dùng `terraform import -var-file=<tfvars> <address> <import_id>` với từng entry đã review), vì file local không được checkout trên runner. Source/workflow `guard-create` chặn role/attachment/inline policy đã tồn tại nhưng lại bị plan tạo mới, thay vì đợi lỗi `EntityAlreadyExists` giữa apply. Giữ boundary và self-role Deny; thay đổi nền IAM vẫn cần phiên admin phù hợp.

Nếu bốn platform secret còn pending và chủ đích dùng lại, xem opt-in restore giới hạn bốn tên trong [DEV_DEPLOYMENT.md](DEV_DEPLOYMENT.md). Mười bảy secret legacy có recovery cũ cần đợi xóa xong hoặc operator restore đúng ARN rồi reconcile/import; không tự restore hay force-delete để làm plan xanh.
