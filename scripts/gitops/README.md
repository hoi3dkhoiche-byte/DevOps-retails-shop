# Cập nhật image qua GitOps

Workflow [`../../.github/workflows/gitops.yml`](../../.github/workflows/gitops.yml) chạy riêng sau `Retail CI`. Nó không cần AWS role, EKS token hoặc kubeconfig. Các script chỉ ghi GitHub khi workflow đã được bật và nhận token; trong quá trình triển khai code này chưa chạy thao tác ghi GitHub nào.

## Bật workflow

1. Clone repo hiện hành `hoi3dkhoiche-byte/DevOps-retails-shop-gitops`; repo đã được khởi tạo. Điền cấu hình qua GitOps candidate và bootstrap theo [runbook Ansible](../../ansible/README.md). Thư mục `gitops/` trong source là seed lịch sử, không dùng để ghi đè repo hiện hành.
2. Giữ `environments/dev/images.yaml` và `environments/prod/images.yaml` ở dạng JSON hợp lệ. JSON là YAML hợp lệ và được Helm đọc trực tiếp; các file này do automation quản lý.
3. Tạo fine-grained PAT chỉ cho repo GitOps, quyền **Contents: Read/Write** và **Pull requests: Read/Write**. Lưu vào Actions Secret `GITOPS_TOKEN` của repo source. Giá trị token không đưa vào Git.
4. Đặt Actions Variable `GITOPS_ENABLED=true` trong repo source. Khi chưa bật, job `gitops-configuration` ghi rõ trạng thái và các bước còn thiếu trong Actions Summary; hai job tạo PR được skip, CI ứng dụng vẫn hoạt động như hiện tại. Nếu đã bật nhưng thiếu `GITOPS_TOKEN`, job configuration thất bại rõ ràng trước khi tạo PR. Token chỉ được dùng ở bước tạo PR; bước cấu hình chỉ đọc trạng thái có/không có, không in giá trị secret.

Dev chỉ nhận release từ `Retail CI` trên `main`, event `push` hoặc `workflow_dispatch`. Script kiểm tra lại kết quả của đủ 14 job bắt buộc trong đúng run attempt, gồm năm source suite, ba E2E, scan, required gate và publish. Rerun riêng job thất bại không thể tái sử dụng kết quả của attempt cũ.

Script tải artifact `gitops-images-<run-id>-<attempt>`, kiểm tra SHA256 của ZIP, chỉ nhận `images.json` và schema, rồi đối chiếu schema với source commit. Profile phải là `mysql-redis-opensearch`; đủ năm repository theo mapping `hmannnguye`; reference bắt buộc là `repository@sha256:...`.

PR dev chỉ thay `environments/dev/images.yaml`. Mỗi release có nhánh automation riêng, không force-update nhánh có sẵn. Job tuần tự hóa theo môi trường, bỏ qua release cũ hơn bản đã merge hoặc đang có PR, và đóng PR automation cũ sau khi tạo PR mới. Operator xem diff rồi merge; Argo CD dev tự sync.

## Nghiệm thu dev và promote prod

Profile mặc định `architecture` chuẩn bị đủ management/dev/prod và toàn bộ platform. Khi bật `GITOPS_ENABLED=true`, job production có thể chạy thủ công trên `main` sau khi dev được nghiệm thu; không còn cần biến riêng `GITOPS_PRODUCTION_ENABLED`. Job chỉ tạo PR bằng đúng bộ digest đã nghiệm thu, không tự apply AWS hoặc sync production. Nếu chọn profile `lab` gọn, giữ release production tắt vì profile này không có worker production. CI ứng dụng vẫn build, test, scan và publish cùng bộ năm image.

`deployment_profile` (`lab`/`architecture`) điều khiển quy mô hạ tầng và platform. Nó khác `backend_profile=mysql-redis-opensearch` trong release image; hợp đồng backend và digest của năm image được giữ nguyên.

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

Chạy thủ công workflow **Retail GitOps** trên `main`, nhập `expected_dev_commit` là full SHA đã nghiệm thu. Workflow tạo một PR prod chứa đúng bộ image dev và bản sao nguyên vẹn bằng chứng vào `environments/prod/accepted.json`; không build hoặc retag. CI GitOps kiểm tra proof với release production, nên dev có thể tiến lên release mới mà không làm mất lịch sử nghiệm thu của prod. Sau merge, sync ứng dụng production thủ công và promote UI sau khi kiểm tra preview. Rollback bằng revert cả image values và proof đi kèm về release ổn định rồi sync lại.

## Kiểm tra script

```bash
python3 -m unittest discover -s scripts/gitops -p 'test_*.py' -v
```

Các unit test dùng API giả và registry manifest giả; không tạo PR hoặc sửa repo từ máy đang chạy kiểm tra.
