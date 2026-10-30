# Cập nhật image qua GitOps

Workflow [`../../.github/workflows/gitops.yml`](../../.github/workflows/gitops.yml) chạy riêng sau `Retail CI`. Nó không cần AWS role, EKS token hoặc kubeconfig. Các script chỉ ghi GitHub khi workflow đã được bật và nhận token; trong quá trình triển khai code này chưa chạy thao tác ghi GitHub nào.

## Bật workflow

1. Khởi tạo nhánh `main` của `hoi3dkhoiche-byte/DevOps-retails-shop-gitops` từ nội dung thư mục `gitops/`, điền cấu hình hạ tầng và bootstrap Argo CD theo hướng dẫn triển khai.
2. Giữ `environments/dev/images.yaml` và `environments/prod/images.yaml` ở dạng JSON hợp lệ. JSON là YAML hợp lệ và được Helm đọc trực tiếp; các file này do automation quản lý.
3. Tạo fine-grained PAT chỉ cho repo GitOps, quyền **Contents: Read/Write** và **Pull requests: Read/Write**. Lưu vào Actions Secret `GITOPS_TOKEN` của repo source. Giá trị token không đưa vào Git.
4. Đặt Actions Variable `GITOPS_ENABLED=true` trong repo source. Khi chưa bật, CI ứng dụng vẫn hoạt động như hiện tại và hai job cập nhật GitOps được skip.

Dev chỉ nhận release từ `Retail CI` trên `main`, event `push` hoặc `workflow_dispatch`. Script kiểm tra lại kết quả của đủ 14 job bắt buộc trong đúng run attempt, gồm năm source suite, ba E2E, scan, required gate và publish. Rerun riêng job thất bại không thể tái sử dụng kết quả của attempt cũ.

Script tải artifact `gitops-images-<run-id>-<attempt>`, kiểm tra SHA256 của ZIP, chỉ nhận `images.json` và schema, rồi đối chiếu schema với source commit. Profile phải là `mysql-redis-opensearch`; đủ năm repository theo mapping `hmannnguye`; reference bắt buộc là `repository@sha256:...`.

PR dev chỉ thay `environments/dev/images.yaml`. Mỗi release có nhánh automation riêng, không force-update nhánh có sẵn. Job tuần tự hóa theo môi trường, bỏ qua release cũ hơn bản đã merge hoặc đang có PR, và đóng PR automation cũ sau khi tạo PR mới. Operator xem diff rồi merge; Argo CD dev tự sync.

## Nghiệm thu dev và promote prod

Sau khi dev đã sync và UI Rollout đã hoàn tất promotion, chạy từ máy quản trị có kubeconfig và Docker buildx. Nếu image private, Docker cần đăng nhập registry trước. `--context` luôn phải truyền rõ để tránh kiểm thử nhầm cluster:

```bash
python3 scripts/gitops/accept_dev.py \
  --images /path/to/gitops/environments/dev/images.yaml \
  --output /path/to/gitops/environments/dev/accepted.json \
  --context arn:aws:eks:ap-southeast-1:ACCOUNT_ID:cluster/retail-lab-eks \
  --operator YOUR_NAME
```

Script kiểm tra readiness của năm workload, UI active Service và Rollout đã Healthy; đối chiếu Pod image ID với manifest/config digest thực trên registry và image đã qua CI. Sau đó kiểm tra UI HTML, Catalog SQL và OpenSearch, Cart, Orders và Checkout qua port-forward local. Nó đọc lại trạng thái pod sau smoke test để phát hiện thay đổi release trong lúc kiểm tra. Giỏ hàng rỗng dùng cho smoke được xóa sau khi kiểm tra.

`accepted.json` lưu bằng chứng runtime và smoke; E2E đầy đủ vẫn thuộc bộ gate CI trước publish. Review rồi commit file acceptance vào repo GitOps. File này phải khớp source commit, run/attempt và đủ năm digest hiện tại; merge release dev mới khiến acceptance cũ không còn hợp lệ.

Chạy thủ công workflow **Retail GitOps** trên `main`, nhập `expected_dev_commit` là full SHA đã nghiệm thu. Workflow tạo PR prod bằng đúng bộ image dev; không build hoặc retag. Sau merge, sync ứng dụng production thủ công và promote UI sau khi kiểm tra preview. Rollback bằng revert image values về release ổn định rồi sync lại.

## Kiểm tra script

```bash
python3 -m unittest discover -s scripts/gitops -p 'test_*.py' -v
```

Các unit test dùng API giả và registry manifest giả; không tạo PR hoặc sửa repo từ máy đang chạy kiểm tra.
