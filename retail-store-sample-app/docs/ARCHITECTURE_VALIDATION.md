# Kiểm chứng thay đổi kiến trúc

Lượt kiểm tra local ngày 30/10/2026, trên worktree có thay đổi chưa commit. Các image dùng tag local `retail-architecture-<service>:local`; đây không phải release GitHub/Docker Hub.

| Kiểm tra                           | Kết quả                                                                                         |
| ---------------------------------- | ----------------------------------------------------------------------------------------------- |
| CI evidence/gate contracts         | 35 test đạt trên Linux, gồm đồng bộ fixture SQL                                                 |
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

Lượt GitHub Actions `40989506534` lỗi do script MySQL làm lan `set -u` sang entrypoint. Sau bản sửa shell, lượt `41005765434` qua source/build/scan và hai mode Compose nhưng Kind vẫn lỗi: temporary server mở `/var/lib/mysql/mysql.sock`, còn client trong script tìm `/var/run/mysqld/mysqld.sock`, gây `ERROR 2002`. Container khởi động lại với dữ liệu init dở; user ứng dụng chưa được tạo nên readiness thất bại. Kiểm tra local trước đó dùng socket trùng mặc định client nên chưa bắt được lỗi này.

Bản hiện tại dùng `mysql-init.sql` thay cho shell, để entrypoint MySQL tự chọn socket và root credentials cho SQL. Compose và ConfigMap Kind dùng cùng nội dung SQL. Kind đặt rõ socket `/var/lib/mysql/ci-fixture.sock` để luôn kiểm tra trường hợp khác mặc định client. Readiness vẫn phải đăng nhập được đủ ba database bằng user ứng dụng; không tăng timeout hoặc bỏ gate.

Hồi quy thực tế với MySQL `8.4.11`: script cũ thất bại `ERROR 2002` trên socket khác mặc định; cùng image dùng SQL native khởi tạo thành công, không restart, xác thực đủ ba user trên database riêng. Test đồng bộ SQL thay thế hai test shell options đã hết áp dụng, nên tổng CI contracts hiện là 35.

| Chế độ               | Cypress   | SQL và restart                                       | Image identity              |
| -------------------- | --------- | ---------------------------------------------------- | --------------------------- |
| Compose + OpenSearch | 20/20 đạt | 2 Orders còn nguyên; Cart quantity 3, unit price 123 | Đủ năm image ở ba thời điểm |
| Kind Kubernetes 1.35 | 14/14 đạt | 2 Orders còn nguyên; Cart quantity 3, unit price 123 | Đủ năm image ở ba thời điểm |

Report hồi quy socket ở `.ci-tools/mysql-native-socket-regression`; E2E Compose với SQL native ở `.ci-tools/latest-failure/runtime/search-native-sql`; Kind ở `.ci-tools/kind-native-sql`. Kind đã xác nhận `@@socket=/var/lib/mysql/ci-fixture.sock`, MySQL không restart, cùng Cypress, SQL/restart và ba lần đối chiếu đủ năm image đều đạt. Các lượt kiểm tra dùng source Cypress từ `git archive` trên filesystem Linux, dữ liệu/cluster mới và dọn fixture sau khi xong. Đây là kiểm chứng local trên bộ image đã build, không phải bằng chứng publish của GitHub.
