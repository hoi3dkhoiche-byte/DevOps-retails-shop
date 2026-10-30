# Kiểm chứng thay đổi kiến trúc

Lượt kiểm tra local ngày 30/10/2026, trên worktree có thay đổi chưa commit. Các image dùng tag local `retail-architecture-<service>:local`; đây không phải release GitHub/Docker Hub.

| Kiểm tra                           | Kết quả                                                                                         |
| ---------------------------------- | ----------------------------------------------------------------------------------------------- |
| CI evidence/gate contracts         | 37 test đạt trên Linux, gồm hồi quy MySQL init                                                  |
| GitOps release/promotion contracts | 20 test đạt                                                                                     |
| Terraform/bootstrap contracts      | 13 test đạt                                                                                     |
| Cart                               | 19 test đạt với MySQL và fixture tương thích cũ                                                 |
| Catalog                            | 16 test đạt, gồm reindex đọc SQL và bắt lỗi bulk một phần                                       |
| Orders                             | 9 test đạt với MySQL/RabbitMQ, thêm 1 test listener đạt                                         |
| Checkout                           | 13 test đạt, gồm tách dữ liệu dev/prod trên một Redis                                           |
| Terraform                          | fmt/validate đạt; không chạy apply AWS                                                          |
| GitOps/platform                    | Render 10 chart đã khóa phiên bản và validator đồ thị đạt                                       |
| GitHub Actions                     | actionlint đạt cho CI, GitOps và Terraform                                                      |
| Credential bootstrap               | bcrypt, TLS certificate/key, tính ổn định và không in plaintext đạt bằng dữ liệu giả            |
| Image build                        | Đủ năm image runtime build thành công                                                           |
| Trivy 0.76.1                       | Năm image đều có 0 HIGH/CRITICAL, gồm OS và thư viện                                            |
| Runtime tích hợp                   | Năm image chạy với một MySQL, Redis, RabbitMQ và OpenSearch thật                                |
| Persistence                        | Orders: 1 record SQL trước/sau restart; Cart: quantity 3, unit price 123 còn nguyên sau restart |
| Image identity                     | Đủ năm image được đối chiếu ở ba thời điểm trước/sau restart; trùng image đã scan               |

Report thô local nằm trong `.ci-tools/architecture-runtime`, `.ci-tools/architecture-scans`, JUnit của các service và các báo cáo test tương ứng. Fixture được dọn sau khi kiểm tra. Các thư mục build/test local không phải cấu hình để đưa lên Git.

Lượt smoke ban đầu gọi API và kiểm tra SQL/restart trực tiếp. Các kiểm tra Cypress bổ sung được ghi bên dưới; Cypress và Kind vẫn là gate bắt buộc trong CI sau khi push. EKS/ALB, AWS secret synchronization, failover managed backend và backup/restore cần nghiệm thu trên tài khoản AWS sau khi điền cấu hình và triển khai.

## Hồi quy CI trên Linux ngày 30/10/2026

Lượt GitHub Actions `40989506534` đã qua source/build/scan, nhưng E2E lỗi ở fixture MySQL. Script init được Git lưu với mode `100644`, nên entrypoint MySQL trên Linux source trực tiếp script. `set -u` bị lan sang entrypoint và gây lỗi `MYSQL_ONETIME_PASSWORD: unbound variable`. Bind mount từ Windows trong lượt smoke ban đầu không tái hiện đúng quyền file này.

Bản sửa giới hạn shell options trong subshell; readiness của Compose và Kind phải đăng nhập được cả ba database bằng user ứng dụng. Test hồi quy thực thi shell Linux kiểm tra startup thành công, giữ nguyên shell options của entrypoint và dừng startup khi SQL lỗi. Kind đồng bộ Redis `7.4.2-alpine`, đồng thời lưu log MySQL hiện tại/lần khởi động trước.

| Chế độ               | Cypress   | SQL và restart                                       | Image identity              |
| -------------------- | --------- | ---------------------------------------------------- | --------------------------- |
| Compose              | 14/14 đạt | 2 Orders còn nguyên; Cart quantity 3, unit price 123 | Đủ năm image ở ba thời điểm |
| Compose + OpenSearch | 20/20 đạt | 2 Orders còn nguyên; Cart quantity 3, unit price 123 | Đủ năm image ở ba thời điểm |
| Kind Kubernetes 1.35 | 14/14 đạt | 2 Orders còn nguyên; Cart quantity 3, unit price 123 | Đủ năm image ở ba thời điểm |

Các lượt kiểm tra dùng source Cypress từ `git archive` trên filesystem Linux. Compose dùng file MySQL init mode `0644` với volume dữ liệu mới; Kind dùng ConfigMap init và cluster mới, kiểm checksum/config ID của đủ năm archive trước khi nạp. Report ở `.ci-tools/latest-failure/runtime/compose-verified`, `search-verified` và `.ci-tools/kind-runtime`. Cả ba luồng đã qua Cypress, SQL/restart và ba lần đối chiếu image; fixture/cluster được dọn sau khi kiểm tra. Đây là kiểm chứng local trên bộ image đã build, không phải bằng chứng publish của GitHub.
