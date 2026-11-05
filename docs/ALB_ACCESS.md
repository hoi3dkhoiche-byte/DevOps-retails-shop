# Truy cập ứng dụng qua ALB

Phạm vi đã chốt là request từ người dùng và response của web app đều đi qua ALB:

```text
Request:  người dùng → ALB public → target group → Pod UI private
Response: người dùng ← ALB public ← target group ← Pod UI private
```

ALB và EKS worker nằm trong cùng VPC; ALB dùng public subnets, worker dùng private app subnets. Phản hồi trên kết nối do ALB mở tới Pod quay về ALB, không dùng NAT làm đường trả response tới người dùng. Kết nối mới đi Internet, DNS, Pod → API/backend, monitoring và các lệnh quản trị là luồng riêng.

Terraform giữ VPC/subnet/route table/IAM. GitOps cài AWS Load Balancer Controller và khai báo Ingress; controller tạo ALB, target group và các Security Group cần thiết. Không tạo ALB thứ hai bằng Terraform cho cùng Ingress.

| Cấu hình | Dev | Production |
| --- | --- | --- |
| Chưa có domain/ACM | HTTP, ALB group `retail-dev`, chỉ `adminCidrs` | HTTP, group `retail-prod`, chỉ `adminCidrs`, chờ release production được bật |
| Có domain/ACM | `dev.retail.<domain>`, HTTPS, giới hạn IP quản trị | `retail.<domain>`, HTTPS |
| Group khi có domain | Dùng chung `retail-public`, phân biệt hostname | Dùng chung `retail-public`, phân biệt hostname |

Hai môi trường không có hostname cần group riêng để các route `/` không xung đột. Deployment hoặc release bị tắt thì không tạo Ingress. Production vẫn cần bằng chứng nghiệm thu dev và promote thủ công.

Các Service ứng dụng giữ `ClusterIP`. ALB dùng target type `ip`, chuyển tới `ui-active:8080`, health check `/actuator/health/readiness`. Controller bật `enableBackendSecurityGroup: true` và quản lý rule SG cho target; không dùng custom frontend SG. NetworkPolicy cho traffic từ public subnet CIDRs và các kết nối nội bộ cần thiết; CIDR không phải danh tính ALB. Quyền admin/port-forward và monitoring được kiểm soát riêng.

## Kiểm tra sau triển khai

Chọn kubeconfig/context của cluster thật đã được bootstrap, rồi chạy:

```bash
kubectl --context retail-lab-eks -n retail-dev get ingress retail-ui -o wide
kubectl --context retail-lab-eks -n retail-dev get service ui-active ui-preview
kubectl --context retail-lab-eks -n retail-dev get pods -o wide
```

Khi chưa có domain, lấy hostname ở cột ADDRESS của Ingress và mở `http://<hostname-ALB>` từ IP quản trị đã khai báo. Khi có domain, tạo DNS alias đến ALB, kiểm tra ACM và HTTP redirect sang HTTPS.

Trên AWS kiểm tra ALB có scheme internet-facing và public subnets; worker không có public IP; target group Healthy; rule target-SG nhận cổng ứng dụng từ backend SG của controller. Kiểm thử truy cập từ IP được phép và ngoài allowlist, kiểm tra response ứng dụng đi qua ALB. Đây là bước nghiệm thu thực tế; render Helm/CI chưa tạo tài nguyên hoặc chứng minh kết nối mạng AWS.

Mã hiện hành nằm trong repo [GitOps](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop-gitops): `applications/templates/ingress.yaml`, `platform/aws-load-balancer-controller.yaml`, `applications/templates/policy.yaml`. [Terraform network](../infra/retail-lab/network.tf) và [worker](../infra/retail-lab/eks.tf) giữ nguyên quy hoạch subnet. Các [bước triển khai](CI_CD_HANDOFF.md) và [bootstrap Ansible](../ansible/README.md) vẫn chạy thủ công.
