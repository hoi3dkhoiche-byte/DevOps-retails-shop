# Diễn tập khôi phục cô lập

`restore-drill.yml` và `scripts/infra/restore_backends.py` tạo kế hoạch trước, sau đó chỉ restore sang tài nguyên thử nghiệm mới. Không đổi endpoint ứng dụng, không ghi đè database/cache đang dùng và không promote production. Dùng operator AWS role có quyền restore, tag và dọn tài nguyên drill; CI Terraform/workload role không được dùng thay operator.

## Kế hoạch và kiểm tra đích

Controller cần cùng source/GitOps checkout, AWS session, kubeconfig và Terraform receipt như Ansible bootstrap. Helper tự kiểm receipt/source SHA, STS account, EKS ACTIVE/version/endpoint và kube context. Nguồn RDS/Redis phải khớp endpoint trong outputs; snapshot phải thuộc đúng account/region/source. RDS snapshot cần encrypted/available; Redis source phải TLS, encrypted và dùng ACL `retail-checkout`.

RDS/Redis được restore vào data subnet không có default route Internet/NAT, security group mới **không có ingress**. Giữ TLS/ACL và parameter group yêu cầu MySQL secure transport. Tên đích luôn có prefix `retail-drill-`; operation ID chỉ nhận 4–20 chữ thường/số/hyphen. Không nhận tên đích tự do để tránh ghi nhầm backend đang chạy.

Từ `ansible/`, ví dụ lập plan RDS bằng snapshot ID đã lấy từ AWS:

```bash
ansible-playbook restore-drill.yml -e @inventory/operator.yml \
  -e '{"retail_restore_kind":"rds","retail_restore_operation_id":"lab20261104","retail_restore_source_backup":"rds:retail-lab-mysql-<snapshot-time>"}'
```

Thay `rds` bằng `redis` và dùng tên snapshot Redis; hoặc dùng `velero` với tên backup `retail-daily` đã Completed. Không dùng placeholder nguyên văn. Chế độ mặc định chỉ gọi API read-only, tạo plan/report **ở máy operator**; không tạo AWS/Kubernetes resource.

Xem `.ci-tools/ansible/restore-plan.json`: account/region, source snapshot ARN/timestamp hoặc Velero UID/archive hash, network/class, đích mới và `plan_sha256`. Plan không có password, token, SecretString hoặc giá trị ConfigMap. Mỗi loại restore/operation nên có đường dẫn plan/report riêng để giữ bằng chứng.

## Thực thi đúng plan đã review

```bash
ansible-playbook restore-drill.yml -e @inventory/operator.yml \
  -e '{"retail_restore_execute":true,"retail_restore_approved_sha256":"<SHA256-vừa-review>"}'
```

Plan cần đúng checksum và không quá 24 giờ. Helper kiểm lại source snapshot và topology trước khi ghi. Thay đổi plan, account, source backup bị thay thế hoặc khác đích sẽ dừng. Có thể chạy lại cùng plan để tiếp tục chờ resource đã tạo nếu tag `RetailDrill` + `RetailPlan` vẫn khớp; không tạo duplicate hoặc tự sửa tài nguyên không thuộc operation đó.

Thời gian chờ mặc định 3600 giây, tùy chọn `retail_restore_timeout` từ 60–7200. Khi timeout/API lỗi, giữ resource/report để điều tra, không tự dọn hoặc thử overwrite nguồn. Tài nguyên drill có thể tiếp tục tính phí cho đến khi cleanup thành công.

### RDS/Redis: phạm vi bằng chứng

Helper chờ resource available và kiểm private/encryption/SG/TLS/ACL. Report ghi `functional_data_validation=not_run`: **resource available chưa chứng minh SQL/table/checksum hoặc cache key/value đã khôi phục đúng**. Thời gian `elapsed_seconds` chỉ đo phần API restore/readiness, không phải RTO ứng dụng đã được nghiệm thu.

Để nghiệm thu dữ liệu, operator cần một client kiểm tra trong VPC, được kiểm soát riêng. Review quyền kết nối tạm vào **SG drill** từ đúng client SG, chỉ cổng 3306 hoặc 6379; không mở public CIDR, không thêm quyền vào SG backend thật. Dùng credential phù hợp với snapshot qua Secrets Manager, không truyền password trên command line hay ghi vào report. Chạy truy vấn read-only: đếm schema/tables và row/checksum cần thiết, phân tách dev/prod; với Redis kiểm ACL, prefix và key/value mẫu đã biết trước backup. Ghi snapshot timestamp, thời điểm dữ liệu mẫu và thời gian chức năng phục hồi để tính RPO/RTO. Gỡ quyền kết nối tạm trước cleanup; helper từ chối SG còn ingress.

Không có client/test dataset thật thì giữ kết quả ở mức API restore, không tự tạo bằng chứng thành công chức năng. Cutover khi sự cố thật cần quy trình incident được review: chọn restore point, xác minh dữ liệu, đổi cấu hình endpoint bằng GitOps, kiểm tra và rollback; helper drill không tự làm các bước này.

Không giả định mật khẩu master trong snapshot luôn khớp version AWSCURRENT của nguồn. Helper MySQL không dùng `ManageMasterUserPassword` trực tiếp trong RestoreDBInstanceFromDBSnapshot vì API hiện ghi giới hạn tham số đó cho Oracle. Nếu cần credential quản trị riêng để đọc dữ liệu trên clone, operator xác minh đúng ID/tag của **DB drill** rồi bật managed password bằng ModifyDBInstance trên clone đã available; đọc ARN `MasterUserSecret` của clone sau khi cập nhật hoàn tất. Không sửa master credential của DB nguồn và không truyền password qua CLI. [Restore API constraints](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_RestoreDBInstanceFromDBSnapshot.html), [Modify managed password](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_ModifyDBInstance.html)

### Velero: phục hồi nội dung thật nhưng giới hạn resource

Chỉ restore `ConfigMap` từ `retail-dev` sang namespace drill mới, không Secret, PV/PVC, Deployment, Service, AppProject/Application hay resource cluster-wide. Tạo NetworkPolicy deny-all trong namespace mới. Snapshot plan ràng buộc UID backup và SHA256/ETag archive S3; xác minh lại trước ghi. Sau `Restore=Completed`, helper so sánh hash nội dung dev ConfigMap với archive thật; khác/thiếu/dư đều fail. `kube-root-ca.crt` do Kubernetes tự sinh được loại khỏi phép so sánh để tránh phụ thuộc thời điểm controller tạo CA.

Đây là bằng chứng restore Kubernetes configuration, không phục hồi EBS/RDS/Redis. Cấu hình Velero hiện chủ đích tắt EBS snapshots và node-agent. Namespace đã có mà không thuộc đúng operation/plan sẽ bị từ chối; không restore vào namespace ứng dụng đang dùng. `--check` không chạy rehearsal; chỉ plan mặc định là chế độ xem trước có API read-only.

## Dọn đúng tài nguyên drill

Sau khi lưu bằng chứng và gỡ mọi ingress tạm:

```bash
ansible-playbook restore-drill.yml -e @inventory/operator.yml \
  -e '{"retail_restore_execute":true,"retail_restore_cleanup":true,"retail_restore_approved_sha256":"<SHA256-của-plan>"}'
```

Cleanup yêu cầu đúng account/plan/tag, chỉ xóa resource tên drill và SG riêng; không xóa snapshot gốc. RDS drill được xóa với skip-final-snapshot có chủ đích vì đây là bản sao thử nghiệm. Redis drill không giữ lại primary cluster. Namespace/Restore Velero được xóa bằng UID/resourceVersion preconditions sau khi controller đã terminal; không xóa namespace đang có restore active. Plan quá 24 giờ vẫn có thể cleanup tài nguyên owned, nhưng không thể tạo restore mới.

Nếu cleanup bị từ chối do tag/UID/version khác, dừng và kiểm tra; không đổi tag hoặc bỏ guard để xóa. Nếu API xóa đã hoàn tất nhưng report bị ngắt, xác minh resource đã không còn trước khi xử lý các phần còn sót bằng operator. Không `terraform destroy` cả lab để dọn drill. Operation này không được thêm vào scheduled CI.

Nguồn: [RDS restore API](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_RestoreDBInstanceFromDBSnapshot.html), [Redis replication group restore](https://docs.aws.amazon.com/AmazonElastiCache/latest/APIReference/API_CreateReplicationGroup.html), [Velero Restore v1.18](https://velero.io/docs/v1.18/api-types/restore/).
