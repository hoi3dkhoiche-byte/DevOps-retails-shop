# Xoay credential ứng dụng có kiểm soát

`rotate-credentials.yml` xoay **một credential của một service/môi trường** qua Job trong VPC, dùng cùng Lease theo account với backend bootstrap. Không đưa password vào inventory, Terraform state, command line hay JSON report. Không tự push GitOps hoặc restart pod ngoài Argo CD.

| Target | Cách đổi | Khả năng chuyển tiếp |
| --- | --- | --- |
| Catalog/Cart/Orders MySQL | ALTER USER với RETAIN CURRENT PASSWORD; đăng nhập TLS bằng password mới rồi mới publish | Giữ password cũ để ứng dụng đang chạy tiếp tục dùng |
| Checkout Redis | ModifyUser với hai password; chờ user active, AUTH/PING qua TLS; publish platform và application Secret | Giữ password cũ; hai Secret có cùng operation ID và có thể resume nếu promote dở |
| Orders RabbitMQ | Đổi password đúng user/vhost; mở kết nối AMQPS và vhost bằng password mới | Một password; bắt buộc maintenance acknowledgement |
| Catalog OpenSearch | PATCH riêng password, giữ role mapping; kiểm tra authentication HTTPS | Một password; bắt buộc maintenance acknowledgement |

Credential admin của RDS/Amazon MQ/OpenSearch, Grafana, Argo CD và token GitHub không nằm trong API rotation này; target không được hỗ trợ sẽ bị từ chối. RDS managed master dùng quy trình của AWS. Script không gọi bootstrap lại để giả làm rotation.

## 1. Điều kiện trước khi chạy

Controller Linux/WSL đã chuẩn bị theo [runbook Ansible](../ansible/README.md), đăng nhập đúng AWS operator; có output/receipt Terraform thật. Dev đã hoạt động, backup hiện hành và không có apply CloudFormation/bootstrap/rotation khác đang chạy. Giữ nguyên nguồn Terraform/GitOps đã review trong cả maintenance window.

Role Job cần quyền Secrets Manager Read/Put/UpdateSecretVersionStage cho application Secret được chọn và hai platform Redis Secret; ElastiCache DescribeUsers/ModifyUser cho `retail-checkout-dev/prod`, STS và DescribeCluster đúng cluster. Không cấp rotation token quyền IAM. Bên trong Job kiểm STS account, live EKS identity/endpoint và output ARN/endpoint trước đổi credential.

## 2. Plan mặc định chỉ đọc metadata

Từ `ansible/`, tạo UUID và ghi nó vào file operator local được ignore, không phải password:

```bash
python -c 'import uuid; print(uuid.uuid4())'
```

Bổ sung target vào `inventory/operator.yml` local đã được ignore, cùng account/profile hiện có:

```yaml
retail_rotation_environment: dev
retail_rotation_service: catalog
retail_rotation_backend: mysql
retail_rotation_operation_id: "<UUID vừa tạo>"
retail_rotation_execute: false
```

```bash
ansible-playbook rotate-credentials.yml -e @inventory/operator.yml
```

Plan chỉ gọi target guard/STS/DescribeSecret, không tải password hoặc sửa backend/Kubernetes. `--check` bị từ chối; plan là hành vi mặc định thực sự của helper. CLI helper chạy trực tiếp với `--execute` bị chặn nếu không ở bounded Job do runner khởi tạo; đây là guard chống chạy nhầm, không thay thế IAM và Lease.

## 3. Thực hiện đúng operation đã review

```bash
ansible-playbook rotate-credentials.yml -e @inventory/operator.yml \
  -e '{"retail_rotation_execute":true}'
```

Với RabbitMQ hoặc OpenSearch, dừng traffic của service theo quy trình maintenance trước, rồi thêm boolean `retail_rotation_ack_maintenance: true`. Các kết nối cũ có thể sống tiếp nhưng không được coi là bằng chứng không gián đoạn. Giữ maintenance tới khi ESO, rollout và acceptance hoàn tất.

Helper tạo version `AWSPENDING` bằng operation UUID. Metadata bên trong Secret ghi target và version gốc để resume/rollback. Sau khi backend nhận password mới, helper kiểm authentication bằng credential mới; chỉ khi đạt mới move `AWSCURRENT` từ đúng version đã đọc. Không ghi đè một version do tác nhân khác vừa publish. Giá trị cũ được giữ ở version gốc/`AWSPREVIOUS`; không tự discard old password.

**Nếu lỗi hoặc mất response:** giữ UUID, chạy lại cùng lệnh. Helper dùng lại pending password, kiểm backend trước khi apply và hoàn tất stage move còn thiếu. Với Redis, nếu platform Secret đã promote còn application Secret chưa, lần resume dùng đúng pending password để đồng bộ cả hai. Không tạo UUID mới để né một `AWSPENDING` đang có.

Operation mới cũng bị chặn khi credential AWSCURRENT còn mang metadata của operation trước nhưng chưa có `RETAIL_FINALIZED`. Điều này ngăn rotation thứ hai ghi đè slot password chuyển tiếp trước khi consumer của rotation thứ nhất đã reload. Với Redis, cả platform và application Secret phải đạt điều kiện trước khi tạo bất kỳ pending version mới nào.

Backend đã đổi nhưng publish thất bại có thể làm ứng dụng một-password tạm thời không đăng nhập được. Đây là trạng thái phục hồi dự kiến: tiếp tục operation hoặc rollback, không chạy bootstrap chung vì nó có thể đẩy ngược password cũ ngoài transaction này. Giữ Lease nếu runner chưa xác nhận Job dừng; dùng [Lease recovery](BOOTSTRAP_LEASE_RECOVERY.md) trước retry khi cần.

## 4. Đồng bộ ESO rồi phát hành secretRevision

Sau `status=success`, role tự gọi `ansible/helpers/prepare_rotation_candidate.py --wait-eso` của repo source để chờ ExternalSecret Ready/refresh metadata rồi đổi đúng `secretRevision` trong candidate cô lập. Candidate phải được review/PR/merge; không sửa canonical checkout hoặc patch Deployment trực tiếp. Nếu bước này lỗi, backend đã đổi thành công: giữ report `.ci-tools/ansible/rotation-<UUID>.json` và chỉ chạy lại helper với report đó, không tạo operation mới. Helper dùng `--source-dir`, `--gitops-dir`, `--local-dir`, `--candidate-dir`, `--terraform-output`, `--runner-report`, `--wait-eso`, `--context`, `--kubeconfig` và `--timeout`; đọc metadata/status thay vì in nội dung Kubernetes Secret.

Sync service qua Argo CD rồi chạy `verify.yml` và acceptance dev với đúng kubeconfig. Với UI không có backend credential target; không thay image digest trong PR rotation. Xác nhận pod mới đã đọc credential mới và không còn pod/revision cũ dùng password cũ trước khi đóng maintenance hoặc xem xét revoke old password.

## 5. Rollback credential

Giữ nguyên UUID/target, thêm execute và rollback:

```bash
ansible-playbook rotate-credentials.yml -e @inventory/operator.yml \
  -e '{"retail_rotation_execute":true,"retail_rotation_rollback":true}'
```

RabbitMQ/OpenSearch vẫn yêu cầu maintenance acknowledgement khi rollback. Helper kiểm lại version current chỉ thuộc operation/gốc rồi khôi phục backend và version AWS gốc; không dùng một `AWSPREVIOUS` bất kỳ đã bị operation khác thay thế. Tiếp tục ESO + GitOps secretRevision mới + acceptance như khi rotate. UUID đã rollback không được dùng để phát hành lại; operation mới dùng UUID mới sau khi giải quyết toàn bộ pending state.

Rollback không chỉ chuyển staging label: MySQL đặt lại password gốc làm primary rồi discard password lỗi; Redis chỉ giữ password gốc. Helper xác minh password lỗi không còn đăng nhập được trước khi đưa Secret về version gốc, ghi `RETAIL_ROLLED_BACK` và chặn dùng lại UUID đã thu hồi. Cách này giữ đúng password mà pod đang dùng khi thực hiện rotation tiếp theo. Giữ cửa sổ rollback ngắn và review quyền truy cập version cũ trong Secrets Manager.

## 6. Finalize và thu hồi password cũ

Sau khi ESO đã sync, PR secretRevision đã merge/sync, pod mới Ready và application acceptance đạt, thực hiện bước riêng có acknowledgement:

```bash
ansible-playbook rotate-credentials.yml -e @inventory/operator.yml \
  -e '{"retail_rotation_execute":true,"retail_rotation_finalize":true,"retail_rotation_ack_consumers_reloaded":true}'
```

Finalize yêu cầu cùng UUID đang AWSCURRENT ở toàn bộ target Secret, không còn AWSPENDING và không kết hợp rollback. MySQL chạy DISCARD OLD PASSWORD; Redis chỉ giữ password mới trong ModifyUser. Helper kiểm đăng nhập mới còn hoạt động và password cũ bị từ chối rồi mới ghi staging label metadata `RETAIL_FINALIZED`. Nếu mất response, chạy lại finalize cùng UUID: helper kiểm thực trạng để không phát hành password khác. Một phần marker Redis đã ghi thành công không ngăn phần còn lại được hoàn tất.

Finalize không tự tạo thêm GitOps candidate vì application đã reload trước bước này. Sau finalized, helper chặn rollback về password đã thu hồi; nếu có sự cố, mở operation mới hoặc dùng quy trình admin phục hồi có review, không kích hoạt lại credential nghi bị lộ. Phiên kết nối cũ có thể vẫn tồn tại; acceptance/acknowledgement phải xác nhận không còn consumer cũ, không chỉ dựa vào kiểm thử đăng nhập mới.

## 7. Drift và kiểm chứng thật

Redis platform password do CloudFormation tham chiếu động. Sau rotation, Secrets Manager có version mới nhưng CloudFormation không tự phát hiện nội dung Secret thay đổi. Đóng băng apply trong maintenance, review plan/stack trước apply tiếp theo; một update Properties có thể thay đổi danh sách password backend. Không copy giá trị Secret vào tfvars để giải quyết drift.

Unit tests kiểm staging, resume sau mất response, partial Redis publish, rollback, pending contention, target/region/account và không lộ secrets. Chúng chưa chứng minh backend AWS thật chấp nhận mọi câu lệnh với engine/quyền hiện hành. Nghiệm thu đầu tiên trên dev trước prod; đo downtime và quan sát lỗi authentication/persistence thực tế.

Nguồn: [MySQL dual passwords](https://dev.mysql.com/doc/refman/8.0/en/password-management.html), [ElastiCache ModifyUser](https://docs.aws.amazon.com/AmazonElastiCache/latest/APIReference/API_ModifyUser.html), [Secrets Manager rollback](https://docs.aws.amazon.com/secretsmanager/latest/userguide/roll-back-secret.html).
