# Kế hoạch hoàn thiện Terraform và Ansible cho Retail AWS lab

Ngày rà soát: 02/11/2026; cập nhật Ansible: 04/11/2026. Đây là kế hoạch viết và kiểm chứng code, chưa phải biên bản triển khai AWS.

**Phương án chốt: giữ Terraform hiện có, bổ sung Ansible cho bootstrap và kiểm tra, tiếp tục dùng Argo CD để đồng bộ ứng dụng.** Một EKS và các backend dùng chung có một Terraform root/state; dev/prod tách bằng node group, namespace, database/user và values GitOps.

Tiến độ sau [đợt đối chiếu lỗi hạ tầng](INFRASTRUCTURE_REVIEW.md): đã sửa trạng thái API EKS, role OpenSearch, dependency NAT, thêm S3 Gateway Endpoint và sửa quyền RabbitMQ/bulk OpenSearch. Đã kiểm chứng 6 nhóm permission smoke trên backend thật trong fixture; chưa triển khai AWS.

**Cập nhật hoàn thiện Terraform trước Ansible:** đã thêm kiểm tra input/account/IAM/state bucket/AZ; runner hỗ trợ BOM, kubeconfig riêng, Complete/Failed/timeout, Lease chống chạy song song và báo cáo JSON; có policy renderer chia phần theo giới hạn IAM, chế độ workflow validate-only và artifact outputs đã allowlist. Kiểm tra mới đạt 76 test Python, 20 test Terraform giả lập và actionlint. Xem [hướng dẫn account AWS mới](AWS_FIRST_DEPLOYMENT.md). Bảng vấn đề dưới đây ghi căn cứ ban đầu, không phải mọi dòng đều còn chưa sửa.

**Ansible đã được triển khai:** chín playbook, inventory localhost, dependency lock, preflight account/cluster/receipt, kubeconfig riêng, backend Job runner, GitOps candidate cô lập, bootstrap ESO/Argo CD, verify readiness và ba playbook vận hành rotation/restore/monitoring-backup acceptance. Xem [runbook](../ansible/README.md). Workflow `Retail Ansible` chỉ kiểm tra offline, không tự provision AWS.

Kiểm chứng cuối ngày 04/11/2026: 163 test hạ tầng, 60 helper test Ansible trên Linux và 23 native Terraform test đạt; syntax-check cả chín playbook, preflight thực chạy offline changed=0, ansible-lint không lỗi/cảnh báo và hai workflow qua actionlint. Lockfile Python 3.12/Linux x86_64 và exact Alpine Job image đã được kiểm thử.

Chưa có AWS CLI/profile hoặc IAM/OIDC thật được nối trong phiên làm việc. IAM template đã đối chiếu tên action/resource-scope với metadata AWS công khai nhưng chưa chạy Access Analyzer trong account. Cần bootstrap account, live preflight, review plan và cold apply để nghiệm thu AWS. Redis node-type orderability mới chỉ kiểm tra được engine metadata trước khi có cluster; không suy thành bằng chứng capacity.

## 1. Căn cứ và hiện trạng

| Nguồn | Kết quả rà soát |
| --- | --- |
| Bản thiết kế `Thiết kế kiến trúc chi tiết.docx` và quyết định người dùng | AWS Singapore, một RDS MySQL với database/user riêng theo service/môi trường; giữ OpenSearch riêng |
| [Terraform](../infra/retail-lab/) tại source commit `d0e13455270aed05c1029422366de96873524edf` | Đã có VPC, EKS/node group/add-on, IAM, backend, S3, Secret containers và outputs |
| [Workflow Terraform](../.github/workflows/terraform.yml) | Đã có validate, plan và apply saved plan riêng; kiểm tra commit/run/attempt/checksum/thời hạn |
| [Backend helpers](../scripts/infra/) | Đã có khởi tạo credential nền, preflight, Job bootstrap backend trong VPC và test |
| [GitOps đã publish](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop-gitops/tree/b241c004b3a45409cb8a38e80c4bfc4bd8c0c0af) | Đã có chart platform/app, metadata prepare, bootstrap ESO/Argo CD, digest dev; AWS deployment và prod release đang tắt |
| Ansible | Đã có chín playbook và role/helper tại `ansible/`; kiểm chứng offline trên Linux, chưa chạy AWS thật |
| Kiểm tra code hiện tại | 163 test hạ tầng, 60 helper test Ansible, 23 native Terraform test và lint đạt; chưa chạy preflight bằng tài khoản AWS, Terraform plan/apply hay bootstrap thật |

13 test hiện tại không phủ đầy đủ response API AWS: ví dụ test EKS đang giả lập `status` khác schema hiện hành. Cần bổ sung ca lỗi thực tế trước khi coi bootstrap sẵn sàng chạy.

## 2. Quyền quản lý của từng công cụ

| Thành phần | Nơi quản lý chính | Vai trò Ansible |
| --- | --- | --- |
| VPC, subnet, NAT, route, SG | Terraform | Kiểm tra điều kiện và đầu ra |
| EKS, node group, IAM/IRSA, managed add-on | Terraform | Kiểm tra account/cluster/endpoint, node và add-on readiness |
| RDS, Redis, RabbitMQ, OpenSearch, S3 | Terraform, gồm CloudFormation stack hiện có cho một số backend | Khởi tạo dữ liệu cấu hình bên trong backend sau khi tạo xong |
| Bốn credential nền MQ/search/Redis | Helper `init-platform`, điều phối bằng Ansible trước Terraform | Tạo hoặc tái sử dụng; chỉ xuất ARN |
| Application/monitoring Secret containers | Terraform | Backend Job điền credential; ESO đồng bộ sang Kubernetes |
| Sáu MySQL database/user, MQ vhost/user, search role | Python backend helper, điều phối bằng Ansible | Chạy Job trong VPC và kiểm tra kết quả |
| ESO | Ansible bootstrap lần đầu, sau đó Argo CD | Nhường quyền đồng bộ sau khi root Application hoạt động |
| Argo CD chart | Ansible/operator, phiên bản lấy từ GitOps | Cài và nâng cấp có chủ đích; hiện chưa có Application tự quản lý chart Argo CD |
| Chín platform chart còn lại và workload | Argo CD theo repo GitOps | Kiểm tra trạng thái; không ghi đè values/Deployment của Argo CD |
| Phiên bản image và promote dev/prod | CI release PR + GitOps/Argo CD | Chỉ nghiệm thu đúng digest; không build/push image |

Ansible nằm trong repo source. Values/chart version và manifest Argo CD lấy từ checkout repo GitOps. Không tạo bộ YAML môi trường thứ hai trong Ansible. Password không đi qua tfvars, outputs, inventory hoặc artifact.

## 3. Các vấn đề phải xử lý trước khi nối pipeline

| Mức | Bằng chứng | Việc cần làm | Điều kiện đạt |
| --- | --- | --- | --- |
| P0 | [preflight.py](../scripts/infra/preflight.py) đọc `status == STANDARD_SUPPORT` | Ưu tiên `versionStatus`; chuẩn hóa trường `status` cũ | Response mới/cũ đúng schema được xử lý đúng; từ chối extended/unsupported và trạng thái không biết |
| P0 | [variables.tf](../infra/retail-lab/variables.tf) chỉ kiểm tra IPv4 `/32`; ví dụ dùng IP tài liệu | Kiểm tra chặt IP quản trị public thật trong preflight; đồng bộ validation với GitOps | Chặn private, reserved, IP tài liệu, placeholder và `/0` trước plan |
| P0 trước chạy thật | S3 backend được dùng trước khi root có thể bật versioning cho bucket; preflight mới `head-bucket` | Làm rõ bootstrap/adopt state bucket, OIDC và Terraform role; kiểm tra bucket owner/region/versioning/BPA/encryption/lock permissions | Checkout sạch chạy `init` được bằng đúng role; không phải chờ apply mới bảo vệ state |
| P1 | Preflight kiểm tra engine nhưng chưa đủ instance/storage/AZ | Kiểm tra EC2 type theo AZ; OpenSearch type, gp3/volume; Redis cấu hình node được dịch vụ hỗ trợ; MQ options và quota liên quan | Báo rõ mục không khả dụng; không tự đổi sang cấu hình đắt hơn |
| P1 | Private node group chưa phụ thuộc trực tiếp vào tuyến egress app hoàn chỉnh | Bổ sung dependency route association/NAT cần thiết sau khi xem graph | Graph đúng thứ tự; cold apply sau này xác nhận node join được. Đây là rủi ro thứ tự, chưa phải lỗi đã quan sát trên AWS |
| P1 | [run_bootstrap.py](../scripts/infra/run_bootstrap.py) đọc JSON mặc định | Dùng UTF-8 có hỗ trợ BOM cho đầu vào Terraform từ PowerShell | Cùng dữ liệu có/không BOM đều dùng được; JSON lỗi bị từ chối trước ghi cluster |
| P1 | Runner chỉ chờ Job `Complete` 900 giây | Theo dõi cả `Complete`/`Failed`, giới hạn các subprocess, lấy thông tin lỗi không chứa secret | Job Failed dừng sớm; phân biệt lỗi API, Pending/image pull, bootstrap và timeout |
| P1 | Prepare GitOps hiện đặt enabled theo việc có `--activate` | Chuẩn bị candidate riêng; phát hiện checkout đang kích hoạt; tách rõ nhập metadata và kích hoạt | Nhập lại output không âm thầm commit cấu hình tắt một đích đang chạy; bootstrap chỉ đọc cấu hình đã review/merge |
| P2 | Job name chính xác đến giây; backend chạy lại có thao tác ghi thật | Thêm suffix duy nhất, khóa chạy song song theo account/cluster, summary JSON không chứa secret | Hai lượt không trùng tên/đạp credential; báo `changed` trung thực |

AWS phân biệt `status` cũ dạng `standard-support` và `versionStatus` dạng `STANDARD_SUPPORT`; lỗi P0 đầu tiên đã đối chiếu tài liệu, không dựa vào phỏng đoán. [EKS API schema](https://docs.aws.amazon.com/eks/latest/APIReference/API_ClusterVersionInformation.html)

## 4. Phần Terraform cần viết/sửa

Giữ root `infra/retail-lab` và địa chỉ resource hiện có. Chưa cần chuyển tất cả sang module: đổi cấu trúc lớn lúc này không giải quyết các lỗi triển khai đã tìm thấy.

1. **Bootstrap tài khoản có runbook cụ thể:** xác minh bucket `manhnguyen1131` thực sự thuộc account, cùng region và đã được bảo vệ; xác minh OIDC/trust và policy của role Terraform. Soạn policy mẫu có thể review tại `infra/retail-lab/policies/terraform-infra.json`: quyền state/lock, API preflight, lifecycle resource, IAM/PassRole và CloudFormation đọc dynamic secret; giới hạn account/region/resource ở các action hỗ trợ. Không dùng `AdministratorAccess` làm mặc định. Nếu tài nguyên chưa tồn tại, operator tạo theo runbook rồi adopt/import đúng địa chỉ Terraform hiện có. Không để Ansible và Terraform cùng liên tục sửa IAM/bucket. Không tạo root dev/prod riêng làm trùng tài nguyên dùng chung.
2. **Đầu vào có validation:** account/role/ARN cùng đích; public admin `/32`; domain đi cùng hosted zone; các instance/storage/HA cần điều chỉnh có kiểu và ràng buộc. Giữ giá trị mặc định theo thiết kế. Khi thêm biến phải sửa đồng bộ `variables.tf`, tfvars ví dụ, `write_tfvars.py` allowlist, preflight và test.
3. **Preflight phản ánh đúng cấu hình:** engine version/add-on được pin; truy vấn khả dụng tại Singapore bằng API thật trước plan. Kiểm tra instance/AZ/quota theo API từng dịch vụ, ghi rõ những gì AWS không cung cấp bằng kiểm tra read-only. Instance được chào bán không bảo đảm capacity lúc apply.
4. **Network/IAM:** kiểm tra thứ tự egress cho private node, IAM cho CNI/Pod SG và mapping ServiceAccount/IRSA. Giữ API public giới hạn IP quản trị, backend private và MQ chỉ mở AMQPS cho Orders pod SG.
5. **State/plan:** giữ S3 native lockfile, provider lock, saved plan checksum/run/attempt/commit và thời hạn 24 giờ. State có quyền riêng; không đưa state hoặc toàn bộ file plan vào Ansible. Output bàn giao chỉ có metadata cần thiết, được kiểm tra kiểu và đích.
6. **Managed backend và credential:** giữ dynamic references trong CloudFormation để không kéo password vào Terraform. Rotation phải đồng bộ backend và secret; đổi giá trị Secret một mình không tự cập nhật resource CloudFormation. Không mở rotation tự động như một phần của bootstrap chạy lại.
7. **Dọn và khôi phục lab:** ghi rõ RDS deletion protection, final snapshot ID không trùng, bucket/state/PVC cần giữ và cách nhập lại state. Destroy là thao tác vận hành riêng; không tự chạy sau test.

S3 native locking cần quyền đọc/ghi/xóa đối tượng `.tflock`; bucket versioning phải có trước khi dùng state. [Terraform S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3)

CloudFormation chỉ đọc lại dynamic secret khi tạo/cập nhật resource liên quan. [AWS Secrets Manager dynamic references](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references-secretsmanager.html)

Thông số nền giữ theo thiết kế/code: management `m6i.xlarge`, dev/prod `m6i.large`; MySQL `db.t4g.small`, Redis `cache.t4g.small`, OpenSearch `r8g.medium.search`, RabbitMQ cluster ba AZ `mq.m7g.large`. Engine/EKS version hiện pin vẫn phải qua regional preflight. Một NAT và các backend single-instance ngoài MQ là cấu hình lab; không đồng nghĩa HA toàn hệ thống. Lập bảng số lượng/giờ chạy bằng AWS Pricing Calculator trước apply, không tự bổ sung NAT/replica.

## 5. Cấu trúc Ansible đề xuất

```text
ansible/
  ansible.cfg
  requirements.yml
  requirements-control.lock.txt
  inventory/
    localhost.yml
    group_vars/all.yml
  playbooks/
    preflight.yml
    init-platform.yml
    bootstrap-backends.yml
    prepare-gitops.yml
    bootstrap-argocd.yml
    verify.yml
  roles/
    preflight/tasks/main.yml
    platform_credentials/tasks/main.yml
    backend_bootstrap/tasks/main.yml
    gitops_prepare/tasks/main.yml
    argocd_bootstrap/tasks/main.yml
    verification/tasks/main.yml
  tests/fixtures/
  README.md
.github/workflows/ansible.yml
```

- Control node: Ubuntu/WSL hoặc Linux runner, Python 3.12; `hosts: localhost`, `connection: local`. Không cần EC2 dynamic inventory hoặc SSH vào worker EKS. Máy Windows hiện tại dùng WSL.
- Pin `ansible-core`, `kubernetes.core`, Python dependencies và công cụ theo matrix kiểm tra được trên Linux. Helm lấy pin từ cấu hình công cụ GitOps hiện tại; chart version lấy từ `versions.yaml` của GitOps. Pin `helm-diff` nếu dùng để kiểm tra thay đổi Helm. Chưa chọn phiên bản dependency mới trước khi thử tương thích.
- Dùng `kubernetes.core.helm`, `kubernetes.core.k8s`, `kubernetes.core.k8s_info`; Python helper gọi bằng `ansible.builtin.command` với `argv`. Chỉ thêm `amazon.aws` nếu có task thực sự dùng module của collection.
- Biến người vận hành: `aws_profile`, đường dẫn source/GitOps checkout, file Terraform outputs, kubeconfig/context rõ ràng. Đặt biến chung trong `inventory/group_vars/all.yml` để Ansible tự nạp khi chạy với `-i ansible/inventory/localhost.yml`; kiểm tra bằng `ansible-inventory --list`. Region/cluster/account dự kiến được đối chiếu với outputs và cấu hình GitOps, không chỉ tin tên context.
- Kubeconfig/output/report đặt trong thư mục local được ignore; bổ sung ignore cụ thể cho phần mới. Tasks xử lý credential dùng `no_log: true`, `diff: false`; kiểm tra ESO bằng readiness, không xuất nội dung Kubernetes Secret.
- Mọi bước thay đổi có timeout và lỗi dừng pipeline. Không dùng `ignore_errors` hoặc `changed_when: false` cho thao tác ghi thật.

Ansible hỗ trợ control node trên WSL; Windows thuần không được hỗ trợ native. [Installation requirements](https://docs.ansible.com/projects/ansible/latest/installation_guide/intro_installation.html)

Helm module có cảnh báo về nhận diện thay đổi khi dùng `chart_repo_url`; cần kiểm chứng rerun với công cụ diff được pin. [Helm module](https://docs.ansible.com/projects/ansible/latest/collections/kubernetes/core/helm_module.html)

## 6. Hợp đồng và thứ tự chạy từng playbook

| Bước | Playbook/công cụ | Dữ liệu vào và thao tác | Điều kiện chuyển bước |
| --- | --- | --- | --- |
| 0 | Runbook account + `preflight.yml` trước Terraform | Profile/account, state bucket, role/OIDC, công cụ và cấu hình dự kiến | Sai account/quyền/IP/version thì dừng; chỉ kiểm tra tài nguyên chưa tạo theo khả năng giai đoạn này |
| 1 | `init-platform.yml` | Gọi `bootstrap_backends.py init-platform` bằng quyền operator | Bốn credential nền có AWSCURRENT; file kết quả chỉ có ARN; chạy lại giữ password |
| 2 | Workflow Terraform | Regional preflight → plan → review → apply saved plan | Đúng commit/hash/attempt/role; apply thành công |
| 3 | Xuất outputs + `preflight.yml` sau Terraform | Đọc output bằng UTF-8/BOM-safe; kiểm tra account/cluster ARN/endpoint, node, add-on, context | File hợp lệ, EKS đúng đích và trạng thái đủ chạy bootstrap |
| 4 | `bootstrap-backends.yml` | Gọi runner đã sửa; Job management dùng IRSA, truy cập backend private | Job thành công; DB/user/vhost/search và credential đúng môi trường |
| 5 | `prepare-gitops.yml` | Dùng helper trong GitOps checkout tạo candidate metadata/platform values | Không đổi bộ digest; diff và render hợp lệ; cấu hình kích hoạt được review/merge |
| 6 | `bootstrap-argocd.yml` | Đọc cấu hình đã merge; kiểm tra đích → ESO CRD/controller → SecretStore/ExternalSecret → Secret Ready → Argo CD → root | Không đi tiếp nếu placeholder, sai đích, Secret chưa Ready hoặc root khác repo/ref/path |
| 7 | `verify.yml` + acceptance helper | Kiểm tra Argo/ESO/platform/dev, digest và smoke; tạo báo cáo | Dev hoạt động đúng release; có acceptance thực tế trước khi đề xuất prod |
| 8 | Workflow GitOps hiện có | PR promote cùng năm digest đã nghiệm thu | Prod sync và UI promote thủ công theo quy trình hiện có |

Không gom bước 0–8 thành một playbook tự apply, merge Git rồi promote. Các mốc plan review, GitOps merge và nghiệm thu vẫn là điểm chuyển giai đoạn rõ ràng.

Ansible bootstrap Argo CD chuyển logic từ script PowerShell sang module chạy Linux; không bắt WSL cài PowerShell để thực hiện bootstrap. Các kiểm tra account/endpoint/root manifest trong script hiện tại phải được giữ nguyên khi chuyển.

## 7. Chạy lại, lỗi và bằng chứng

**Chạy lại an toàn nghĩa là:** giữ credential đã có, dữ liệu/schema không bị xóa, quyền không vượt môi trường, không phát sinh resource AWS/Helm release trùng. Job bootstrap mới vẫn là thao tác thật và được báo là đã chạy; không yêu cầu toàn bộ playbook luôn `changed=0` để đạt kiểm tra.

Backend runner cần trả summary có Job identity, đích account/cluster, các bước thành công/thất bại và thời gian; không chứa password, token, SecretString hay private key. Báo cáo kiểm tra read-only có thể `changed=false`. Tách summary khỏi log để Ansible phân loại lỗi thay vì dò chuỗi stdout.

Khi Job Failed hoặc timeout: lấy log/event liên quan đã lọc, trả exit khác 0, không tự rotate password hoặc tạo lại backend. Job có TTL, dữ liệu cấu hình tạm được thu dọn có kiểm soát; không xóa bằng chứng trước khi ghi báo cáo.

Với ESO đã được Argo CD quản lý: kiểm tra trạng thái rồi chuyển sang verify; không tự chạy Helm upgrade với values khác. Nâng cấp ESO về sau đi bằng GitOps; nâng cấp Argo CD đi qua playbook/operator đọc phiên bản đã review.

## 8. CI và tiêu chí nghiệm thu

| Lớp kiểm tra | Việc phải chạy | Bằng chứng đạt |
| --- | --- | --- |
| PR Terraform, không AWS | `fmt`, init backend=false/lock readonly, validate, tests preflight/plan/bootstrap | Có số test chạy; thêm fixture API EKS thật, IP sai, BOM, Job Failed/timeout và sai context |
| PR Ansible, không AWS | `ansible-lint`, `ansible-playbook --syntax-check`, inventory/vars mẫu, render và contract tests | Không cần credential; không gọi backend thật; lỗi cấu hình/thứ tự bootstrap bị bắt |
| PR GitOps | Workflow validation hiện có | Đúng source SHA/chart, năm digest, sync order, inactive/prod guard và values thật |
| Manual sau Terraform | Read-only preflight → từng playbook bootstrap → verify | Đúng account/endpoint, ESO và platform Ready, dev workload đúng digest |
| Rerun có AWS | Chạy lại trên cùng đích | Password và dữ liệu còn nguyên, không tạo AWS resource trùng, report thay đổi trung thực |
| Nghiệm thu ứng dụng | Checkout end-to-end, reindex/search, persistence, dev/prod isolation, UI Blue/Green/HPA | Acceptance helper và bằng chứng test thực tế |
| Vận hành | Rotation có kiểm soát, MQ failover và restore đo thời gian | RPO/RTO chỉ được xác nhận sau phép thử, không suy từ Helm render |

`--check` không phải kiểm thử offline đầy đủ: module command có thể bị skip, còn Kubernetes module có thể cần API thật. Không ép task ghi chạy bằng `check_mode: false` trong một lượt dry run. [Command check-mode semantics](https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/command_module.html)

Workflow Ansible mới chạy PR validation trên `ubuntu-24.04`; bootstrap giữ dạng manual trên WSL/operator ở giai đoạn đầu. Chỉ thêm workflow bootstrap remote khi có runner/network và IAM Access Entry phù hợp. GitHub hosted runner không tự nằm trong IP `/32` đã cho phép của EKS; không mở API `/0` để chữa lỗi kết nối. Nếu dùng OIDC cho bootstrap, cần role/trust/access riêng, không tái sử dụng quyền CI build image.

Timeout khởi điểm: Job backend 15 phút; ESO Helm 15 phút; ExternalSecret Ready 5 phút; Argo CD Helm 15 phút; các readiness/smoke riêng 5 phút. PR validation tối đa 20 phút; workflow manual có concurrency theo account/cluster, không hủy lượt đang ghi. Giữ báo cáo/log đã lọc 14 ngày; không upload state, credential hay kubeconfig.

## 9. Chia đợt code và điều kiện xong

| Đợt | Phạm vi file | Điều kiện xong trước đợt tiếp theo |
| --- | --- | --- |
| 1. Terraform/preflight | `scripts/infra/preflight.py`, tests, `variables.tf`, `write_tfvars.py`, ví dụ tfvars, IAM/state runbook; `eks.tf` nếu graph cần sửa | P0 được sửa; test dùng schema AWS thật; input/instance/region checks rõ; validate đạt |
| 2. Runner bootstrap | `run_bootstrap.py`, `bootstrap_backends.py` ở phần report cần thiết, tests | BOM-safe, tên Job không trùng, Complete/Failed/timeout và report được kiểm tra; giữ credential |
| 3. Nền Ansible | `ansible.cfg`, inventory/vars, dependency pins, preflight và init-platform roles/playbooks | Syntax/lint đạt; Linux/WSL chạy được; không cần SSH worker |
| 4. Ansible sau apply | Repo source: backend, prepare GitOps, ESO/Argo CD và verification roles/playbooks. Repo GitOps: PR riêng cho `scripts/prepare.py`, test và hướng dẫn nếu cần thay đổi activation | Kiểm tra đích và activation; thứ tự đúng; dùng helper/values từ GitOps đã publish; rerun semantics rõ |
| 5. CI và runbook | `.github/workflows/ansible.yml`, cập nhật Terraform path filters nếu cần, ignore và tài liệu | Checkout sạch tái lập validation; hướng dẫn cold start/rerun/rotation/restore/teardown đầy đủ |
| 6. Nghiệm thu AWS | Chạy plan/apply và playbook theo các mốc ở trên khi bắt đầu triển khai thật | Dev hoạt động đúng bộ image; bằng chứng runtime đầy đủ trước promote prod |

Phần sửa helper activation phải thực hiện tại repo `DevOps-retails-shop-gitops`, không chỉ sửa seed `gitops/` cũ trong source. Phối hợp hai PR bằng revision tương thích; Ansible không được chạy với helper cũ chưa có hành vi đã kiểm tra. Nếu chỉ dùng candidate checkout riêng đã đủ giữ an toàn và không cần đổi helper, ghi rõ lựa chọn này trong PR và bổ sung test tương ứng.

Hai file sửa local trước đợt lập kế hoạch là `scripts/gitops/accept_dev.py` và `scripts/gitops/test_accept_dev_ports.py`; cần giữ nguyên khi triển khai kế hoạch. Bản kế hoạch này không commit/push source hoặc thay đổi cấu hình GitOps đang publish.

Các thông tin vận hành cần có lúc chạy thật: AWS profile/account và role quản trị, public IP quản trị, quyền trên state bucket, ARN/policy Terraform phù hợp. Domain/ACM là tùy chọn; GitOps token và receiver alert cấu hình riêng. VPC/SG/role endpoint sau tạo lấy từ Terraform, không yêu cầu tự đặt ID giả để hoàn thiện code.
