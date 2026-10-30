# Kiểm chứng thay đổi kiến trúc

Lượt kiểm tra local ngày 30/10/2026, trên worktree có thay đổi chưa commit. Các image dùng tag local `retail-architecture-<service>:local`; đây không phải release GitHub/Docker Hub.

| Kiểm tra                           | Kết quả                                                                                         |
| ---------------------------------- | ----------------------------------------------------------------------------------------------- |
| CI evidence/gate contracts         | 34 test đạt                                                                                     |
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

Lượt smoke local gọi API và kiểm tra SQL/restart trực tiếp. Bộ Cypress đầy đủ và Kind tiếp tục là gate bắt buộc trong CI sau khi push. EKS/ALB, AWS secret synchronization, failover managed backend và backup/restore cần nghiệm thu trên tài khoản AWS sau khi điền cấu hình và triển khai.
