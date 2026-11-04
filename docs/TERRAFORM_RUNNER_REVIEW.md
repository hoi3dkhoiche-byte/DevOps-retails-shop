# Đối chiếu Terraform CI role và backend runner

Rà soát ngày 04/11/2026 trên baseline source `c09095dffc87fae2fe8674271113a0c18001a9c6` và thay đổi local sau đó. Danh sách cũ mục 1–10 đã xử lý trước baseline này; bảng dưới chỉ ghi các phát hiện A–E mới. Chưa đăng nhập account để chạy IAM simulator, describe-broker hoặc cold apply.

| Điểm | Kết luận | Thay đổi và bằng chứng |
| --- | --- | --- |
| A — RabbitMQ `.on.aws` | Lỗi thật | Runner đã nhận cả `.mq.ap-southeast-1.amazonaws.com` và `.mq.ap-southeast-1.on.aws`; vẫn yêu cầu AMQPS/5671, không credentials/path/query/fragment. Test cả flat/native outputs, sai region/đuôi giả và luồng ghi địa chỉ vào Secret/management API |
| B — `ec2:RunInstances` | Thiếu quyền thật | Thêm statement riêng giới hạn Singapore và `ec2:LaunchTemplate` trong account. `CreateTags` đã có. Chưa dùng `ec2:IsLaunchTemplateResource=true` vì EKS còn bổ sung AMI/subnet/instance profile ngoài template |
| C — S3 refresh | Lỗi thật với provider đã pin | Provider AWS **6.71.0** đọc accelerate và replication khi refresh bucket. Đã thêm `s3:GetAccelerateConfiguration`, `s3:GetReplicationConfiguration` cho đúng hai bucket logs/backup |
| D — tự nâng quyền | Rủi ro thật, đã giảm nhưng chưa đóng toàn diện | Deny self-role mutation/PassRole, Deny sửa policy parts của CI, giới hạn managed attachments vào 7 AWS policy đang dùng. Inline policy/trust delegation trên các role project khác vẫn có quyền cao; cần boundary bắt buộc do admin quản lý hoặc stack IAM riêng để đóng mọi đường gián tiếp |
| E1 — tên output endpoint S3 | Lỗi thật | Exporter dùng đúng `s3_gateway_endpoint_id`. Test đối chiếu toàn bộ allowlist với các output Terraform và từ chối entry sensitive |
| E2 — hash/wheel Alpine | Chưa phát hiện lỗi dependency | Chạy đúng image digest của Job với UID1000, read-only rootfs, `/tmp` executable, 512MiB/0.5CPU: pip `--require-hashes` thành công, native imports và crypto smoke đạt. Thêm gate lặp lại trong validation workflow |
| E3 — Lease bị giữ | Hành vi fail-closed có chủ đích | Không tự chiếm Lease hết hạn. Thêm runbook dừng launcher/Job đúng holder, xác nhận writer dừng và mở khóa bằng UID/resourceVersion/holder JSON Patch |

## Căn cứ chính

Amazon MQ liệt kê hai dạng hostname IPv4/dual-stack. Việc chỉ whitelist `.amazonaws.com` sẽ chặn broker có endpoint `.on.aws` trước khi Job được tạo. Sau deploy thật, kiểm tra endpoint được trả về bằng `aws mq describe-broker --broker-id <id> --region ap-southeast-1 --query 'BrokerInstances[].Endpoints'`. [AWS MQ endpoint formats](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/connect-to-amazonmq.html), [RabbitMQ getting started](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/getting-started-rabbitmq.html)

EKS yêu cầu identity gọi CreateNodegroup/UpdateNodegroupVersion có RunInstances và CreateTags khi sử dụng launch template/tag specification. EC2 có ví dụ giới hạn RunInstances bằng `ec2:LaunchTemplate`; implementation dùng account/region cố định thay vì quyền launch vô điều kiện. Đây là policy cần thiết theo tài liệu, chưa phải bằng chứng tài khoản hiện tại đã cho phép qua SCP/boundary. [EKS launch templates](https://docs.aws.amazon.com/eks/latest/userguide/launch-templates.html), [EC2 policy examples](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ExamplePolicies_EC2.html#ec2-launch-templates)

Đã đọc đúng source provider v6.71.0: `resourceBucketRead` gọi `findBucketAccelerateConfiguration` và `findBucketReplication`; AccessDenied ở các nhánh này không được xem là bucket không có cấu hình. Wildcard `s3:GetBucket*` không cấp hai action có tên khác. [Provider source v6.71.0](https://github.com/hashicorp/terraform-provider-aws/blob/v6.71.0/internal/service/s3/bucket.go#L984)

## Giới hạn và cách áp dụng phần IAM

CI role là hạ tầng có quyền cao; manual dispatch và review plan không phải IAM boundary. Self-role Deny chặn trực tiếp attach AdministratorAccess, thêm inline policy hoặc sửa trust của chính `retail-lab-terraform`. Allowlist managed attachments chặn việc attach managed policy ngoài bảy policy EKS/node/EBS đang dùng.

Biện pháp này **không ngăn CI ghi inline Administrator policy vào một role project khác rồi sửa trust hoặc sử dụng role đó qua dịch vụ**. Không được mô tả template mới là mô hình chống mọi đường nâng quyền. Muốn đóng rủi ro gián tiếp cần thay đổi quyền quản lý IAM: mandatory boundary trên toàn bộ role được delegation, boundary do admin sở hữu và không thể bị sửa/gỡ; hoặc đưa IAM ra stack admin riêng. [AWS permissions boundaries](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html)

Root hiện vẫn theo dõi self-role và attachments đã import. Phiên admin phải tạo/reconcile đúng trust, max session 7200, hai default tag và toàn bộ policy parts trước khi CI chạy; no-op refresh được phép. Bất kỳ plan nào sửa các resource nền này phải được admin xử lý rồi tạo lại plan CI. Nếu chỉ thêm Deny nhưng giữ role bootstrap thiếu tag, lần CI apply đầu sẽ lỗi TagRole; runbook đã sửa cả lệnh create-role và tag-role cho role có sẵn. Xem [AWS first deployment](AWS_FIRST_DEPLOYMENT.md).

Policy renderer hiện tách ba phần; tất cả đều dưới giới hạn 6144 ký tự JSON không khoảng trắng. Render lại và đọc manifest thay vì giả định chỉ có hai phần cũ. Thay file template local không tự cập nhật policy đã attach trên AWS.

## Kiểm chứng đã thực hiện

- 87 test Python hạ tầng đạt, gồm các case mới endpoint, output contract và policy guard.
- Terraform fmt/validate và 20 native test dùng mock provider đạt; không gọi AWS.
- Chạy thành công `scripts/infra/dependency_smoke.py` với đúng pinned Alpine image và lệnh installer được chia sẻ trực tiếp từ Job.
- Workflow Terraform có gate Alpine mới và qua actionlint.
- Các action mới khớp Service Authorization Reference công khai của AWS; chưa chạy Access Analyzer/Policy Simulator trong account.

Các kiểm tra offline không chứng minh quota/capacity, hiệu lực policy tổng hợp trong account hoặc bootstrap/restore thật. [Lease recovery runbook](BOOTSTRAP_LEASE_RECOVERY.md) là thao tác operator, không tự chạy trong validation.
