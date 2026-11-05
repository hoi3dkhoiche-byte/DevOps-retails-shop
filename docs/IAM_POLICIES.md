# IAM policy trong dự án

IAM trả lời hai việc: **ai được dùng một role** và **role đó được làm gì trên AWS**. Code hiện có bốn lớp dưới đây. Bật profile `architecture` chọn đủ thành phần triển khai; các lớp giới hạn quyền vẫn được giữ.

## 1. Trust policy: ai được dùng role

Trust nằm ở `assume_role_policy` của role, mô tả danh tính được assume role:

- Role `retail-lab-terraform` tin GitHub OIDC, audience `sts.amazonaws.com`, subject đúng `repo:hoi3dkhoiche-byte/DevOps-retails-shop:ref:refs/heads/main`. Workflow Terraform còn yêu cầu thao tác AWS thủ công trên `main`.
- Role control plane tin dịch vụ EKS; role worker tin dịch vụ EC2.
- 12 role IRSA tin OIDC của đúng cluster và `system:serviceaccount:<namespace>:<name>` tương ứng. Pod dùng token ServiceAccount để nhận credential tạm thời.

File nguồn: [iam.tf](../infra/retail-lab/iam.tf), [eks.tf](../infra/retail-lab/eks.tf). [render_terraform_policy.py](../scripts/infra/render_terraform_policy.py) cũng tạo `trust.json` cho bước bootstrap role Terraform.

Trust cho phép nhận role; quyền gọi API nằm ở policy gắn vào role.

## 2. Identity policy Terraform: quyền quản lý hạ tầng

Template [terraform-infra.json](../infra/retail-lab/policies/terraform-infra.json) bao gồm:

| Nhóm quyền | Mục đích |
| --- | --- |
| State S3 và lock | Đọc/ghi state, lấy/trả lock; giữ bucket state khi teardown |
| Preflight và metadata | Kiểm tra account plan, engine/version, instance/AZ, IAM và backend |
| Network, EKS, backend | Quản lý VPC/NAT/subnet/SG, EKS/node group/add-on, RDS, MQ, Redis, OpenSearch và CloudFormation |
| Secret, S3 dữ liệu, log | Quản lý container Secret; CloudFormation đọc dynamic reference; quản lý bucket logs/backup và log group |
| IAM project, OIDC, PassRole | Quản lý đúng tập role project có boundary; chỉ attach policy được cho phép; chuyển role cho dịch vụ đã chỉ định |
| ACM và Route 53 | Phục vụ cấu hình domain khi được operator sử dụng |
| Explicit Deny | Chặn tự sửa role/policy CI, sửa/gỡ boundary và cấp quyền lên role không có boundary đúng |

Renderer hiện chia template thành **4 managed policy**: `retail-lab-terraform`, `retail-lab-terraform-02`, `-03`, `-04`. Cả bốn được attach vào cùng role Terraform; đây là cách chia theo giới hạn 6144 ký tự mỗi policy. Không phải mỗi phần đại diện một service. Renderer tạo thêm `manifest.json` với ARN, tên file và kích thước; số phần phải đọc lại từ manifest nếu template thay đổi.

Một số API create/read/list dùng `Resource: "*"`; các điều kiện account/region/service hoặc ARN cụ thể được áp dụng nơi API hỗ trợ. Policy này có quyền tạo/xóa hạ tầng nên cần admin review trước khi attach. CI build/test/publish Docker Hub không dùng role Terraform này.

## 3. Permissions boundary: giới hạn quyền tối đa

[workload-boundary.json](../infra/retail-lab/policies/workload-boundary.json) được admin tạo thành policy `retail-lab-workload-boundary`. [workload-boundary.tf](../infra/retail-lab/workload-boundary.tf) đọc và so document với template; Terraform gắn boundary vào đủ **14 role runtime**: control plane, worker và 12 IRSA.

Boundary tự nó không cấp quyền API. Với quyền identity policy của các role này, thao tác phải đồng thời nằm trong policy cấp quyền và boundary; các Deny vẫn thắng. Boundary chặn quản trị IAM, assume role tiếp, sửa EKS access/resource policy và truy cập token repository ngoài role ESO Argo CD. Quyền dịch vụ được cho phép bên trong vẫn cần giới hạn ở policy từng role.

Role Terraform không tự sửa được boundary hoặc chính role/policy CI. Admin xử lý các thay đổi nền đó rồi reconcile/import trước lần plan CI tiếp theo. Boundary này không gắn lên service-linked role do AWS quản lý.

## 4. Policy runtime: mỗi thành phần dùng quyền riêng

| Role | Quyền phục vụ |
| --- | --- |
| `retail-lab-eks-control-plane` | EKS control plane và VPC resource controller |
| `retail-lab-eks-nodes` | EKS worker, ECR pull, CNI và SSM agent |
| `retail-lab-ebs-csi` | Tạo/gắn/quản lý volume EBS cho PVC |
| `retail-lab-lb` | ALB/target group/listener, SG và tài nguyên liên quan theo policy controller |
| `retail-lab-autoscaler` | Đọc capacity; thay đổi ASG có tag sở hữu cluster |
| `retail-lab-rollouts` | Đọc ALB/target health phục vụ rollout |
| `retail-lab-eso-dev`, `retail-lab-eso-prod` | Chỉ đọc Secret dưới prefix đúng môi trường |
| `retail-lab-eso-monitoring` | Đọc credential Grafana và cấu hình Alertmanager |
| `retail-lab-eso-argocd` | Đọc Secret Argo CD/Redis và repo credentials khi được cấu hình |
| `retail-lab-grafana` | Đọc CloudWatch metrics và metadata tài nguyên |
| `retail-lab-loki` | Đọc/ghi/xóa object trong bucket logs |
| `retail-lab-velero` | Bucket backup, object trong prefix `velero/`; snapshot EBS đang tắt |
| `retail-lab-bootstrap` | Đọc credential backend; ghi/xoay Secret đã chỉ định và Redis user; không đọc/ghi token repository |

Các role trong bảng được khai báo ở [eks.tf](../infra/retail-lab/eks.tf), [iam.tf](../infra/retail-lab/iam.tf), [monitoring-iam.tf](../infra/retail-lab/monitoring-iam.tf) và [bootstrap-iam.tf](../infra/retail-lab/bootstrap-iam.tf). Policy ALB riêng nằm ở [load-balancer-controller.json](../infra/retail-lab/policies/load-balancer-controller.json). IAM chỉ cấp quyền AWS; quyền SQL, RabbitMQ, Redis ACL, OpenSearch và Kubernetes RBAC được cấu hình riêng.

## Operator, service-linked role và các trường JSON

Danh tính operator là người chạy AWS CLI/kubectl/Ansible. Flow hiện được hướng dẫn là IAM Console user → credential tạm thời → operator role. User/role operator cần bootstrap ngoài root Terraform, trust đúng danh tính và các quyền thao tác cần thiết; `admin_role_arn` của operator được cấp EKS access. Xem [controller setup](CONTROLLER_SETUP.md). Không đưa mật khẩu/access key vào source.

Các service-linked role như EKS/node group, Auto Scaling, RDS, MQ, Redis, ELB và OpenSearch do AWS service sử dụng. Code tái sử dụng OpenSearch role đã tồn tại hoặc tạo khi chọn rõ biến tương ứng. Luồng teardown giữ IAM và bucket state; lần dựng sau đối chiếu manifest/import để tránh tạo trùng.

| Trường JSON | Ý nghĩa |
| --- | --- |
| `Version` | Phiên bản ngôn ngữ policy, hiện `2012-10-17` |
| `Statement` | Danh sách quy tắc; `Sid` đặt tên để dễ review |
| `Effect` | `Allow` cho phép hoặc `Deny` từ chối |
| `Action` | API được áp dụng, ví dụ `s3:GetObject` |
| `Resource` | ARN tài nguyên chịu quy tắc |
| `Principal` | Danh tính/dịch vụ được tin trong trust hoặc resource policy |
| `Condition` | Điều kiện bổ sung: subject OIDC, region, tag, boundary, dịch vụ nhận role… |

Chỉnh file JSON local chưa thay đổi quyền đang attach trên AWS. Render, review và cập nhật bằng danh tính admin theo [runbook AWS](AWS_FIRST_DEPLOYMENT.md), rồi chạy preflight/plan với đúng profile trước triển khai.
