# Nghiệm thu monitoring và backup

`ansible/verify-operations.yml` thu bằng chứng runtime về Prometheus, Alertmanager, Velero và snapshot backend. Mặc định chỉ đọc API/S3; không tạo backup, không restore, không gửi thông báo và không tạo `accepted.json` để promote ứng dụng.

## Điều kiện

Trên controller Linux/WSL đã cài dependency lock Ansible, đăng nhập AWS bằng operator role và có kubectl. Cần Terraform artifact `terraform-outputs.json` + `metadata.json`, canonical GitOps checkout và source đúng infrastructure commit của receipt. Helper gọi lại preflight receipt/hash/account/EKS/context trước các API khác; không hỗ trợ bỏ qua receipt cho thao tác vận hành này.

Platform và dev cần được bootstrap/sync trước. Nếu chưa tới lịch backup đầu tiên, kiểm tra này sẽ báo thiếu backup thay vì coi trạng thái đó là thành công. Tạo backup theo Schedule đã review hoặc chờ lịch chạy; không sửa freshness limit chỉ để che backup đang thất bại.

```bash
cd ansible
ansible-playbook verify-operations.yml -e @inventory/operator.yml
```

Các biến có thể đặt trong file operator:

```yaml
retail_ops_max_backup_age_hours: 36
retail_ops_require_alerting: false
retail_ops_send_test_alert: false
retail_ops_report: "{{ retail_local_dir }}/operations-report.json"
```

## Những gì được kiểm tra

- Prometheus `/-/ready`, API targets và query engine. Cả năm PodMonitor dev phải có target hoạt động/fresh: `ui`, `catalog`, **`carts`** theo label chart Cart, `checkout`, `orders`. Không đòi target production khi production chưa triển khai.
- Alertmanager `/-/ready`, API status và sự hiện diện của external receiver. Cấu hình có credential chỉ được đọc trong bộ nhớ; report không chứa URL/token/password hoặc nguyên bản Alertmanager config. `retail_ops_require_alerting=true` làm gate thất bại nếu chưa cấu hình receiver.
- Backup mới nhất thuộc Schedule `retail-daily` phải `Completed`, không errors, có resource và không quá hạn. Một lần backup mới thất bại không bị che bởi lần thành công cũ.
- Đọc archive S3 thật từ bucket account-scoped, ràng buộc ETag, giới hạn 64MiB compressed/512MiB expanded, không extract file ra đĩa. Kiểm tra archive có Kubernetes resource/ConfigMap và không có Secret. Report chỉ ghi đếm/hash/metadata, không dữ liệu resource.
- Snapshot RDS và Redis automated phải có thật, đúng nguồn/account/region, available và fresh. Snapshot RDS phải encrypted. Đây là bằng chứng backup tồn tại; chưa chứng minh dữ liệu đã được restore và sử dụng thành công.

Report mặc định tại `.ci-tools/ansible/operations-report.json` (Git ignored). `status=success` chỉ xác nhận phạm vi đọc bên trên. Report luôn ghi `restore_proven=false`, còn notification delivery là `not_tested` nếu không chạy phép gửi riêng.

## Thử kênh thông báo thật

Sau khi receiver đã cấu hình trong Secrets Manager/ESO và Alertmanager áp dụng, dùng operation ID riêng:

```bash
ansible-playbook verify-operations.yml -e @inventory/operator.yml \
  -e '{"retail_ops_require_alerting":true,"retail_ops_send_test_alert":true,"retail_ops_operation_id":"notify-lab-20261104"}'
```

Đây là thao tác ghi được yêu cầu rõ ràng: helper POST một alert `RetailOpsAcceptance`, severity `info`, hết hạn sau 5 phút, label `retail_operation` bằng operation ID. Không gửi mặc định. Đảm bảo routing của receiver nhận severity/label này; việc có receiver trong config không chứng minh alert đã đi vào receiver đó.

**Alertmanager chấp nhận POST không chứng minh người nhận đã nhận thông báo.** Lượt gửi trả `notification_pending`, exit code 2; playbook cố ý không báo acceptance thành công. Operator kiểm tra email/Telegram/hệ thống nhận thật và ghi bằng chứng public message ID/ticket, operation ID, thời gian gửi/nhận và người xác nhận bên cạnh report. Không đưa receiver token, địa chỉ riêng hoặc nội dung credential vào Git. Chỉ coi phép thử delivery đạt sau khi có xác nhận thực tế này; không sửa JSON thành success bằng tay.

Không chạy playbook bằng `--check` để giả kiểm thử offline. Kiểm thử code dùng unit tests; kiểm thử runtime cần cluster thật. Bằng chứng deployment ứng dụng vẫn đi qua `verify.yml` và dev acceptance helper riêng.

Nguồn: [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/), [Alertmanager API](https://github.com/prometheus/alertmanager/blob/main/api/v2/openapi.yaml), [Velero backup format](https://velero.io/docs/v1.18/output-file-format/).
