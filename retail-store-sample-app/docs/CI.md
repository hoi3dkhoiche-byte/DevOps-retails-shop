# CI của Retail

Workflow đang dùng là [`../../.github/workflows/ci.yml`](../../.github/workflows/ci.yml). Git root là `DevOps`; app nằm trong `retail-store-sample-app`. Các YAML trong `.github/workflows` của app chỉ được giữ làm tham chiếu upstream, không phải workflow đang hoạt động. Không đưa lại các caller ECR/release cũ lên Git root.

## Cấu hình cần có trên GitHub và Docker Hub

| Nơi lưu          | Tên                   | Giá trị hoặc mục đích                        |
| ---------------- | --------------------- | -------------------------------------------- |
| Actions Variable | `DOCKERHUB_NAMESPACE` | `hmannnguye`                                 |
| Actions Secret   | `DOCKERHUB_USERNAME`  | `hmannnguye`                                 |
| Actions Secret   | `DOCKERHUB_TOKEN`     | Token được phép push các repository bên dưới |

Workflow đọc namespace qua `vars`, username/token qua `secrets`. Secret Docker Hub chỉ xuất hiện ở job publish sau khi gate đạt. CI không cần AWS role, AWS credentials thật, kubeconfig EKS, database production hay quyền ghi GitOps.

| Service  | Docker Hub repository                     |
| -------- | ----------------------------------------- |
| UI       | `hmannnguye/devops-retails-shop`          |
| Catalog  | `hmannnguye/devops-retails-shop-catalog`  |
| Cart     | `hmannnguye/devops-retails-shop-cart`     |
| Checkout | `hmannnguye/devops-retails-shop-checkout` |
| Orders   | `hmannnguye/devops-retails-shop-orders`   |

Cần xác nhận bốn repository có hậu tố và token đã được tạo, rồi điền Variable/Secrets trước khi publish thật. Repository GitOps là [`hoi3dkhoiche-byte/DevOps-retails-shop-gitops`](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop-gitops). CI app xuất hợp đồng image; chưa tự sửa hoặc đẩy commit vào repository GitOps.

## Luồng kiểm tra và required check

`plan → style + source → images → E2E + scan → ci-required → publish`

- PR vào `main`: kiểm tra tiêu đề, format, source/test theo Nx affected; khi có thay đổi app thì build đủ năm image, chạy Compose/search/Kind và Trivy. Không publish, không nhận secret publish.
- Push `main`: full validation cho năm service rồi mới publish.
- Manual: workflow phải chạy trên `main`, vẫn qua toàn bộ gate; không có chế độ bỏ qua test/scan.
- PR chỉ đổi tài liệu có thể không chọn service. Plan vẫn kiểm tra Nx graph, lưu diff và danh sách Nx affected; static checks vẫn bắt buộc.
- Thay đổi workflow, toolchain, lockfile, helper hoặc đường dẫn chung/không phân loại được sẽ kiểm tra toàn bộ. Mất diff thì fallback full validation.

Tracing và load-test scripts upstream được giữ làm tham chiếu, chưa nối vào workflow mới hoặc required gate ở giai đoạn này; chưa có chế độ manual tracing/load mới được nghiệm thu.

Job có tên ổn định **`ci-required`**. Sau khi workflow đã được đưa lên GitHub và check xuất hiện, đặt chính tên này làm required status check cho `main`, giữ các required check đang có nếu vẫn cần. Việc cấu hình branch protection/ruleset trên GitHub chưa được thực hiện trong thay đổi local này.

Gate đọc kết quả tất cả job bắt buộc và artifact bằng chứng. `failure`, `cancelled`, thiếu job hoặc `skipped` ngoài kế hoạch đều làm gate thất bại. Chỉ chấp nhận skip source/image/E2E/scan khi plan đã chứng minh không có service bị ảnh hưởng; không coi thiếu project/test là tài liệu-only.

[`../scripts/ci/suites.json`](../scripts/ci/suites.json) khai báo suite bắt buộc và suite chưa có. UI integration chưa có test mang integration tag; Catalog/Checkout chưa có unit suite độc lập. Các trường hợp này có trạng thái `not_applicable` và lý do cụ thể, không được báo là test đạt. Các suite unit/integration bắt buộc phải có ít nhất một test thực thi; zero test, tất cả bị skip, lỗi setup, thiếu/malformed report hoặc summary khác native report đều thất bại. Gate đọc lại JUnit XML, Jest JSON và Go JSON events sau khi tải artifact.

## Đúng bộ image đã được kiểm tra

Nx build mỗi runtime image `linux/amd64` đúng một lần, không push ở bước build. Mỗi archive trong `dist/ci/images` gắn với metadata chung của bộ image và entry riêng trong `metadata.json`:

- `source_commit`, run ID/attempt/URL, `platform`, `backend_profile`;
- service, repository/tag, tên archive, SHA-256 của archive và image config ID.

E2E, scan và publish kiểm tra lại đủ năm archive, metadata và checksum sau khi download. Không chấp nhận archive khác commit/run/attempt/platform, thiếu service, sai repository/tag hoặc config ID. Mọi archive được kiểm tra trước khi load vào Docker.

Compose CI override khai báo image cho đủ năm service, xóa build và đặt `pull_policy: never`; lệnh chạy có `--no-build`. Fixture database/broker/search vẫn được pull theo phiên bản cấu hình. Script kiểm tra trường `.Image` bất biến của container đang chạy, rồi chuẩn hóa về config digest để đối chiếu archive; không dùng tag có thể thay đổi làm bằng chứng identity.

`images[].image_id` luôn là **canonical image config digest**. Với Docker image store truyền thống, `.Id` chính là config digest. Docker 29 với containerd có thể trả manifest ID trong `.Id`; helper kiểm tra descriptor tương ứng rồi lấy annotation `config.digest`. Hash bytes của config trong archive vẫn phải khớp digest này. Không thay contract theo cách Docker store trình bày ID.

Kind load đúng các archive, đặt image pull policy `Never` và dùng các Helm chart hiện có. Script đọc imageID của Pod trên đúng node tạm rồi phân giải manifest/index về config digest trong containerd; không mặc định runtime manifest digest bằng local image ID. Có kiểm tra lại identity sau khi restart Orders.

Trivy quét OS và library của chính năm archive. HIGH/CRITICAL, lỗi scanner hoặc thiếu report đều chặn gate. Không có `continue-on-error`, bỏ qua vulnerability chưa sửa hay rebuild sau scan.

Publish chỉ load/push archive đã kiểm tra. Archive đã mang final tag theo commit/run/attempt, nên publisher không retag và không rebuild. Sau push, lấy registry digest, pull theo digest và so sánh canonical config digest với image đã E2E/scan. **Registry digest và image config digest là hai giá trị khác loại.** Chỉ ghi `images.json` khi push và đối chiếu thành công đủ năm service. Nếu push lỗi một phần, image có thể đã tồn tại trên registry nhưng không có release manifest hoàn chỉnh cho GitOps. Không dùng `latest` để triển khai.

Artifacts gắn cả run ID và attempt. Khi chạy lại, chọn **Re-run all jobs**; không trộn archive/report của attempt trước với attempt mới. Nếu archive đã hết hạn, chạy lại toàn bộ để tạo bộ image mới.

## Hợp đồng `images.json` cho GitOps

Schema chuẩn nằm ở [`../scripts/ci/images.schema.json`](../scripts/ci/images.schema.json). Artifact `gitops-images-<run>-<attempt>` chứa `images.json` và schema ở cùng thư mục gốc.

| Trường            | Hợp đồng                                                                    |
| ----------------- | --------------------------------------------------------------------------- |
| `schema_version`  | `1`                                                                         |
| `source_commit`   | Full commit SHA của checkout đã kiểm tra; PR dùng merge checkout của GitHub |
| `run`             | `id`, `attempt`, `url`                                                      |
| `platform`        | `linux/amd64`                                                               |
| `backend_profile` | `upstream`                                                                  |
| `images`          | Đủ và chỉ đủ `ui`, `catalog`, `cart`, `checkout`, `orders`                  |
| Mỗi image         | `repository`, `tag`, `digest`, `reference`, `image_id`                      |

Ví dụ dưới đây chỉ minh họa schema; các SHA không phải release đã publish. Script tạo một JSON hoàn chỉnh cho đủ năm service:

```python
import json

commit = "a" * 40
digest = "sha256:" + "b" * 64
image_id = "sha256:" + "c" * 64
repositories = {
    "ui": "devops-retails-shop",
    "catalog": "devops-retails-shop-catalog",
    "cart": "devops-retails-shop-cart",
    "checkout": "devops-retails-shop-checkout",
    "orders": "devops-retails-shop-orders",
}
release = {
    "schema_version": 1,
    "source_commit": commit,
    "run": {
        "id": "123",
        "attempt": "1",
        "url": "https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/actions/runs/123/attempts/1",
    },
    "platform": "linux/amd64",
    "backend_profile": "upstream",
    "images": {},
}
for service, name in repositories.items():
    repository = f"docker.io/hmannnguye/{name}"
    release["images"][service] = {
        "repository": repository,
        "tag": f"sha-{commit}-run-123-1",
        "digest": digest,
        "reference": f"{repository}@{digest}",
        "image_id": image_id,
    }
print(json.dumps(release, indent=2))
```

Consumer GitOps phải validate schema, đủ năm service và profile đã được nghiệm thu rồi render **`repository@sha256:...`**. Năm chart app hỗ trợ `image.digest`, ưu tiên digest khi có giá trị; `image.tag` chỉ dùng khi digest rỗng. Static check kiểm tra mỗi chart render được digest reference.

Profile hiện tại dùng Catalog MySQL/MariaDB, Cart DynamoDB Local, Orders PostgreSQL, Checkout Redis và RabbitMQ/OpenSearch. **PostgreSQL cho Catalog/Cart chưa được nghiệm thu.** Không đổi provider/endpoint rồi coi là migration. Khi adapter và migration thực sự có, cần cập nhật fixture, test, tracing, schema/profile và điều kiện chấp nhận bên GitOps riêng.

E2E hiện yêu cầu submit đơn qua backend thật, đọc số record PostgreSQL của Orders, restart ứng dụng và đối chiếu lại đơn hàng. Điều này không chứng minh EFS/RDS HA hoặc kiến trúc runtime AWS.

## Timeout, concurrency và artifacts

| Job                 | Timeout                                          |
| ------------------- | ------------------------------------------------ |
| Plan                | 20 phút                                          |
| Style/static        | 30 phút                                          |
| Source, mỗi service | 70 phút; mỗi lệnh build/lint/test tối đa 20 phút |
| Build năm image     | 60 phút                                          |
| E2E, mỗi mode       | 65 phút                                          |
| Scan                | 60 phút; Trivy tối đa 10 phút/image              |
| `ci-required`       | 15 phút                                          |
| Publish             | 45 phút                                          |

Readiness HTTP polling có deadline 300 giây; Compose `--wait-timeout` là 360 giây; Kind startup là 180 giây; Helm/rollout có deadline riêng. Cypress có timeout 20 phút. Logs và native reports được giữ để điều tra; timeout subprocess sẽ dừng process group. Script thu diagnostics và cleanup trong `finally`; workflow có cleanup dự phòng bằng `always()` cho tài nguyên runner tạm.

Lỗi P1 cũ là `kubectl wait ... || exit_code=$?` chỉ gán biến mà không trả lỗi cho job, khiến bước deploy có thể xanh dù Pod chưa ready. CI mới truyền lỗi readiness/rollout/timeout về job và gate; không dùng biến đó để nuốt lỗi.

PR có commit mới hủy run cũ cùng PR. Main/manual dùng group riêng theo run nên không bị run mới hủy toàn workflow. Publish được tuần tự hóa bằng group `dockerhub-retail-publish`, `cancel-in-progress: false`, bảo vệ publisher đang push. GitHub chỉ giữ một publisher pending trong group: một run mới hơn có thể thay run đang chờ, nhưng không hủy publisher đang chạy. Không dùng output chưa hoàn chỉnh làm release.

| Artifact                                              | Giữ     |
| ----------------------------------------------------- | ------- |
| Image archives                                        | 2 ngày  |
| Plan, metadata, source/E2E/scan/style reports và logs | 14 ngày |
| `images.json` + schema cho GitOps                     | 90 ngày |

`.gitattributes` quy định LF cho `mvnw`, `.sh` và các file CI; ba `mvnw` có executable bit trong Git. Static CI phải xác minh bằng clean Linux checkout, không suy từ quyền file trên Windows bind mount.

## Kiểm tra và kích hoạt

Từ app root, chạy contract tests bằng:

```bash
python3 -B -m unittest discover -s scripts/ci -p 'test_*.py' -v
```

Các test âm kiểm tra zero/all-skipped/missing/stale native evidence, summary mâu thuẫn report, checksum/config mismatch, PR publish, scanner lỗi và push lỗi một phần. Chúng kiểm tra logic CI, không thay thế test ứng dụng hoặc E2E.

| Kiểm tra đã quan sát                                          | Trạng thái                                                                                    |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Contract tests trên Windows                                   | 28 test đạt                                                                                   |
| Parse Python/TOML/JSON/YAML                                   | Đạt ở lượt kiểm tra local                                                                     |
| Compose config đủ năm image, không build, pull policy `never` | Đạt; chưa khởi chạy app stack                                                                 |
| Clean Linux checkout: executable bit và LF                    | Đạt trên snapshot local qua Git checkout-index                                                |
| Contract tests trên clean Linux snapshot                      | 28 test đạt                                                                                   |
| actionlint 1.7.7                                              | Đạt                                                                                           |
| Helm 3.19.0: lint và render digest cho năm chart              | Đạt                                                                                           |
| Docker 29.7.2 save/load archive của scratch fixture           | Đạt; chỉ kiểm tra hợp đồng archive/ID                                                         |
| Nx graph/affected thực tế trên Node 22 Alpine                 | Đạt: 14 projects; Cart chọn `cart`, README chọn rỗng; diff `HEAD~1..HEAD` chọn đủ năm service |
| Full build/test ứng dụng, Cypress/Kind, Trivy và push thật    | Chưa có bằng chứng chạy đạt                                                                   |
| Branch protection và thay đổi repository GitOps               | Chưa thực hiện                                                                                |

Chi tiết lệnh, phạm vi và giới hạn của kiểm tra local nằm ở [CI_VALIDATION.md](CI_VALIDATION.md).

Trước khi dùng làm release: xác nhận Docker Hub repositories/token, điền đúng Variable/Secrets, đưa workflow lên GitHub, chạy và xem bằng chứng của từng gate, rồi yêu cầu `ci-required` trên `main`. Không tuyên bố pipeline xanh hoặc release hoàn tất chỉ dựa vào kiểm tra cú pháp/contract tests.
