# Kết quả kiểm tra CI local — 2026-10-28

Ngày ghi nhận theo múi giờ `Asia/Saigon`. Đây là bằng chứng kiểm tra thay đổi local, chưa phải một run GitHub Actions hoặc một release ứng dụng.

| Kiểm tra                                                       | Kết quả đã ghi nhận                                             |
| -------------------------------------------------------------- | --------------------------------------------------------------- |
| Python contract tests trên Windows                             | **28 test đạt**                                                 |
| Clean Linux checkout: `mvnw` executable và LF của shell files  | **Đạt**                                                         |
| Python contract tests trên clean Linux snapshot                | **28 test đạt**                                                 |
| actionlint 1.7.7                                               | **Đạt** cho workflow ở Git root                                 |
| Helm 3.19.0                                                    | **Đạt** lint và render `repository@digest` cho đủ năm chart     |
| Docker Compose config                                          | **Đạt** đủ năm app image, không còn build, `pull_policy: never` |
| Docker 29.7.2 scratch fixture: build/save/load/config identity | **Đạt**                                                         |
| Nx runtime trên Node 22 Alpine                                 | **Đạt** graph, inferred target và lựa chọn affected             |
| Parse Python/TOML/JSON/YAML và kiểm tra whitespace của diff    | Đã kiểm tra local; không thay thế gate chạy ứng dụng            |

## Lệnh và phạm vi

Contract tests chạy từ app root:

```bash
python3 -B -m unittest discover -s scripts/ci -p 'test_*.py' -v
```

Bộ test âm kiểm tra zero/all-skipped test, thiếu hoặc stale report, summary mâu thuẫn native report, checksum/config mismatch, điều kiện PR publish, lỗi scanner và push lỗi một phần. Bộ 28 test đã chạy đạt trên cả Windows và clean Linux snapshot.

Kiểm tra Linux dùng container validator `rhysd/actionlint:1.7.7`, mount source chỉ đọc. Helper local `.ci-tools/validate-linux.py` sao chép snapshot working tree và mode từ Git index vào repo tạm, chạy `git add` và `git checkout-index` để tạo thư mục sạch trên Linux; không commit hoặc sửa index của repo người dùng. Helper xác nhận ba `mvnw` executable, không có CR trong `mvnw`/`.sh`, rồi chạy contract tests.

Lệnh lint chạy trên snapshot Linux:

```bash
actionlint -shellcheck= /tmp/retail-clean/.github/workflows/ci.yml
```

Actionlint đã kiểm tra workflow ở Git root và metadata của local actions. Đây là kiểm tra schema/expression workflow của actionlint; ShellCheck được tắt rõ ràng trong lệnh này. Helm 3.19.0 được tải từ bản phát hành chính thức và đối chiếu checksum trước khi dùng. Với từng chart `ui`, `catalog`, `cart`, `checkout`, `orders`, validator chạy:

```bash
/tmp/helm lint <chart>
/tmp/helm template <service> <chart> \
  --set-string image.repository=<repository-của-service> \
  --set-string image.digest=sha256:<64-ký-tự-hex>
```

Validator xác nhận output chứa đúng `repository@sha256:...`. Đây là static rendering, chưa tạo Pod hay kiểm tra persistence của app.

Docker Compose config được resolve với năm tag probe và credentials fixture CI. Kết quả cho đủ năm service có `pull_policy: never` và không có `build`; không khởi chạy app stack trong phép kiểm tra này.

## Nx runtime thực tế

Kiểm tra chạy trong container Node 22 Alpine tạm. Yarn 4.11 cài immutable thành công 419 package trong 15,2 giây; Nx đọc được 14 project. Target build của Checkout được suy luận thành `nx:run-script`; integration có working directory `src/checkout`.

Các phép chọn project thực tế đã đạt:

- Diff local `HEAD~1..HEAD` chọn đủ năm service, phù hợp commit đã đưa toàn bộ source app vào repo.
- `--files` trỏ file của Cart chọn đúng `cart`.
- `--files` trỏ README không chọn service.

Log nằm ở `../../.ci-tools/nx-runtime.log`, được Git ignore và chỉ là bằng chứng local. Kết quả này xác nhận graph/target/selection; chưa chạy build hoặc test ứng dụng bằng các target đó.

## Bằng chứng archive bằng Docker thật

Helper `.ci-tools/probe-archive.py` chạy Docker 29.7.2 với một image `FROM scratch` có tag riêng cho phép thử. Nó build image, `docker image save`, xác minh checksum/config bytes, `docker image load` rồi kiểm tra lại canonical config digest; sau đó xóa đúng tag fixture vừa tạo. Publisher không cần retag vì archive phát hành đã có final run tag.

Bằng chứng local nằm tại [`../dist/ci/local-validation/archive-probe.json`](../dist/ci/local-validation/archive-probe.json), scope ghi rõ `Docker archive API fixture; not application E2E`. File này và các helper `.ci-tools` được Git ignore; không phải release artifact hay bằng chứng năm image ứng dụng đã đạt E2E.

Phép thử đã làm rõ Docker 29/containerd có thể trả manifest ID ở `.Id`. Contract giữ `images[].image_id` là config digest; helper chuẩn hóa qua descriptor/annotation `config.digest`, còn bytes config trong archive phải có hash khớp. Compose kiểm tra immutable container `.Image` rồi chuẩn hóa cùng cách, không kiểm tra lại mutable tag thay cho image thực chạy.

## Chưa được nghiệm thu

- Chưa build đủ năm image ứng dụng hoặc chạy toàn bộ build/lint/unit/integration của ứng dụng.
- Chưa chạy Cypress trên Compose/search/Kind, kiểm tra Pod identity thực, hay test đơn hàng/SQL/restart của app.
- Chưa quét Trivy thực trên năm app image; test âm chỉ kiểm tra logic chặn scanner failure và vulnerability gate.
- Chưa push Docker Hub, chưa xác minh digest pullback của một release thật, chưa có `images.json` release hoàn chỉnh.
- Chưa có GitHub Actions run cho thay đổi này, chưa sửa branch protection/ruleset và chưa sửa repository GitOps.
- Tracing/load scripts upstream được giữ nhưng chưa nối vào workflow mới, required gate hoặc một chế độ manual mới được nghiệm thu.

Profile thiết kế hiện tại là `upstream`: Catalog MySQL/MariaDB, Cart DynamoDB Local, Orders PostgreSQL, Redis/RabbitMQ/OpenSearch. PostgreSQL cho Catalog/Cart chưa được nghiệm thu. Các kết quả local trên không chứng minh EKS, EFS/PVC hoặc HA trên AWS.
