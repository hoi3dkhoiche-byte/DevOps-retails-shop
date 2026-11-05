# Chuẩn bị controller Linux/WSL

Installer [setup-controller.sh](../scripts/setup-controller.sh) cài công cụ vào workspace, không cài package global, không tự đăng nhập hoặc gọi API AWS. Mặc định `--check` chỉ đọc version và kiểm tra dependency local; `--install` mới tải/cài.

## Điều kiện và đường dẫn

Dùng Ubuntu/WSL x86_64 với glibc, Python **3.12**, module `venv`, Git, curl, GnuPG và coreutils (`sha256sum`, `install`, `getconf`, `awk`, `cut`, `mktemp`). Không cần unzip: script dùng `zipfile` của Python. Một distro mới có thể cần operator cài các prerequisite này bằng package manager trước; script không tự chạy sudo/apt.

Nên clone source vào filesystem Linux, ví dụ `~/work/DevOps-retails-shop`: việc cài/import hàng nghìn file Python và khởi động AWS CLI trên `/mnt/c/...` có thể chậm đáng kể. Nếu chạy checkout tại `/mnt/c/...`, các file cũng có thể bị xem là world-writable; installer xuất `ANSIBLE_CONFIG` tuyệt đối để Ansible nạp đúng cấu hình. Venv chứa đường dẫn tuyệt đối nên không copy venv giữa checkout hoặc giữa filesystem Windows/Linux. Không cấu hình profile lâu dài trỏ vào `/tmp`; khi đổi checkout, chạy lại installer và cập nhật đường dẫn CLI của process provider bên dưới.

```bash
cd /path/to/DevOps-retails-shop
bash scripts/setup-controller.sh --check
bash scripts/setup-controller.sh --install
source .ci-tools/controller/env.sh
bash scripts/setup-controller.sh --check
```

Nếu Ubuntu chưa có `ensurepip`/`python3.12-venv`, vẫn có thể chuẩn bị AWS CLI và ba công cụ hạ tầng trước:

```bash
bash scripts/setup-controller.sh --install-tools
source .ci-tools/controller/env.sh
aws --version
```

Mode này không chạm Ansible venv/collection; có thể đăng nhập bằng AWS CLI ngay. Khi operator đã bổ sung prerequisite venv của distro, chạy lại `--install` để hoàn thiện Ansible. Nếu `--install` dừng ở bước venv, công cụ đã cài vẫn sử dụng được qua `env.sh`; không xóa/chạy lại toàn workspace.

Các thư mục được ghi là `.ci-tools/controller` và `ansible/.venv`, `ansible/.collections`; chúng đã được Git ignore. Download cache và staging được giữ để điều tra; script không recursive-delete workspace. Không thay root/user home hoặc sửa shell startup. Nếu checksum cached file không đúng, script dừng và yêu cầu kiểm tra/xóa đúng một file đó trước retry.

| Công cụ | Pin |
| --- | --- |
| AWS CLI | 2.39.3, Linux x86_64, detached PGP signature và fingerprint AWS CLI Team |
| Terraform | 1.17.1, SHA256 Linux amd64 từ HashiCorp |
| Helm | 3.19.0, SHA256 Linux amd64 từ Helm |
| kubectl | 1.36.0, SHA256 Linux amd64 từ Kubernetes |
| Ansible/Python | `ansible/requirements.txt` với `--require-hashes`, CPython 3.12 |
| Kubernetes collection | `kubernetes.core` 6.6.0 từ `ansible/requirements.yml` |

Các checksum được pin trong script và lấy từ release publisher; đây là kiểm tra bytes với giá trị đã review, không gọi checksum tải cùng lúc là một chữ ký độc lập. AWS CLI dùng PGP public key có fingerprint `FB5DB77FD5C118B80511ADA8A6310ACC4672475C`, GnuPG keyring nằm trong workspace; private key/credential không nằm ở đó. Khi nâng pin cần review release, checksum/key/signature và lock tương thích trước khi sửa script. Key AWS hiện được tài liệu công bố hết hạn 01/07/2027, cần review key khi tới kỳ thay thế.

## Đăng nhập sau khi công cụ đã sẵn sàng

Cài công cụ không tạo account, IAM user hoặc role. Với account lab chưa dùng IAM Identity Center, admin chuẩn bị IAM user có quyền đăng nhập Console và `sts:AssumeRole` vào đúng role operator; role phải trust user đó và có quyền quản trị hạ tầng lab cần thiết. IAM user còn cần policy `SignInLocalDevelopmentAccess` để hoàn tất `aws login`, hoặc policy tương đương giới hạn `signin:AuthorizeOAuth2Access`/`signin:CreateOAuth2Token` vào public client phù hợp. Người dùng tự đặt mật khẩu/MFA trong Console. `admin_role_arn` của Terraform phải là ARN **IAM role này**, không phải ARN user hay root.

Dùng ba profile riêng để phân biệt danh tính:

| Profile | Công dụng | Danh tính STS mong đợi |
| --- | --- | --- |
| `retail-iam-console` | Phiên đăng nhập browser của IAM user | `arn:aws:iam::<ACCOUNT_ID>:user/<user>` |
| `retail-iam-session` | Process provider xuất credential tạm thời cho SDK | Cùng IAM user ở trên |
| `retail-operator` | Assume role quản trị lab từ phiên IAM | `arn:aws:sts::<ACCOUNT_ID>:assumed-role/<operator-role>/<session>` |

Các lệnh dưới dành cho profile mới, với role operator đã tồn tại. Thay `123456789012` và tên role bằng giá trị thật trước khi chạy:

```bash
source .ci-tools/controller/env.sh
ACCOUNT_ID=123456789012
OPERATOR_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/retail-lab-operator"

aws login --profile retail-iam-console --region ap-southeast-1
aws sts get-caller-identity --profile retail-iam-console
# Chỉ tiếp tục khi Account đúng và Arn là IAM user dự kiến, không phải :root.

AWS_CLI=$(command -v aws)
aws configure set credential_process "\"$AWS_CLI\" configure export-credentials --profile retail-iam-console --format process" --profile retail-iam-session
aws configure set region ap-southeast-1 --profile retail-iam-session
aws configure set role_arn "$OPERATOR_ROLE_ARN" --profile retail-operator
aws configure set source_profile retail-iam-session --profile retail-operator
aws configure set role_session_name retail-dev-operator --profile retail-operator
aws configure set duration_seconds 3600 --profile retail-operator
aws configure set region ap-southeast-1 --profile retail-operator
aws sts get-caller-identity --profile retail-operator
# Phải thấy assumed-role của đúng operator role và đúng Account trước preflight/apply.
export AWS_PROFILE=retail-operator
```

Browser mở luồng đăng nhập; chọn **IAM user**. Ô Account ID/account alias nhận **Account ID 12 chữ số** hoặc alias đã tạo trong IAM; tên hiển thị account trong trang Console không tự trở thành alias. Nhập đúng IAM username và tự hoàn thành mật khẩu/MFA. Nếu browser còn phiên root, đổi sang phiên IAM dự kiến trước khi xác nhận CLI. Không gửi access key, mật khẩu, MFA hoặc token qua chat.

`aws login` có thể hỏi `y/n` khi profile đã gắn với danh tính khác, ví dụ phiên root cũ. Chỉ trả lời `y` nếu chủ đích thay đúng profile browser đó bằng IAM user; nếu cần giữ phiên cũ, chọn `n` và dùng tên profile mới. Xác minh lại STS sau login. Với các profile process/role đã tồn tại, chọn tên mới hoặc review cấu hình trước; chuỗi trên không tự xóa provider cũ còn sót trong profile. Không dùng profile root làm `source_profile` của flow operator này. Nếu cần bootstrap IAM/state bằng phiên admin riêng, giữ ở profile khác và chỉ dùng cho bước nền đã review.

Process provider giúp Ansible/boto3/Terraform đọc credential tạm thời mà không phụ thuộc hỗ trợ `login_session` của từng SDK; role profile mới là danh tính dùng cho kubectl/Ansible. Ba profile phải khác nhau để tránh vòng lặp. Đường dẫn executable trong `credential_process` là tuyệt đối và được quote; phải còn tồn tại khi SDK chạy. Không chạy `aws configure export-credentials` riêng rồi chép kết quả ra file/chat: chỉ để SDK gọi lệnh đó qua process provider. Khi phiên IAM hết hạn, chạy lại `aws login --profile retail-iam-console`, không tạo static access key để thay thế.

Nếu account dùng IAM Identity Center, tạo profile riêng bằng `aws configure sso --profile retail-operator-sso` rồi `aws sso login --profile retail-operator-sso`; xác minh STS và dùng role được tổ chức cấp. Không trộn provider SSO, `login_session` và role vào cùng một profile. Sau khi có account ID/role thật, tiếp tục [AWS first deployment](AWS_FIRST_DEPLOYMENT.md) và [runbook Ansible](../ansible/README.md). `--check` của installer không chứng minh quyền hoặc kết nối AWS.

Nguồn chính thức: [AWS CLI install/signature](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), [AWS CLI sign in](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html), [role profiles và source_profile](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html), [process provider](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sourcing-external.html), [Terraform checksums](https://releases.hashicorp.com/terraform/1.17.1/terraform_1.17.1_SHA256SUMS), [Helm releases](https://github.com/helm/helm/releases/tag/v3.19.0), [kubectl installation](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/).
