# Chuẩn bị AWS và chạy Terraform lần đầu

Sau khi apply và tải artifact deployment-context, dùng [runbook Ansible](../ansible/README.md) để bootstrap backend, review GitOps candidate và cài ESO/Argo CD. Ansible controller cần Linux/WSL với phiên AWS operator riêng.

Tài khoản đã có nhưng máy hiện chưa có AWS CLI/profile. Có thể bắt đầu phần bootstrap tài khoản bằng **AWS CloudShell trong Console**, không cần gửi access key hoặc token qua chat. Terraform và Ansible chưa tạo tài nguyên thật trong đợt hoàn thiện code này.

## 1. Vào đúng account và Singapore

Đăng nhập AWS Console bằng danh tính quản trị của bạn, chọn `ap-southeast-1`, mở CloudShell. Chạy:

```bash
aws sts get-caller-identity
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
git clone https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop.git
cd DevOps-retails-shop
```

Clone source sau khi các thay đổi mới đã được push. Nếu chưa có quyền tạo IAM/VPC/EKS thì dùng danh tính quản trị phù hợp; không tạo access key root để chép vào GitHub. Các lệnh dưới đây là bước operator thực hiện sau khi đọc policy, không được workflow app CI tự chạy.

## 2. Chuẩn bị state bucket trước Terraform init

Code hiện dùng bucket `manhnguyen1131`, key `retail/lab/terraform.tfstate`. Kiểm tra:

```bash
aws s3api head-bucket --bucket manhnguyen1131 --expected-bucket-owner "$ACCOUNT_ID" --region ap-southeast-1
```

Nếu bucket chưa tồn tại và tên còn dùng được, tạo bằng account này trong Singapore. Nếu thuộc account khác hoặc bị AccessDenied, dừng để xác định nguyên nhân; không coi lỗi quyền là không tồn tại. Khi chọn tên khác, đổi đồng bộ bucket backend trong `versions.tf`, resource versioning trong `backends.tf`, hằng `STATE_BUCKET` trong `preflight.py` và đối số policy renderer, rồi review/commit trước khi chạy workflow.

```bash
# Chỉ chạy khi đã xác nhận bucket chưa tồn tại và tên có thể dùng trong account này.
aws s3api create-bucket --bucket manhnguyen1131 --region ap-southeast-1 --create-bucket-configuration LocationConstraint=ap-southeast-1
```

Với bucket đúng owner, bật các điều kiện bắt buộc:

```bash
aws s3api put-bucket-versioning --bucket manhnguyen1131 --expected-bucket-owner "$ACCOUNT_ID" --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket manhnguyen1131 --expected-bucket-owner "$ACCOUNT_ID" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket manhnguyen1131 --expected-bucket-owner "$ACCOUNT_ID" --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

Không đặt expiration cho state/lock. Preflight kiểm tra owner, region, versioning, public access block và encryption bằng API read-only. Quyền ghi/xóa lock thật chỉ được chứng minh khi `terraform plan` lấy và trả `.tflock` thành công.

## 3. Tạo policy và trust để review

```bash
python3 scripts/infra/render_terraform_policy.py --account-id "$ACCOUNT_ID" --state-bucket manhnguyen1131
```

Kết quả nằm trong `.ci-tools/aws-bootstrap/`: các policy JSON, `trust.json` và `manifest.json`. Mỗi phần tối đa 6144 ký tự JSON không khoảng trắng. Manifest có primary ARN và additional ARNs để điền tfvars. Renderer không gọi AWS và không cấp quyền.

Đọc toàn bộ policy trước khi dùng. Đây là policy **hạ tầng có quyền quản lý IAM của project**, không phải policy app CI. VPC/network create/relationship operations và một số read/list dùng Resource `*` với giới hạn region do resource mới chưa có ID; Route 53/ACM chỉ dùng khi có domain. Hãy thu hẹp zone hoặc bỏ nhóm domain nếu không cần. Không coi template là bản đã được IAM Access Analyzer nghiệm thu.

Đã đối chiếu tên action và khả năng scope với JSON Service Authorization Reference công khai của AWS. `mq:CreateBroker` và `acm:RequestCertificate` không hỗ trợ resource ARN nên được tách vào statement Resource `*` giới hạn region; ARN wildcard không thay thế được yêu cầu này. Quyền ENI permission của MQ có thêm điều kiện `ec2:AuthorizedService`. Đây là kiểm tra metadata công khai, chưa phải mô phỏng hiệu lực policy trong account có SCP/boundary/bucket policy. [AWS service reference](https://docs.aws.amazon.com/service-authorization/latest/reference/service-reference.html)

Kiểm tra từng file bằng Access Analyzer trong account:

```bash
aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY --policy-document file://.ci-tools/aws-bootstrap/retail-lab-terraform.json
```

Lặp lại với các phần `-02`, `-03` nếu có. Sửa mọi ERROR trước khi tạo policy; review các finding còn lại theo phạm vi lab. Tạo từng managed policy bằng `aws iam create-policy --policy-name <name-trong-manifest> --policy-document file://<file>`. Nếu policy đã tồn tại, review version hiện hành rồi cập nhật có chủ đích; không tạo duplicate hoặc tự attach AdministratorAccess để chữa lỗi.

## 4. GitHub OIDC và Terraform role

Kiểm tra OIDC provider `arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com`. Nếu thực sự chưa có, tạo:

```bash
aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com
aws iam create-role --role-name retail-lab-terraform --max-session-duration 7200 --assume-role-policy-document file://.ci-tools/aws-bootstrap/trust.json --tags Key=Project,Value=retail-lab Key=ManagedBy,Value=terraform
```

Chỉ dùng hai lệnh create cho resource chưa tồn tại. Với resource đã có, xác minh provider URL/audience và role trust trước khi reuse. Trust được pin vào `repo:hoi3dkhoiche-byte/DevOps-retails-shop:ref:refs/heads/main`, audience `sts.amazonaws.com`.

Attach từng policy ARN trong manifest vào `retail-lab-terraform`. Role/policy attachments đã được bootstrap ngoài Terraform cần import trước lần apply đầu: primary vào `aws_iam_role_policy_attachment.terraform`, các phần bổ sung vào `aws_iam_role_policy_attachment.terraform_additional["<policy-arn>"]`, role vào `aws_iam_role.terraform`. Định dạng import attachment là `<role-name>/<policy-arn>`. Operator có thể dùng Terraform container hoặc CLI đã pin để import, sau khi init backend đúng account.

Policy mới **Deny thao tác ghi lên chính role CI** và policy `retail-lab-terraform*`; CI chỉ refresh/read chúng. Vì root Terraform vẫn theo dõi các resource đã import này, operator phải chuẩn bị đúng trust, session 7200 giây, mọi attachment trong manifest và hai default tag trước lần CI apply đầu. Với role cũ chưa có tag, dùng phiên admin riêng:

```bash
aws iam tag-role --role-name retail-lab-terraform --tags Key=Project,Value=retail-lab Key=ManagedBy,Value=terraform
```

Nếu plan định sửa role/trust/tag/attachment của chính CI, dừng để admin review và apply phần nền bằng phiên admin rồi tạo lại plan CI; không bỏ Deny để CI tự cấp quyền. Cập nhật policy parts phải render/review/attach đủ manifest mới bằng admin; số phần có thể thay đổi. Không dùng role CI để chạy các lệnh bootstrap IAM này.

Các managed policy được CI attach vào role project đã được giới hạn vào 7 policy EKS/node/EBS đang dùng. Đây là biện pháp bảo vệ trực tiếp role CI, **chưa phải mô hình chống mọi đường nâng quyền**: quyền inline policy/trust trên role workload vẫn là delegation IAM có quyền cao. Muốn giới hạn toàn diện cần boundary bắt buộc do admin quản lý trên mọi role được CI tạo, hoặc tách IAM sang stack admin; chưa triển khai hai thay đổi kiến trúc đó trong lab này. [AWS permissions boundaries](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html)

Terraform role, service-linked role do Terraform tạo và versioning của state có `prevent_destroy`. Khi dọn lab, giữ các thành phần nền này; nếu muốn đổi quyền quản lý thì dùng quy trình state handoff được review, không tắt bảo vệ và chạy destroy toàn bộ theo thói quen.

## 5. Credential nền và inputs thật

Làm theo [infra runbook](../scripts/infra/README.md) để chạy `init-platform` bằng quyền operator. Bốn platform Secret phải có AWSCURRENT trước plan; chỉ ARN đi vào tfvars. Chạy resolver chọn add-on tương thích EKS tại Singapore. Điền:

- Public IPv4 `/32` thật của máy sẽ quản trị EKS; IP tài liệu/private không được chấp nhận.
- Role admin có thật, OIDC provider ARN, primary/additional policy ARNs từ manifest.
- Bốn ARN platform Secret; pin add-on builds từ API.
- Domain/hosted zone nếu dùng; có thể giữ rỗng cho lab.

`admin_role_arn` là role của người sẽ chạy kubectl/Ansible, không phải root ARN, IAM user ARN hoặc role CI build image. Nếu account mới chỉ có root login, thiết lập một danh tính quản trị ngắn hạn qua IAM Identity Center hoặc role operator phù hợp trước; lấy ARN role thực tế từ IAM. Preflight kiểm tra role tồn tại, OIDC có đúng URL/audience và các policy có thể attach trước khi tạo EKS. Không điền một ARN tự đặt chưa có trong account.

Tại repo source, đặt Actions Variables `TERRAFORM_ROLE_ARN` và `TERRAFORM_TFVARS_JSON`. JSON chỉ có cấu hình/ARN; không có password. CloudShell khởi tạo tài khoản không thay thế danh tính AWS của máy chạy kubectl/Ansible. Trước giai đoạn đó, cài AWS CLI v2 trên WSL/Linux và thiết lập đăng nhập ngắn hạn phù hợp với IAM/SSO của account; không chép static keys vào inventory.

## 6. Validate → plan → apply → outputs

Workflow **Retail Terraform** có ba operation:

1. `validate`: test code và fixture, không cần AWS role.
2. `plan`: đăng nhập OIDC, kiểm tra account/state/capacity, tạo saved plan. Đọc diff và SHA256.
3. `apply`: chọn đúng run/attempt/SHA256 của plan đã review. Chưa có plan thì chưa apply.

Sau apply, artifact `deployment-context-<run>-<attempt>` chứa `terraform-outputs.json` đã allowlist cùng metadata/checksum. Không có state hoặc giá trị password/token. Dùng outputs này cho runner bootstrap và chuẩn bị GitOps. Backend bootstrap hỗ trợ `--kubeconfig` và `--report`, có khóa chống chạy đồng thời; xem hướng dẫn recovery trong runbook trước khi xóa lease.

Nghiệm thu cuối vẫn cần cold apply thực tế, bootstrap thành công, dev đúng digest, persistence/isolation, MQ failover và restore. Test offline không chứng minh capacity, quota, IAM hoặc RTO/RPO của account.

Nguồn: [S3 backend locking](https://developer.hashicorp.com/terraform/language/backend/s3), [GitHub OIDC cho AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws), [IAM policy quotas](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html), [CloudShell](https://docs.aws.amazon.com/cloudshell/latest/userguide/welcome.html).
