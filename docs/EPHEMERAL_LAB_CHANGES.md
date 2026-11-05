# Thay đổi cho lab dựng → kiểm chứng → xóa

Mục tiêu là chạy các thành phần cần để kiểm chứng Terraform, Ansible, GitOps và năm service, sau đó xóa tài nguyên. Không tự provision khi push code và không tự nâng account plan. Thay đổi nằm ở repo source và checkout GitOps `.ci-tools/gitops-publish`.

## Cấu hình hạ tầng

`deployment_profile` mặc định `lab`; `architecture` giữ thiết kế đầy đủ. Lab có một worker management và một worker dev `m7i-flex.large`, mỗi nhóm tối đa một node, không production. MySQL dùng `db.t4g.micro`, gp3 20Gi, backup một ngày, không autoscale storage/protection/final snapshot. RabbitMQ 4.2 dùng một `mq.m7g.medium` và một subnet. OpenSearch giữ engine/type nhưng giảm gp3 còn 10Gi. Redis TLS/ACL và việc tách database/user theo service/môi trường được giữ.

VPC CNI lab bật prefix delegation để giữ đủ IP Pod trên worker nhỏ khi dùng trunk ENI cho Security Groups for Pods. Network policy và Pod SG dùng standard mode. Profile architecture giữ sizing, retention, protection và ba AZ RabbitMQ cũ. Chuyển profile của môi trường đang chạy vẫn phải review replacement và mất dữ liệu.

File chính: `infra/retail-lab/{variables,eks,backends,outputs}.tf`, `scripts/infra/{preflight,write_tfvars,export_outputs}.py`. Xem [bảng profile](DEV_DEPLOYMENT.md).

## Account, credential và dựng lại

Preflight xác nhận account/plan trước tài nguyên: architecture cần Paid; lab cho Free/Paid đang active nhưng worker Free phải có eligibility, amd64 và AZ hợp lệ. Profile apply được lấy từ saved plan đã verify checksum; không nhận override để bỏ gate. Không cấp quyền nâng plan. Các catalog check không chứng minh backend được Free plan chấp nhận: profile mới chưa được live apply.

Mười bảy secret Terraform có recovery 0 trong lab và 7 ngày trong architecture. Bốn platform credential ngoài Terraform có finalizer riêng sau khi mọi backend vắng mặt; opt-in restore chỉ khôi phục đúng bốn tên và dùng lại giá trị, không tự xoay password. Secret legacy đang pending cần đợi hoặc restore/import có chủ đích. Password mới bảo đảm đủ các nhóm ký tự mà OpenSearch yêu cầu.

## GitOps/Ansible

GitOps nhận metadata `deployment_profile`; đầu ra cũ giữ semantics architecture. Lab giữ Argo CD, ESO, Load Balancer Controller, Rollouts, metrics-server và Prometheus/Grafana gọn. Tắt Dex/Notifications/ApplicationSet, Loki/Alloy/Velero/autoscaler và production. Metrics lab dùng retention 6 giờ/emptyDir, không phải backup hoặc monitoring production.

CPU/memory được khai báo cho controller, sidecar, init và hook. Gate render chart thật tính cả Pod Prometheus do operator tạo, replica và một Pod thay thế. Bộ lab hiện là 17 platform Pod kể cả hook, requests 880m CPU/1968Mi RAM, memory limits 4384Mi trước Pod thay thế; kiểm tra có dự phòng cho hệ thống trên node 8Gi. Đây là ngân sách cấu hình, không phải số liệu tải thực tế.

Ansible bootstrap/verify kiểm profile khớp Terraform, nạp đúng overlay và chỉ chờ platform được bật. Helm chart version và bộ năm image theo digest được giữ nguyên. GitOps commit local được CI source pin: `7c41472481ab76fb1b49d771c14110a59d435c1d`.

## CI/CD và teardown

- Push/PR chạy validate không cần AWS credential; AWS plan/apply chỉ manual trên main.
- Create giữ `plan` → `apply`; cleanup có `destroy-plan` → `destroy-apply`, xác nhận project/account/run/attempt/SHA256.
- Teardown xóa tài nguyên lab, giữ IAM/state bằng `removed { destroy=false }`, có manifest re-import. Không dùng bare `-destroy` để thay luồng này.
- Cleanup chặn khi còn workload, ALB/PVC/volume không rõ sở hữu, RDS protection hoặc bucket chưa rỗng. Free plan không ngăn cleanup. Cluster sống cần operator controller có network/Kubernetes access; hosted CI không tự mở `/0` hoặc nhận cluster-admin.
- GitOps production PR cần bật thêm `GITOPS_PRODUCTION_ENABLED=true`. CI build/test/E2E/scan/publish image vẫn giữ các gate bắt buộc; bật GitOps chỉ tạo PR image.

Xem [runbook teardown](LAB_TEARDOWN.md) và [chuẩn bị AWS](AWS_FIRST_DEPLOYMENT.md).

## Kiểm chứng và đưa code lên Git

Kiểm tra cuối: 236 test hạ tầng trên WSL, 29 Terraform mock test, 67 helper test Ansible, 31 test GitOps; Helm render cả hai profile và budget đều đạt. Ansible lint đạt 0 lỗi/0 cảnh báo; các workflow thay đổi qua actionlint. Không dựng lại AWS trong lượt sửa code.

GitOps đã commit **local**, chưa push. Đẩy GitOps trước để SHA được checkout trong CI tồn tại trên GitHub, rồi commit/push các thay đổi source còn lại:

```powershell
git -C .ci-tools/gitops-publish push origin main
```

State/IAM và policy đã có trên account phải được đối chiếu theo runbook trước lần deploy mới; code source không tự cập nhật policy đang attach trên AWS. Các báo cáo AWS/billing/kubeconfig/state riêng của phiên vận hành vẫn nằm trong `.ci-tools` được ignore.
