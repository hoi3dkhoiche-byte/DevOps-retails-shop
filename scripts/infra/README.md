# Retail lab infrastructure

This root implements the architecture in `Thiết kế kiến trúc chi tiết.docx`, with the user's clarification that one RDS MySQL instance has separate service/environment databases and OpenSearch remains a separate search backend. It does not replace the upstream examples under `retail-store-sample-app/terraform`.

The target is Singapore, EKS 1.36, three AZs and management/dev/prod node groups. Terraform owns AWS resources and the CNI, CoreDNS, kube-proxy and EBS CSI managed add-ons. Argo CD owns application/platform charts. Amazon MQ uses a private three-AZ RabbitMQ 4.2 cluster; RDS and Redis start with the specified lab capacity. EFS remains disabled until an actual shared-file workload exists.

## Before the first deployment

1. The operator must already own `s3://manhnguyen1131`. Enable bucket Versioning and Block Public Access before initializing the backend. State uses encryption and S3 native lock files; do not configure a state-expiration lifecycle rule.
2. Create or reuse the account's GitHub OIDC provider and create `retail-lab-terraform` with the trust below. Attach a reviewed policy for the AWS resource types in this module, including S3 state/lock access and Secrets Manager dynamic-reference resolution by CloudFormation. Set the role's maximum session duration to 7200 seconds. Application CI does not receive this role or an EKS Access Entry.
3. Install the locked bootstrap tools in a virtual environment: `python -m pip install --require-hashes -r scripts/infra/requirements.txt`. Run `python scripts/infra/bootstrap_backends.py init-platform` using the operator AWS profile. It creates/reuses four platform administrator/Redis credentials and writes **ARNs only** to `.ci-tools/platform-secret-arns.json`; it prints no password.
4. Copy `infra/retail-lab/terraform.tfvars.example` to a local ignored tfvars file. Supply the actual administrator public `/32`, IAM role ARN, secret ARNs and reviewed Terraform policy ARN. Run `python scripts/infra/resolve-addons.py`; review EKS 1.36-compatible versions and pin the chosen full `eksbuild` strings. No `most_recent` upgrades are used.
5. Use the operator profile for the initial Terraform initialization/import. If the Terraform IAM role was created manually, import it and its existing policy attachment so the root takes ownership without recreation. The source repo's `terraform.yml` can then run future plans and applies.

6. Check OpenSearch's account-wide VPC service-linked role before planning. The default `create_opensearch_service_linked_role=false` reuses either `AWSServiceRoleForAmazonOpenSearchService` or the supported legacy `AWSServiceRoleForAmazonElasticsearchService`; preflight checks the role path/trust and Terraform verifies an existing role is available. On a fresh account with neither role, set this variable to `true` so Terraform creates the OpenSearch role before the backend stack. If you opt into managing a role that already exists, import it before planning rather than creating a duplicate. The created shared role has `prevent_destroy` and is not a disposable lab resource.

   ```powershell
   aws iam get-role --role-name AWSServiceRoleForAmazonOpenSearchService
   aws iam get-role --role-name AWSServiceRoleForAmazonElasticsearchService
   # Only when the modern role already exists and create_opensearch_service_linked_role=true:
   terraform -chdir=infra/retail-lab import 'aws_iam_service_linked_role.opensearch[0]' arn:aws:iam::ACCOUNT_ID:role/aws-service-role/opensearchservice.amazonaws.com/AWSServiceRoleForAmazonOpenSearchService
   ```

   Replace `ACCOUNT_ID` with the real account ID. Reusing a legacy role does not require importing it or creating a second role. The reviewed Terraform policy must allow `iam:ListRoles`, preflight/provider `iam:GetRole` for these role ARNs and, in creation mode, `iam:CreateServiceLinkedRole` limited by `iam:AWSServiceName = opensearchservice.amazonaws.com`. Normal lab deployment does not need `iam:DeleteServiceLinkedRole`; removal of a shared role belongs to a separate reviewed cleanup procedure and `prevent_destroy` blocks removal through this configuration. An IAM `AccessDenied` is a failure, never evidence that the role is absent. No role is created by read-only preflight. See [OpenSearch service-linked roles](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/slr-aos.html).

The OIDC role trust subject is exactly `repo:hoi3dkhoiche-byte/DevOps-retails-shop:ref:refs/heads/main`, with audience `sts.amazonaws.com`. Terraform keeps this same trust when it takes ownership. No GitHub environment is used that would change the subject to an environment-based value.

## Workflow inputs and review

Set repository Variables `TERRAFORM_ROLE_ARN` and `TERRAFORM_TFVARS_JSON`. The latter is the JSON equivalent of the documented tfvars and contains configuration/ARNs only. Credential values are rejected by the input-key allowlist and belong in Secrets Manager.

PRs run formatting, provider validation and offline contract tests with no AWS credentials. From `main`, manually run **Retail Terraform** with `operation=plan`. Read the `terraform-plan.txt` artifact and copy the job summary's plan run ID, attempt and SHA256. Start a separate manual **apply** run with those three values. Apply verifies the successful plan workflow, source SHA, attempt, age (24 hours) and binary-plan checksum before AWS login. A changed main commit, rerun, expired plan or mismatched checksum requires a fresh plan. Apply uses the saved plan; it does not silently create another plan.

`preflight.py` reads Singapore availability for EKS/add-ons, MySQL 8.4.11 and its instance class, Redis OSS 7.1, Amazon MQ 4.2 cluster mode and OpenSearch 3.3. It checks platform credential **metadata** and existing state-bucket access, without retrieving passwords. The exact versions and offered capacity must still pass this regional check when deployment occurs.

OpenSearch preflight also calls `describe-instance-type-limits` for the exact domain tuple in `backends.tf`: `OpenSearch_3.3`, `r8g.medium.search`, one data node without dedicated masters, EBS gp3 50 GiB. It rejects unsupported storage/count/volume and missing or malformed limits rather than assuming compatibility from the engine list. The reviewed Terraform/preflight role needs `es:DescribeInstanceTypeLimits`. Unit fixtures verify parsing and rejection; actual regional availability is determined only when this read-only API runs with the deployment account. See [OpenSearch API](https://docs.aws.amazon.com/opensearch-service/latest/APIReference/API_DescribeInstanceTypeLimits.html).

The Load Balancer Controller IAM policy is the exact upstream policy for controller `v3.6.2` / chart `3.6.2`. Its immutable source commit and canonical JSON checksum are recorded in `infra/retail-lab/policies/load-balancer-controller.provenance.json` and checked offline by `test_lbc_policy.py`. The official release policy does not contain `DescribeIpPools`; do not add speculative IAM actions. Any controller upgrade must review and update the complete policy and provenance together with the GitOps chart/image pin.

EKS support detection uses the current `versionStatus` API field and normalizes the deprecated `status` format. Worker creation explicitly waits for app subnet routes/NAT. An S3 Gateway VPC Endpoint on the app route table routes same-region S3 traffic from Loki/Velero without using NAT; it does not remove Docker Hub or other external egress dependencies. The gateway endpoint has no additional endpoint charge; S3 request/storage charges remain. See [AWS S3 gateway endpoints](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html).

The external reviewed Terraform IAM policy must also cover the new endpoint lifecycle: `ec2:CreateVpcEndpoint`, `ec2:DescribeVpcEndpoints`, `ec2:ModifyVpcEndpoint`, `ec2:DeleteVpcEndpoints` and required tagging operations, with resource/region conditions where supported. Adding the Terraform resource does not change an account policy supplied only by ARN.

## Private backend initialization

After Terraform apply, export `terraform -chdir=infra/retail-lab output -json` to a local JSON file. Its outputs contain endpoints and secret/role ARNs, not password values. Configure the operator's kubectl context for `retail-lab-eks`, then run:

```powershell
python scripts/infra/run_bootstrap.py --terraform-output .ci-tools/terraform-output.json --context <explicit-retail-context>
```

The helper checks the API endpoint against Terraform outputs before creating a short-lived Job on management. The Job authenticates with the dedicated `retail-ops:backend-bootstrap` IRSA role, uses the official RDS CA with hostname/certificate verification, and:

- Creates `catalog_dev`, `cart_dev`, `orders_dev`, `catalog_prod`, `cart_prod`, `orders_prod`, with separate SSL-required users and privileges limited to each schema.
- Creates `retail-dev`/`retail-prod` RabbitMQ vhosts and Orders users with permissions limited to the declared exchanges and queues.
- Creates OpenSearch users and roles restricted to `products-dev` or `products-prod`.
- Stores Redis TLS URLs, application credentials, Grafana login and Argo CD credentials directly in the existing Secrets Manager containers.

Orders needs configure/read/write on exactly `orders.events`, `orders.dlx`, `orders.events.v2` and `orders.events.dlq`: AMQP queue bindings read the source exchange and write the destination queue. Catalog's role allows the cluster-level bulk dispatch action plus index-level operations restricted to `products-dev` or `products-prod`. Run backend bootstrap again after deploying these permission changes so existing backend users/roles are updated; changing this Python file alone does not change credentials/permissions already deployed in AWS.

The Job reuses existing credentials when rerun. Argo CD's AWS secret holds its bcrypt admin-password hash, password modification time, server signing key and internal self-signed certificate/private key. The operator's login password is available only in that AWS secret. Argo CD's internal Redis password is separate. ESO synchronizes these into Kubernetes before the Argo CD install, following the GitOps bootstrap instructions. The generated CA/trust material for RDS is public; app credentials and private Argo CD key never enter Git or Terraform state.

Choose an email or Telegram destination separately and populate `/retail/platform/alertmanager/config` if external notifications are enabled. No recipient is invented by bootstrap. DB/messaging/search bootstrap must finish successfully before app releases are enabled. Certificate/credential rotation must update the backend and AWS secret consistently, then let ESO refresh and change the chart's `secretRevision` to restart environment-variable consumers.

## Optional private source and GitOps repositories

Argo CD reads both the application source repository (charts pinned to the release SHA) and the GitOps repository (environment values). If either is private, its credential must cover that repository. In the maintained GitOps checkout, enable `repositoryCredentials.enabled` and keep `urlPrefix: https://github.com/hoi3dkhoiche-byte/`. ESO generates an Argo CD `repo-creds` Secret using that prefix, covering both fixed repository URLs. The prefix includes the owner and trailing slash; it is not a credential for every GitHub account.

Terraform creates the empty Secrets Manager container `/retail/platform/argocd/repositories`; the operator supplies a JSON object with `username` and `password`, where `password` is a GitHub token with Contents read access to both selected repositories. Set it securely in AWS, never in tfvars, chart values or chat. Argo CD's ESO IRSA reader may read this container. The backend initialization Job is explicitly excluded from reading or overwriting it and never generates a GitHub token. GitOps bootstrap waits for this ExternalSecret before installing Argo CD and applying the root when the option is enabled.

This read credential is separate from the source workflow's `GITOPS_TOKEN`, which creates image PRs. When the source repository is private, the GitOps repository's validation workflow also needs its own Actions Secret `SOURCE_READ_TOKEN` for cross-repository chart checkout; the same configured token is not automatically copied from AWS to GitHub Actions. Initial operator clones require operator Git access before cluster bootstrap can start. Both project repositories are public at this review, so the option stays disabled and current deployment behavior is unchanged. Follow the maintained GitOps README for activation; the source seed's original bootstrap instructions are historical.

## Ownership and operational checks

This is a fresh target root. If upstream Terraform already owns deployed workloads or controllers, migrate state explicitly with a reviewed `removed { lifecycle { destroy = false } }`/import plan before Argo CD takes over; do not delete their state/code and apply a destructive plan. The target does not create another ALB outside the LB Controller.

Default one-worker environments and one-AZ monitoring PVCs do not establish application HA. Management stays in AZ-A while its retained gp3 PVCs exist there. RabbitMQ's three-AZ cluster and quorum queues are separately configured for messaging durability. RDS backup retention is seven days and Redis snapshot retention seven days; perform actual restore/failover tests before claiming the design RPO/RTO.

Validation includes Terraform `fmt`/`validate`, six native mocked-provider Terraform tests and Python contract tests. The native tests include a mocked apply proving only the Argo CD ESO reader can access the repository credential container, while the backend initialization Job is excluded. Mocked providers make no AWS calls. The Terraform workflow's validation gate also starts disposable RabbitMQ 4.2.0 and security-enabled OpenSearch 3.3.0 fixtures, proves the old permissions fail, then verifies the fixed users can bind/publish/consume/dead-letter and bulk/search without crossing environments. It installs only the hash-pinned AMQP test dependency from `requirements-permissions.txt`. These checks do not provision AWS, prove engine capacity, validate all CloudFormation service-side constraints or demonstrate recovery time.

The OpenSearch demo fixture also grants `own_index` to every user by default, which can conceal a missing cluster bulk action through inherited permissions. The probe removes only that wildcard mapping in the disposable fixture and checks the effective Catalog role before testing. It does not modify global role mappings on AWS; the production bootstrap role now declares the bulk action explicitly and does not rely on demo defaults.

For a local permission smoke, install `requirements-permissions.txt` in a test virtual environment, set a strong disposable `PERMISSIONS_FIXTURE_ADMIN_PASSWORD`, start `docker compose -f scripts/infra/permissions-fixtures.yml up -d --wait --wait-timeout 300`, then run `python scripts/infra/permissions_smoke.py --prove-legacy`. Stop only these test fixtures with `docker compose -f scripts/infra/permissions-fixtures.yml down --volumes` after collecting results. The fixture ports are bound to loopback, demo TLS acceptance is confined to the loopback-only probe, and no production backend URL is accepted. Run native Terraform tests without AWS credentials with `terraform -chdir=infra/retail-lab test -no-color` after backend-disabled init.

References: [EKS version lifecycle](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html), [RDS MySQL versions](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.VersionMgmt.html), [VPC CNI policy requirements](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html), [Pod security groups](https://docs.aws.amazon.com/eks/latest/best-practices/sgpp.html), [CloudFormation secure dynamic references](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references.html).
