# Cấu hình đầy đủ và vòng đời dựng → kiểm chứng → xóa

Theo yêu cầu bật lại toàn bộ thành phần, mặc định hiện là profile `architecture`. Lần triển khai AWS tiếp theo do operator thực hiện. Đợt sửa này chỉ thay code/cấu hình, không tạo AWS, không nâng account plan và không kích hoạt GitOps bằng các giá trị giả. Luồng dựng → kiểm chứng → xóa vẫn được giữ. Thay đổi nằm ở repo source và checkout GitOps `.ci-tools/gitops-publish`.

## Cấu hình hạ tầng

`deployment_profile` mặc định `architecture`: đủ ba nhóm management/dev/prod, management `m6i.xlarge`, dev/prod `m6i.large`. MySQL dùng `db.t4g.small`, gp3 30Gi, autoscale đến 100Gi, backup 7 ngày, deletion protection và final snapshot. RabbitMQ 4.2 dùng `mq.m7g.large`, cluster ba AZ. OpenSearch 3.3 `r8g.medium.search` dùng gp3 50Gi. Redis TLS/ACL và việc tách database/user theo service/môi trường được giữ. Multi-AZ MySQL vẫn là tùy chọn tường minh, mặc định Single-AZ theo thiết kế.

Profile `lab` gọn vẫn có thể chọn rõ: hai worker `m7i-flex.large`, MySQL micro/20Gi/backup một ngày, một broker `mq.m7g.medium` và OpenSearch 10Gi. Tfvars đã ghi `lab` không bị mặc định mới ghi đè.

VPC CNI lab bật prefix delegation để giữ đủ IP Pod trên worker nhỏ khi dùng trunk ENI cho Security Groups for Pods. Network policy và Pod SG dùng standard mode. Profile architecture giữ sizing, retention, protection và ba AZ RabbitMQ cũ. Chuyển profile của môi trường đang chạy vẫn phải review replacement và mất dữ liệu.

File chính: `infra/retail-lab/{variables,eks,backends,outputs}.tf`, `scripts/infra/{preflight,write_tfvars,export_outputs}.py`. Xem [bảng profile](DEV_DEPLOYMENT.md).

## Account, credential và dựng lại

Preflight xác nhận account/plan trước tài nguyên: architecture cần `PAID`/`ACTIVE`; lab cho Free/Paid đang active nhưng worker Free phải có eligibility, amd64 và AZ hợp lệ. Profile apply được lấy từ saved plan đã verify checksum; không nhận override để bỏ gate. Không cấp quyền nâng plan. Các catalog check không chứng minh backend được Free plan chấp nhận: profile lab mới chưa được live apply. [IAM_POLICIES.md](IAM_POLICIES.md) giải thích trust, bốn phần policy Terraform, boundary và 14 role runtime; bật platform không tự nới quyền IAM hoặc cập nhật policy đang attach trên AWS.

Mười bảy secret Terraform có recovery 0 trong lab và 7 ngày trong architecture. Bốn platform credential ngoài Terraform có finalizer riêng sau khi mọi backend vắng mặt; opt-in restore chỉ khôi phục đúng bốn tên và dùng lại giá trị, không tự xoay password. Secret legacy đang pending cần đợi hoặc restore/import có chủ đích. Password mới bảo đảm đủ các nhóm ký tự mà OpenSearch yêu cầu.

## GitOps/Ansible

GitOps mặc định `deploymentProfile: architecture` và nhận metadata Terraform tương ứng; đầu ra cũ giữ semantics architecture. Đủ Argo CD (Dex, Notifications, ApplicationSet), ESO, Load Balancer Controller, Rollouts, metrics-server, Prometheus/Grafana/Alertmanager, cluster-autoscaler, Loki, Alloy và Velero. Prometheus giữ 7 ngày trên PVC 30Gi, cap 24GB; Loki giữ 168 giờ trên S3. Lab overlays gọn vẫn được giữ cho lựa chọn `lab` tường minh.

`deployment.enabled=false` được giữ khi chưa có output AWS thật. Production configuration có trong architecture; release ứng dụng production vẫn cần đúng bộ digest đã nghiệm thu dev, PR và sync/promote thủ công. Repo credentials cho repo private, receiver gửi cảnh báo ra ngoài và domain/ACM cần operator điền cấu hình thực.

CPU/memory được khai báo cho controller, sidecar, init và hook. Gate render chart thật tính cả Pod Prometheus do operator tạo, replica và một Pod thay thế. Bản render architecture có 26 platform Pod kể cả hook, requests 1870m CPU/4944Mi RAM, memory limits 13216Mi trước Pod thay thế. Lab có 17 Pod, requests 880m CPU/1968Mi RAM, memory limits 4384Mi. Đây là ngân sách cấu hình, không phải số liệu tải thực tế.

Ansible bootstrap/verify kiểm profile khớp Terraform, nạp đúng overlay và chỉ chờ platform được bật. Helm chart version và bộ năm image theo digest được giữ nguyên. Mốc bật architecture dùng GitOps commit `a9113acc0dfe83b0dad1ec3665fd0b70133d9163`; các bổ sung CI/CD sau đó dùng SHA hiện hành trong `.github/workflows/ansible.yml`. Luôn đẩy GitOps trước khi push source tham chiếu SHA mới.

## CI/CD và teardown

- Push/PR chạy validate không cần AWS credential; AWS plan/apply chỉ manual trên main.
- Create giữ `plan` → `apply`; cleanup có `destroy-plan` → `destroy-apply`, xác nhận project/account/run/attempt/SHA256.
- Teardown xóa tài nguyên lab, giữ IAM/state bằng `removed { destroy=false }`, có manifest re-import. Không dùng bare `-destroy` để thay luồng này.
- Cleanup chặn khi còn workload, ALB/PVC/volume không rõ sở hữu, RDS protection hoặc bucket chưa rỗng. Free plan không ngăn cleanup. Cluster sống cần operator controller có network/Kubernetes access; hosted CI không tự mở `/0` hoặc nhận cluster-admin.
- GitOps dùng `GITOPS_ENABLED=true` và token để tạo PR; production chạy thủ công sau nghiệm thu dev, không cần thêm công tắc production riêng. CI build/test/E2E/scan/publish image vẫn giữ các gate bắt buộc; bật GitOps chỉ tạo PR image.

Xem [runbook teardown](LAB_TEARDOWN.md) và [chuẩn bị AWS](AWS_FIRST_DEPLOYMENT.md).

## Kiểm chứng và đưa code lên Git

Sau khi đổi mặc định, đã chạy lại và đạt: 236 test Python hạ tầng, 29 Terraform mock test, 67 helper test Ansible, 25 test release và 31 test GitOps. Terraform fmt/validate và actionlint cho hai workflow thay đổi đều đạt. Helm render cả hai profile, kiểm tra budget và toàn bộ application graph theo đúng source chart của release cũng đạt. Ansible playbook không đổi trong lượt này; kết quả lint 0 lỗi/0 cảnh báo thuộc mốc kiểm chứng trước. Đây là kiểm chứng code; không dựng lại hoặc nghiệm thu AWS trong lượt sửa code.

GitOps commit `a9113acc0dfe83b0dad1ec3665fd0b70133d9163` đã được push lên `main`. Với các lần cập nhật tiếp theo, đẩy GitOps trước để SHA được checkout trong CI tồn tại trên GitHub, rồi commit/push source:

```powershell
git -C .ci-tools/gitops-publish push origin main
```

State/IAM và policy đã có trên account phải được đối chiếu theo runbook trước lần deploy mới; code source không tự cập nhật policy đang attach trên AWS. Các báo cáo AWS/billing/kubeconfig/state riêng của phiên vận hành vẫn nằm trong `.ci-tools` được ignore.
