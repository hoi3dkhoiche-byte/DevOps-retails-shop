# Đối chiếu đánh giá hạ tầng Retail AWS lab

Ngày kiểm tra: 03/11/2026. Kết quả bên dưới đối chiếu code, kiểm tra Docker Hub read-only và chạy backend fixture cô lập trên máy; không phải biên bản triển khai AWS.

## Bổ sung mục 8–10 sau commit `7da3973`

| Mục đánh giá | Kết luận hiện tại | Xử lý |
| --- | --- | --- |
| 8. EKS support field | Lỗi cũ là đúng nhưng đã được sửa và push trong `7da39736b2c1fcf49fc61cbed48de2bfb416ad76` | Giữ `versionStatus` và fallback chuẩn hóa `status`; các regression vẫn chạy |
| 8. OpenSearch instance/storage | Đúng: trước đó mới kiểm tra engine | Thêm `DescribeInstanceTypeLimits` cho đúng `OpenSearch_3.3`/`r8g.medium.search`, một data node, gp3 50 GiB; kiểm tra số node/dung lượng và giới hạn không dùng dedicated masters |
| 9. Private repository credentials | Đúng nếu repo private. Kiểm tra API GitHub hiện cho thấy cả source và GitOps đều `private=false`, nên chưa có lỗi xác thực hiện tại | Thêm tùy chọn ESO `repo-creds` áp dụng cho cả hai repo, mặc định tắt; chờ credential Ready khi bootstrap; Actions checkout private source dùng Secret riêng |
| 10. Load Balancer Controller policy | Không thiếu quyền theo release đang pin | Policy hiện tại khớp hoàn toàn upstream controller `v3.6.2`/chart `3.6.2`; giữ nguyên quyền, thêm provenance/checksum và test chống lệch |

Kiểm chứng đợt bổ sung: 35 test Python infra, 6 native test Terraform với provider giả lập (gồm kiểm tra IAM credential chỉ dành cho ESO) và 45 test GitOps đều đạt; đã render thêm toàn bộ 10 chart platform. Chưa gọi API AWS bằng account triển khai hoặc kiểm chứng credential của một repo private thật.

### Mục 8: kiểm tra trước plan

`describe-instance-type-limits` truy vấn cặp engine/type thực tế ở Singapore và trả giới hạn storage/node. Preflight từ chối dữ liệu thiếu/sai hoặc cấu hình nằm ngoài giới hạn; API lỗi hoặc thiếu `es:DescribeInstanceTypeLimits` cũng phải dừng. Test dùng response theo schema AWS; chưa có kết quả API từ account triển khai để khẳng định cỡ máy đang được cung cấp. [API AWS](https://docs.aws.amazon.com/opensearch-service/latest/APIReference/API_DescribeInstanceTypeLimits.html)

### Mục 9: hai repo và ba nơi sử dụng credential

Argo CD lấy chart từ source repo và values từ GitOps repo; credential chỉ cho một repo không đủ khi cả hai private. Secret kiểu `repo-creds` dùng URL prefix `https://github.com/hoi3dkhoiche-byte/` cho cả hai URL đã khai báo. ESO đọc JSON `username`/`password` từ `/retail/platform/argocd/repositories`; token chỉ cần quyền đọc Contents cho hai repo. Token không nằm trong Git và backend Job không được đọc/ghi Secret này. Khi tùy chọn bật, bootstrap đợi `argocd-repositories` Ready trước Argo CD/root. [Argo CD credential templates](https://argo-cd.readthedocs.io/en/stable/user-guide/private-repositories/#credential-templates)

Tách rõ: operator cần quyền Git để clone ban đầu; Argo CD dùng credential AWS/ESO để reconcile; workflow validation ở repo GitOps dùng Actions Secret `SOURCE_READ_TOKEN` nếu source private. `GITOPS_TOKEN` của CI tạo PR image là credential có nhiệm vụ khác. Hướng dẫn chi tiết ở [infra runbook](../scripts/infra/README.md) và repo GitOps đang duy trì.

### Mục 10: đối chiếu đúng phiên bản

Đã đối chiếu toàn bộ JSON, gồm action/resource/condition, với policy chính thức tại commit bất biến `d19307ccb61acb577ebeb9ffbf20d19047e7a275`: khớp đủ 86 action entries. Policy này cũng không có `DescribeIpPools`; tài liệu IAM ELBv2 có `ModifyIpPools` nhưng không có action được suy đoán kia. Không thêm quyền chỉ vì tên nghe tương ứng. Nguồn và canonical SHA256 nằm trong [provenance](../infra/retail-lab/policies/load-balancer-controller.provenance.json), được kiểm tra bằng test offline. [Policy upstream](https://github.com/kubernetes-sigs/aws-load-balancer-controller/blob/d19307ccb61acb577ebeb9ffbf20d19047e7a275/docs/install/iam_policy.json), [IAM ELBv2 actions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_elbv2.html)

## Kết luận và thay đổi

| Mục đánh giá | Kết luận | Thay đổi trong đợt sửa |
| --- | --- | --- |
| 1. RabbitMQ Orders | Đúng: quyền cũ không cho queue binding đọc exchange/ghi queue | Sửa configure/read/write để khớp chính xác bốn resource Orders; thêm regression và phép thử broker thật |
| 2. OpenSearch bulk | Đúng là role khai báo thiếu quyền bulk cấp cluster; quyền rộng được kế thừa có thể che lỗi | Bổ sung `indices:data/write/bulk*` ở cluster, giữ index pattern riêng; không cấp rộng toàn bộ `cluster_composite_ops` |
| 3. Node group/NAT | Đúng là thiếu dependency tường minh, có rủi ro cold apply; chưa quan sát lỗi join trên tài khoản AWS | Cho node group đợi NAT, route associations app/public và S3 endpoint |
| 4. OpenSearch service-linked role | Đúng với account chưa có role/quyền tạo; không phải mọi account đều lỗi | Mặc định reuse role OpenSearch hoặc legacy Elasticsearch; tùy chọn tạo mới có bảo vệ xóa; preflight kiểm tra trước plan |
| 5. Docker Hub | Rate limit là rủi ro thật; private image chưa phải lỗi của năm repo hiện tại | Đã kiểm tra public/anonymous manifest; giữ cấu hình public và hướng dẫn authenticated pull khi cần |
| 6. S3 endpoint | Đúng: chưa có Gateway Endpoint nên S3 traffic từ app subnet đi NAT | Thêm S3 Gateway Endpoint vào app route table; quyền workload/bucket vẫn được IAM kiểm soát |
| 7. Tài liệu | Đúng có lệch; ảnh thực tế có ba biểu tượng EKS; OpenSearch được người dùng chốt bổ sung sau DOCX | README trỏ đến GitOps đã publish và bảng kiến trúc hiện hành bên dưới; ghi rõ phạm vi Access Entry |

Ngoài danh sách, đã sửa cách preflight đọc support status của EKS: dùng `versionStatus`, hỗ trợ chuẩn hóa `status` cũ. Test mới dùng đúng schema AWS và chặn extended/unknown status. [EKS API fields](https://docs.aws.amazon.com/eks/latest/APIReference/API_ClusterVersionInformation.html)

### Mục 1–2: quyền backend

[bootstrap_backends.py](../scripts/infra/bootstrap_backends.py) giờ cấp quyền Orders bằng regex khớp đúng `orders.events`, `orders.dlx`, `orders.events.v2`, `orders.events.dlq` trong vhost của mỗi môi trường. `queue.bind` cần write trên queue và read trên exchange; khai báo DLX cũng cần quyền tương ứng. [RabbitMQ access control](https://www.rabbitmq.com/docs/access-control)

Role Catalog giữ write/read/manage chỉ trên `products-dev` hoặc `products-prod`; thêm cluster action phục vụ bulk dispatch. Quyền index vẫn bắt buộc khi bulk request chạy, nên cluster action này không cấp quyền ghi sang index môi trường khác. [OpenSearch permissions](https://docs.opensearch.org/latest/security/access-control/permissions/)

Phép thử OpenSearch 3.3.0 phát hiện cấu hình demo mặc định còn gán role `own_index` cho `users: ['*']`, qua đó cấp thêm cluster composite operations. Vì vậy role cũ có thể bulk được nếu dựa vào quyền kế thừa này. Smoke test chỉ bỏ wildcard mapping demo **trong fixture tạm**, xác minh authinfo có đúng một role `retail-catalog-<env>`, rồi kiểm tra role bootstrap độc lập: role cũ trả HTTP 403, role mới bulk/search thành công và không ghi được index môi trường kia. Code bootstrap không xóa hoặc sửa global role mapping của backend AWS.

Các quyền của user/role đã tồn tại không tự đổi khi sửa source: cần chạy lại backend bootstrap sau khi cập nhật code. Không cần tạo password hoặc build lại năm application image chỉ để áp dụng thay đổi quyền này. App thực tế vẫn phải được smoke test sau cập nhật.

### Mục 3–4 và 6: Terraform

[eks.tf](../infra/retail-lab/eks.tf) có dependency cho đường egress hoàn chỉnh trước khi tạo private workers. [network.tf](../infra/retail-lab/network.tf) có S3 Gateway Endpoint gắn app route table để Loki/Velero dùng cùng region không đi NAT. Đây không thay thế NAT cho Docker Hub hoặc traffic ngoài S3. AWS không tính phí thêm cho gateway endpoint; phí S3/request/storage vẫn áp dụng. [S3 gateway endpoint](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html)

[backends.tf](../infra/retail-lab/backends.tf) khai báo thứ tự service-linked role trước backend stack. `create_opensearch_service_linked_role=false` reuse role hợp lệ hiện có; `true` tạo role mới cho account chưa có. Role mới có `prevent_destroy` vì dùng chung ở cấp account. Nếu role đã tồn tại và muốn Terraform quản lý, phải import trước. Role Elasticsearch legacy được AWS hỗ trợ reuse. [AWS service-linked role](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/slr-aos.html)

Preflight chỉ kiểm tra IAM read-only, không tạo role. `AccessDenied` không được coi là role không tồn tại. Chính sách IAM ngoài Terraform root cần quyền đọc/tra role và quyền `CreateServiceLinkedRole` có điều kiện khi chọn tạo; hướng dẫn tại [infra runbook](../scripts/infra/README.md).

### Bằng chứng kiểm tra code

- Terraform 1.17.1 với provider đã lock: `fmt`, init backend-disabled, `validate` và 5 native mock tests đạt; container kiểm tra không có mạng AWS.
- Python infra: 26 test đạt, gồm quyền resource, cách ly môi trường, trạng thái EKS, role IAM, readiness và chặn URL fixture trỏ vào backend thật.
- [Permission smoke](../scripts/infra/permissions_smoke.py) với [fixtures](../scripts/infra/permissions-fixtures.yml): 6 nhóm kiểm tra thực tế đạt trên RabbitMQ 4.2.0 và OpenSearch 3.3.0 bật Security plugin, gồm tái hiện quyền cũ bị từ chối và quyền mới hoạt động/cách ly dev-prod.
- Workflow Terraform đã thêm native Terraform tests và permission smoke vào job `validate`; plan/apply tiếp tục phụ thuộc job này. Actionlint/ShellCheck đạt sau thay đổi.
- Docker Hub metadata/manifest được kiểm tra ẩn danh; không lấy credential của máy. Không gọi Terraform apply hoặc thay đổi backend/quyền người dùng trên AWS.
- Fixture đã được dọn sau thử; log và bằng chứng máy cục bộ nằm trong `.ci-tools/backend-permissions-full.log` và `.ci-tools/backend-permissions-evidence.json`, không đưa credential vào repo.

## Mục 5 — Docker Hub và quyền kéo image

**Kết luận: rủi ro rate limit đúng; nhận định image private gây lỗi hiện tại chưa đúng với năm repository đang dùng.**

Kiểm tra mới bằng Docker Hub API không đăng nhập cho kết quả:

| Repository trong namespace `hmannnguye` | `is_private` | HEAD manifest ẩn danh của digest dev | Digest khớp |
| --- | --- | --- | --- |
| `devops-retails-shop` | `false` | 200 | Có |
| `devops-retails-shop-catalog` | `false` | 200 | Có |
| `devops-retails-shop-cart` | `false` | 200 | Có |
| `devops-retails-shop-checkout` | `false` | 200 | Có |
| `devops-retails-shop-orders` | `false` | 200 | Có |

HEAD dùng token pull ẩn danh do registry cấp, không dùng credential Docker của máy và không tải image layer. Các digest lấy từ release `41033493219/1`, source `d0e13455270aed05c1029422366de96873524edf`, trong [manifest GitOps đã publish](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop-gitops/blob/b241c004b3a45409cb8a38e80c4bfc4bd8c0c0af/releases/41033493219-1/images.json). Kiểm tra này chứng minh quyền đọc manifest tại thời điểm kiểm tra; chưa chứng minh một node EKS đã kéo đủ layer hoặc loại trừ rate limit ở IP NAT AWS.

Cả năm source chart đã có `imagePullSecrets: []` và render trường này vào Pod template:

- [UI values](../retail-store-sample-app/src/ui/chart/values.yaml), [Pod template](../retail-store-sample-app/src/ui/chart/templates/deployment.yaml).
- [Catalog values](../retail-store-sample-app/src/catalog/chart/values.yaml), [Pod template](../retail-store-sample-app/src/catalog/chart/templates/deployment.yaml).
- [Cart values](../retail-store-sample-app/src/cart/chart/values.yaml), [Pod template](../retail-store-sample-app/src/cart/chart/templates/deployment.yaml).
- [Checkout values](../retail-store-sample-app/src/checkout/chart/values.yaml), [Pod template](../retail-store-sample-app/src/checkout/chart/templates/deployment.yaml).
- [Orders values](../retail-store-sample-app/src/orders/chart/values.yaml), [Pod template](../retail-store-sample-app/src/orders/chart/templates/deployment.yaml).

GitOps hiện không ghi đè danh sách rỗng vì năm image ứng dụng public. Không thêm một tên Secret chưa tồn tại: cấu hình như vậy không cung cấp quyền registry và có thể tạo cảnh báo pull secret. Secret đăng nhập Docker Hub ở CI phục vụ publish; nó không tự chuyển sang kubelet của EKS.

Các worker private đi qua một NAT Gateway nên pull ẩn danh có thể cùng tính theo một IP egress. Docker áp dụng giới hạn pull cho người dùng ẩn danh theo IPv4 hoặc IPv6 subnet; rate limit và abuse limit là hai cơ chế khác nhau. Chưa có bằng chứng trong đợt kiểm tra này rằng IP NAT của lab đã bị giới hạn. [Docker Hub pull usage and limits](https://docs.docker.com/docker-hub/usage/pulls/)

Với lab hiện tại, giữ repository public và kiểm tra node events khi nghiệm thu. Nếu cần authenticated/private pull, bổ sung credential pull-only qua Secrets Manager → ESO → Secret kiểu `kubernetes.io/dockerconfigjson` trong từng namespace, rồi đặt cùng `imagePullSecrets` cho cả năm service của môi trường đó. Không đặt token trực tiếp trong values hoặc dùng write token CI cho node. Nếu chuyển registry sang ECR phải cập nhật đồng bộ CI publish, bộ repository/digest trong release và kiểm tra GitOps; không chỉ đổi chuỗi repository tại Deployment.

## Mục 7 — Tài liệu và phạm vi kiến trúc

**Kết luận: tài liệu cũ có lệch thật; một số khác biệt đã được người dùng quyết định lại trong hội thoại.**

| Nhận định | Kết quả đối chiếu |
| --- | --- |
| `architecture.platform.png` là kiến trúc Voting App | Đúng. Ảnh có vote/result/worker, PostgreSQL, ECR và NLB; có management EKS cùng dev EKS và prod EKS. Cụ thể là ba biểu tượng cluster, không phải kiến trúc một EKS hiện hành |
| DOCX không nhắc OpenSearch | Đúng theo nội dung XML của tài liệu đã gửi. Người dùng đã chốt giữ OpenSearch riêng sau đó; đây là quyết định đang được code/README thực hiện |
| Terraform mới có EKS Access Entry admin | Đúng. `eks.tf` hiện tạo một admin access entry/policy association; IRSA của workload không thay thế quyền Kubernetes cho dev/viewer/operator |
| CIDR/node group/backend hiện tại | Đối chiếu bằng Terraform, README và values; xem cấu hình hiện hành ở bảng dưới |

Quyền dev/viewer/bootstrap chỉ được triển khai khi có IAM principal ARN thật và phạm vi cần cấp. Không tự chế ARN hoặc cấp cluster-admin cho CI build image để làm đủ bảng quyền. Tài liệu phải ghi rõ IAM access matrix đầy đủ chưa được nghiệm thu; lab quản trị bằng role admin hiện có không chứng minh đã cách ly quyền cho mọi nhóm người dùng.

### Kiến trúc hiện hành để triển khai

| Thành phần | Cấu hình đang theo |
| --- | --- |
| Region/cluster | Singapore `ap-southeast-1`, một `retail-lab-eks` |
| Worker | Ba node group management/dev/prod; management ở AZ-a; dev/prod chạy trong các app subnet |
| Môi trường | Namespace `retail-dev` và `retail-prod` trên cùng cluster |
| Dữ liệu Catalog/Cart/Orders | Một RDS MySQL, sáu database/user riêng theo service và môi trường |
| Checkout | ElastiCache Redis dùng chung, ACL và key prefix riêng |
| Search | OpenSearch riêng, user/role/index tách môi trường; reindex đọc Catalog MySQL |
| Messaging | Amazon MQ RabbitMQ private, cluster ba AZ, Orders dùng AMQPS |
| Ingress | ALB do AWS Load Balancer Controller tạo; UI có active/preview service theo Blue/Green |
| Image | Năm repository Docker Hub, deploy bằng digest; ECR pull policy trên node không đổi registry đang dùng |
| Secret | Secrets Manager → ESO → Kubernetes Secret; không lưu password vào Git |
| Đồng bộ | Terraform sở hữu AWS/managed add-on; bootstrap ESO/Argo CD; Argo CD quản lý platform và ứng dụng theo GitOps |

Nguồn hiện hành là [README](../README.md), [Terraform entrypoint](../infra/retail-lab/), [GitOps repository](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop-gitops) và [cấu hình AWS GitOps](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop-gitops/blob/main/docs/aws-architecture.md). `architecture.platform.png` và DOCX là tài liệu lịch sử cần được đối chiếu với các quyết định sau đó; không dùng ảnh cũ làm sơ đồ bàn giao hệ thống hiện tại.

Đợt rà soát này không sửa file DOCX/ảnh gốc và không xác nhận quyền người dùng hoặc hạ tầng đã hoạt động trên tài khoản AWS.
