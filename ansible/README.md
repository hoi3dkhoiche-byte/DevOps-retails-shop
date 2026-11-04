# Ansible bootstrap cho Retail AWS lab

Terraform quản lý AWS infrastructure; Ansible chạy các thao tác bootstrap sau Terraform và kiểm tra kết quả; Argo CD tiếp tục quản lý cấu hình ứng dụng/platform trong GitOps. Không SSH vào managed node để cài Kubernetes hoặc backend.

Các playbook đã được viết và kiểm thử offline. Ngày 04/11/2026: 55 test helper trên Linux đạt, sáu playbook qua syntax-check, preflight chạy thành công với changed=0, ansible-lint và actionlint đạt. Chưa chạy chúng với AWS thật vì máy chưa được cấu hình AWS CLI/đăng nhập. Bắt đầu phần account/OIDC/state tại [AWS_FIRST_DEPLOYMENT.md](../docs/AWS_FIRST_DEPLOYMENT.md).

## Controller và dependency

Dùng Linux x86_64 với Python 3.12, hoặc Ubuntu trong WSL2. Ansible controller không chạy trực tiếp bằng PowerShell/Windows Python. Với WSL, clone repo vào filesystem Linux để tránh quyền thư mục mount Windows khiến Ansible bỏ qua `ansible.cfg`.

Cần Git, AWS CLI v2, kubectl tương thích EKS 1.36 và Helm 3.19.0 trong PATH. AWS CLI dùng phiên đăng nhập ngắn hạn của operator đã được EKS access entry cho phép; không đặt access key/password trong inventory. Helm và kubectl phải cài trên cùng controller Linux, không dùng file `.exe` từ workspace Windows.

Từ thư mục gốc source:

```bash
python3.12 -m venv ansible/.venv
source ansible/.venv/bin/activate
python -m pip install --require-hashes -r ansible/requirements.txt
git clone https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop-gitops.git .ci-tools/gitops-publish
cd ansible
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
ansible-galaxy collection install -r requirements.yml -p .collections
ansible-playbook preflight.yml
```

Nếu checkout GitOps đã có thì dùng checkout đó, không clone đè. Mặc định `retail_gitops_dir` trỏ vào `.ci-tools/gitops-publish`; không dùng thư mục `gitops/` lịch sử trong source. Dependency được khóa bằng version và SHA256 trong `requirements.txt` cho Linux x86_64/Python 3.12; collection Kubernetes được pin trong `requirements.yml`. Tham khảo [hỗ trợ controller của Ansible](https://docs.ansible.com/projects/ansible/latest/installation_guide/intro_installation.html) và [module Helm](https://docs.ansible.com/projects/ansible/latest/collections/kubernetes/core/helm_module.html).

## Cấu hình operator

Tạo `ansible/inventory/operator.yml` ở máy operator, file này được Git ignore:

```yaml
retail_account_id: "<12 chữ số của account thực>"
retail_aws_profile: "<profile AWS CLI đã đăng nhập>"
```

Để `retail_aws_profile: ""` nếu dùng phiên AWS được cấp sẵn bằng environment; không tạo tên profile giả. Chạy `aws sts get-caller-identity` trên controller để xác nhận danh tính. Giá trị mặc định còn lại nằm tại [inventory/group_vars/all.yml](inventory/group_vars/all.yml); mọi đường dẫn dưới `.ci-tools/ansible` đều là dữ liệu local được ignore.

## Thứ tự chạy

Các lệnh dưới chạy từ `ansible/`, sau khi activate venv. Mỗi playbook tự kiểm tra prerequisite; không cần chạy một playbook để tạo fact cho playbook khác.

| Bước | Lệnh | Thao tác |
| --- | --- | --- |
| 1 | `ansible-playbook preflight.yml` | Kiểm tra source/GitOps/version pin offline; không cần AWS |
| 2 | `ansible-playbook init-platform.yml -e @inventory/operator.yml` | Kiểm tra STS account; tạo hoặc tái sử dụng credential nền trong Secrets Manager; chỉ ghi ARN ra local |
| 3 | Workflow **Retail Terraform**, operation `plan` rồi `apply` | Theo runbook Terraform; Ansible không tự apply infrastructure |
| 4 | Tải artifact `deployment-context-<run>-<attempt>` | Đặt `terraform-outputs.json` và `metadata.json` vào `.ci-tools/ansible/` |
| 5 | `ansible-playbook bootstrap-backends.yml -e @inventory/operator.yml` | Kiểm tra receipt/hash/source commit, account và live EKS; tạo kubeconfig riêng; chạy Job bootstrap database/user/ACL trong VPC |
| 6 | `ansible-playbook prepare-gitops.yml -e @inventory/operator.yml -e '{"retail_activate_candidate": true}'` | Tạo bản candidate đã nhập output thật và bật deployment; không sửa checkout GitOps đang dùng |
| 7 | Review candidate, tạo PR GitOps và merge | Kiểm tra diff cấu hình/ARN/backend, chạy validator của GitOps; không đưa manifest sở hữu `.retail-candidate.json` vào Git |
| 8 | Fetch/pull GitOps `main` sau merge | Canonical checkout phải sạch và cùng commit với `main` thực tế trên GitHub |
| 9 | `ansible-playbook bootstrap-argocd.yml -e @inventory/operator.yml` | Cài ESO → chờ ExternalSecrets Ready → cài Argo CD → apply root Application |
| 10 | `ansible-playbook verify.yml -e @inventory/operator.yml` | Chờ Applications đồng bộ, kiểm tra ESO và pod/digest dev; không tạo bằng chứng nghiệm thu chức năng |

`init-platform` tạo ARN cần điền vào Terraform input trước plan đầu tiên. Helper tái sử dụng credential hiện có, không tự xoay password mỗi lần chạy. Vì API hiện chưa trả thông tin create/reuse, task này báo `changed` một cách bảo thủ; không xem recap đó là bằng chứng đã thay password.

Receipt từ workflow Terraform ràng buộc output với SHA256, account, run/attempt và **source commit của infrastructure**. Checkout source đúng commit ghi trong `metadata.json` trước bước 5; commit này có thể khác commit image ứng dụng. Nếu lấy output thủ công, phải chủ động đặt boolean `retail_require_output_receipt: false`; account/endpoint/live EKS vẫn được kiểm tra, receipt lỗi có sẵn vẫn bị từ chối. Không tắt receipt chỉ để vượt lỗi checksum.

## Review và đưa candidate vào GitOps

Candidate mặc định tại `.ci-tools/ansible/gitops-candidate`, có bản sao các file tracked công khai và manifest checksum riêng. Helper chạy `prepare.py` và `check-release.py` chính thức của GitOps, kiểm tra đủ 5 image, rồi mới ghi candidate. Chạy lại không đổi dữ liệu sẽ báo unchanged; candidate đã sửa tay được giữ nguyên và yêu cầu chọn đường dẫn candidate mới.

Review phần thay đổi tại `config.yaml` và `platform/*.yaml` so với canonical checkout. Chép **các file cấu hình đã review** vào một branch của repo GitOps, chạy kiểm tra theo README GitOps rồi tạo PR/merge. Không chép `.git`, dữ liệu `.local`, Terraform outputs, credential hoặc `.retail-candidate.json`; không chạy `prepare.py --terraform-output` trực tiếp trên checkout đang phục vụ bootstrap. Candidate không chứa Git metadata và không được dùng trực tiếp làm canonical checkout.

Nếu chưa đặt `retail_activate_candidate: true`, helper giữ deployment disabled. Production vẫn disabled cho đến khi có bằng chứng nghiệm thu dev và PR promote riêng. Khi validate đầy đủ chart ứng dụng, dùng source checkout đúng `release.source_commit` trong `environments/dev/images.yaml`, theo hướng dẫn GitOps; không dùng nhầm SHA infrastructure.

Trước bootstrap, fetch/pull canonical GitOps bằng Git trên controller. Guard yêu cầu origin đúng repository, branch `main`, working tree sạch, HEAD khớp remote `main`, root Application dùng đúng repo/ref/path/valueFiles và không có inline overrides. Nếu repo private, controller cần Git read credentials; Argo CD cần repo credentials template cho cả source và GitOps theo README của repo GitOps. Hai loại quyền này được cấu hình riêng.

## Quyền quản lý và xử lý lỗi

ESO được cài trước Argo CD để đồng bộ admin/Redis credentials. Sau khi root đã quản lý ESO và các credential Applications, playbook chỉ kiểm tra chúng, không Helm upgrade hoặc apply đè các tài nguyên do Argo CD sở hữu. Argo CD bản thân nó vẫn là Helm release do operator quản lý. Các phiên bản lấy từ `versions.yaml` của GitOps.

Backend runner dùng explicit kubeconfig/context, timeout và Lease theo account để tránh bootstrap song song. JSON report nằm trong `.ci-tools/ansible/backend-bootstrap-*.json`; task thất bại vẫn giữ đường dẫn báo cáo và trả playbook failure. Nếu runner giữ Lease vì chưa xác nhận Job dừng, kiểm tra Job/Pod/Lease trước khi thử lại; không xóa Lease khi Job còn chạy. Report chỉ có metadata/trạng thái, không password.

[Runbook khôi phục Lease](../docs/BOOTSTRAP_LEASE_RECOVERY.md) có lệnh dừng đúng holder Job và mở khóa bằng kiểm tra UID/resourceVersion/holder; không tự thu hồi Lease chỉ vì hết hạn.

Các playbook có thao tác ghi từ chối `--check` với thông báo rõ ràng. `--syntax-check` và `preflight.yml` là cách kiểm tra offline; `--check` không thay thế nghiệm thu với API thật. Các module Helm/ESO và vòng đợi pod/Application đều có timeout, không chờ vô hạn.

`verify.yml` xác nhận triển khai sẵn sàng, không thay thế chức năng mua hàng. Trước promote prod, chạy helper nghiệm thu của GitOps với **cùng AWS session và kubeconfig riêng**:

```bash
# Từ gốc source; chạy --help để lấy các tham số smoke/acceptance cần thiết.
export KUBECONFIG="$PWD/.ci-tools/ansible/kubeconfig.yaml"
python .ci-tools/gitops-publish/scripts/release/accept_dev.py --help
```

Helper acceptance hiện dùng `kubectl --context`, nên biến `KUBECONFIG` là cần thiết. Không tự tạo `accepted.json` hoặc bật production chỉ vì pod Ready.

## Kiểm tra code

Từ `ansible/` trên controller đã cài dependency:

```bash
python -B -m unittest discover -s tests -p 'test_*.py' -v
ansible-lint --offline
for play in preflight init-platform bootstrap-backends prepare-gitops bootstrap-argocd verify; do
  ansible-playbook --syntax-check "$play.yml"
done
ansible-playbook preflight.yml
```

Workflow [Retail Ansible](../.github/workflows/ansible.yml) chỉ chạy các kiểm tra offline trên PR/push/manual; không nhận AWS credentials, không apply Terraform, không ghi AWS/Kubernetes và không tự push GitOps.
