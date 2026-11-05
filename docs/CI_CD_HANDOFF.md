# CI/CD và bàn giao triển khai

CI ứng dụng build, kiểm thử, scan và publish Docker Hub; GitOps nhận PR cập nhật digest. Terraform plan/apply và bootstrap Ansible do operator chủ động chạy. Push source không dựng AWS. Profile mặc định là `architecture`; việc đổi IAM operator sang AdministratorAccess không đổi quyền role Terraform CI.

## Workflow đang dùng

| Repo / workflow | Khi chạy | Đầu ra |
| --- | --- | --- |
| Source / `Retail CI` | PR, push main, manual | Năm image đã qua source test, ba E2E, scan; gate `ci-required`; `images.json` chứa digest |
| Source / `Retail GitOps` | Sau CI main thành công; production chạy manual | `gitops-configuration` báo trạng thái cấu hình; PR dev hoặc PR production đã nghiệm thu |
| Source / `Retail Terraform` | PR/push kiểm tra offline; plan/apply/teardown chỉ manual | Báo cáo test, saved plan/checksum, artifact deployment-context sau apply |
| Source / `Retail Ansible` | PR/push/manual | Helper tests có JUnit/counts, lint và syntax-check chín playbook; không có AWS credentials |
| GitOps / `GitOps validation` | PR, push main, manual | Gate `gitops-required`; kiểm tra release/proof, render chart và budget cả hai profile; reports/logs giữ 30 ngày |

CI ứng dụng đã có actionlint cho các workflow source và kiểm tra chart render digest. Không dùng các workflow lịch sử nằm trong `retail-store-sample-app/.github/workflows` làm entrypoint của repo hiện tại.

## Cấu hình GitHub Actions

Các tên sau thuộc **repo source** `hoi3dkhoiche-byte/DevOps-retails-shop`:

| Loại | Tên | Nội dung |
| --- | --- | --- |
| Variable | `DOCKERHUB_NAMESPACE` | `hmannnguye` |
| Secret | `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` | Danh tính/token publish Docker Hub; không chuyển sang Variables |
| Secret | `GITOPS_TOKEN` | Fine-grained PAT chỉ cho repo GitOps; Contents và Pull requests Read/Write |
| Variable | `GITOPS_ENABLED` | `true` khi Secret GitOps đã có; để trống/`false` thì chỉ tắt tạo PR, không tắt CI/publish |
| Variable | `TERRAFORM_ROLE_ARN` | `arn:aws:iam::527310641616:role/retail-lab-terraform`; không dùng role operator admin |
| Variable | `TERRAFORM_TFVARS_JSON` | Object tfvars hoàn chỉnh đã review; ARN/endpoint/IP thực tế, không chứa password/token |

`DOCKERHUB_NAMESPACE`, hai Docker Hub Secret, `TERRAFORM_ROLE_ARN`, Secret `GITOPS_TOKEN` và `GITOPS_ENABLED=true` đã được đối chiếu/cấu hình trong lượt cập nhật ngày 05/11/2026. Token do operator nhập trực tiếp vào GitHub Secret; chỉ kiểm tra tên Secret, không đọc lại giá trị. Operator còn chuẩn bị `TERRAFORM_TFVARS_JSON` cho lần deploy tiếp theo. Cờ GitOps chỉ được bật sau khi có Secret.

Tạo PAT tại [Fine-grained token](https://github.com/settings/personal-access-tokens/new?name=retail-gitops-actions&target_name=hoi3dkhoiche-byte&expires_in=30&contents=write&pull_requests=write): Resource owner `hoi3dkhoiche-byte`, Only select repositories → `DevOps-retails-shop-gitops`, Contents và Pull requests Read/Write. Lưu vào [Actions Secret của repo source](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/settings/secrets/actions/new), tên `GITOPS_TOKEN`. Không đưa token vào chat, Git, tfvars hoặc values. Khi token hết hạn, thay giá trị Secret này.

Hai repo hiện public nên không cần `SOURCE_READ_TOKEN`. Nếu chuyển private, cấp token đọc source cho GitOps validation và token đọc GitOps cho Ansible checkout; Argo CD còn cần repo credentials riêng trong AWS/ESO. `GITOPS_TOKEN` tạo PR không tự trở thành credential của Argo CD.

## Bằng chứng và production

Scan và `ci-required` đối chiếu báo cáo native Trivy với đúng image ID, Linux/amd64 và phiên bản scanner đã pin. Bộ image qua E2E/scan vẫn là bộ được publish; không rebuild trong job publish.

Terraform/Ansible helper suite lưu `results.json` và `junit.xml` cùng source SHA, run ID/attempt và số test. Suite rỗng hoặc hoàn toàn bị skip không được báo thành công. Reports giữ 14 ngày; fixture readiness/permission log được giữ trước cleanup, không upload raw backend logs chứa thông tin đăng nhập.

PR production cập nhật **cả** `environments/prod/images.yaml` và `environments/prod/accepted.json`. Proof là bản nghiệm thu dev của đúng năm digest được promote; nó vẫn hợp lệ khi dev chuyển sang release mới. GitOps validation kiểm tra proof của chính release production, nên sửa tay production image mà không có proof khớp sẽ fail. Rollback bằng revert cả hai file về cùng release đã nghiệm thu.

Check ổn định dùng cho bảo vệ main là `ci-required` ở source và `gitops-required` ở GitOps. Workflow Terraform/Ansible có path filter; không đặt tên job của chúng thành required check toàn cục khi không có job chọn/bỏ qua thay đổi chạy trên mọi PR.

## Lần triển khai AWS tiếp theo

1. Làm theo [AWS first deployment](AWS_FIRST_DEPLOYMENT.md), đối chiếu IAM/state đã giữ lại và Secret còn trong recovery. Operator tự chuẩn bị credential nền và tfvars thật trước plan. Preflight architecture yêu cầu account `PAID/ACTIVE`; code không nâng gói AWS.
2. Từ workflow Retail Terraform, chọn `plan`, review saved plan rồi chọn `apply` với đúng run/attempt/checksum. Không dùng kết quả CI ứng dụng làm bằng chứng Terraform đã apply.
3. Apply thành công ghi hướng dẫn artifact `deployment-context-<run>-<attempt>` trong job summary. Tải `terraform-outputs.json` và `metadata.json`, checkout đúng source commit trong receipt và làm theo [runbook Ansible](../ansible/README.md).
4. Bootstrap backend, review/merge GitOps candidate có AWS outputs thật, cài ESO/Argo CD và verify dev. Commit PR cập nhật image không tự thay thế các bước này.
5. Nghiệm thu chức năng dev trước promote production. Sau phiên lab, dùng [teardown giữ IAM/state](LAB_TEARDOWN.md); architecture còn deletion protection/final snapshot cần xử lý theo runbook.

Không có self-hosted deploy runner hay phiên AWS/cluster được dựng thêm trong lượt cập nhật CI/CD này.

Xem [đường truy cập ALB](ALB_ACCESS.md): khi chưa có domain, dev có HTTP ALB giới hạn IP quản trị; request/response của web app đi qua ALB, worker giữ trong private subnets. Production chỉ có Ingress sau khi release đã được nghiệm thu/promote.
