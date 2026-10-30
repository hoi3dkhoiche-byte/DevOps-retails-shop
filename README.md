# Retail AWS lab

Kiến trúc triển khai dùng một EKS tại Singapore, ba node group management/dev/prod và hai namespace `retail-dev`, `retail-prod`.

| Thành phần | Cấu hình |
| --- | --- |
| Catalog, Cart, Orders | Một RDS MySQL 8.4.11; database/user riêng theo service và môi trường |
| Checkout | Redis dùng chung, tách ACL và key prefix dev/prod |
| Tìm kiếm | OpenSearch; index và user/role riêng theo môi trường; reindex đọc Catalog SQL |
| Sự kiện Orders | Amazon MQ RabbitMQ 4.2, cluster ba AZ, private AMQPS; outbox MySQL, confirm, retry, DLQ và chống xử lý trùng |
| Credential | Secrets Manager → ESO → Kubernetes Secret |
| UI | Argo Rollouts Blue/Green; dev tự promote, prod promote thủ công |
| CI | Build một lần, kiểm tra cùng bộ image, scan và push Docker Hub |
| CD | PR cập nhật digest trong GitOps; prod dùng đúng bộ đã được nghiệm thu ở dev |

## Các thư mục sử dụng

- [`retail-store-sample-app`](retail-store-sample-app): source, Dockerfile, Helm chart và test ứng dụng.
- [`.github/workflows/ci.yml`](.github/workflows/ci.yml): CI ứng dụng. [`docs/CI.md`](retail-store-sample-app/docs/CI.md) mô tả gate và artifact.
- [`infra/retail-lab`](infra/retail-lab): Terraform cho kiến trúc mới. Các module trong `retail-store-sample-app/terraform` là mẫu upstream, không phải entrypoint của lab mới.
- [`scripts/infra/README.md`](scripts/infra/README.md): preflight, plan/apply và bootstrap database/secret từ trong VPC.
- [`gitops/README.md`](gitops/README.md): bộ seed cho repo GitOps, phiên bản platform đã khóa và thứ tự bootstrap.
- [`scripts/gitops/README.md`](scripts/gitops/README.md): tự tạo PR image và nghiệm thu/promote dev → prod.

## Trước khi triển khai

Điền IP quản trị thực tế, account/IAM role, phiên bản EKS add-on tương thích được preflight trả về và ARN các platform secret trong cấu hình hạ tầng. Domain/ACM có thể bổ sung sau; cấu hình HTTP lab giới hạn IP đã có.

Terraform chạy theo hai bước riêng: tạo plan, xem diff và SHA256, sau đó mới chạy apply với plan đã xem. Nếu đang có hạ tầng/state hoặc Helm release cũ, cần kiểm kê và chuyển quyền quản lý theo hướng dẫn trước khi áp dụng; không xóa resource cũ để ép Argo CD tạo lại.

Chuẩn bị và khởi tạo repo GitOps bằng nội dung thư mục `gitops/`. Sau đó đặt `GITOPS_TOKEN` và bật Variable `GITOPS_ENABLED=true` trong repo source. CI ứng dụng vẫn chạy khi chưa bật bước GitOps.

Các giá trị `REPLACE_*` và IP tài liệu trong file ví dụ phải được thay bằng giá trị thực. Script chuẩn bị GitOps từ chối placeholder và IP quản trị không hợp lệ. Việc push source không tự chạy Terraform apply hay bootstrap AWS.

## Kiểm tra local

```powershell
python -B -m unittest discover -s retail-store-sample-app/scripts/ci -p 'test_*.py' -v
python -B -m unittest discover -s scripts/gitops -p 'test_*.py' -v
python -B -m unittest discover -s scripts/infra -p 'test_*.py' -v
python gitops/validate.py
terraform -chdir=infra/retail-lab init -backend=false -input=false
terraform -chdir=infra/retail-lab validate
```

Các suite ứng dụng chạy qua Nx/CI với database fixture thật. Gate yêu cầu báo cáo native, đúng image ID và dữ liệu Orders/Cart còn nguyên sau restart. Chạy lại CI bằng **Re-run all jobs** để giữ đúng run/attempt cho toàn bộ artifact.

Kết quả local kiểm chứng source và manifest; nghiệm thu EKS/ALB/Secrets Manager, failover RabbitMQ, backup/restore và mục tiêu RTO/RPO cần chạy trên tài khoản AWS sau khi triển khai.
