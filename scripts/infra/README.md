# Retail lab infrastructure

This root implements the architecture in `Thiết kế kiến trúc chi tiết.docx`, with the user's clarification that one RDS MySQL instance has separate service/environment databases and OpenSearch remains a separate search backend. It does not replace the upstream examples under `retail-store-sample-app/terraform`.

The target is Singapore, EKS 1.36, three AZs and management/dev/prod node groups. Terraform owns AWS resources and the CNI, CoreDNS, kube-proxy and EBS CSI managed add-ons. Argo CD owns application/platform charts. Amazon MQ uses a private three-AZ RabbitMQ 4.2 cluster; RDS and Redis start with the specified lab capacity. EFS remains disabled until an actual shared-file workload exists.

## Before the first deployment

1. The operator must already own `s3://manhnguyen1131`. Enable bucket Versioning and Block Public Access before initializing the backend. State uses encryption and S3 native lock files; do not configure a state-expiration lifecycle rule.
2. Create or reuse the account's GitHub OIDC provider and create `retail-lab-terraform` with the trust below. Attach a reviewed policy for the AWS resource types in this module, including S3 state/lock access and Secrets Manager dynamic-reference resolution by CloudFormation. Set the role's maximum session duration to 7200 seconds. Application CI does not receive this role or an EKS Access Entry.
3. Install the locked bootstrap tools in a virtual environment: `python -m pip install --require-hashes -r scripts/infra/requirements.txt`. Run `python scripts/infra/bootstrap_backends.py init-platform` using the operator AWS profile. It creates/reuses four platform administrator/Redis credentials and writes **ARNs only** to `.ci-tools/platform-secret-arns.json`; it prints no password.
4. Copy `infra/retail-lab/terraform.tfvars.example` to a local ignored tfvars file. Supply the actual administrator public `/32`, IAM role ARN, secret ARNs and reviewed Terraform policy ARN. Run `python scripts/infra/resolve-addons.py`; review EKS 1.36-compatible versions and pin the chosen full `eksbuild` strings. No `most_recent` upgrades are used.
5. Use the operator profile for the initial Terraform initialization/import. If the Terraform IAM role was created manually, import it and its existing policy attachment so the root takes ownership without recreation. The source repo's `terraform.yml` can then run future plans and applies.

The OIDC role trust subject is exactly `repo:hoi3dkhoiche-byte/DevOps-retails-shop:ref:refs/heads/main`, with audience `sts.amazonaws.com`. Terraform keeps this same trust when it takes ownership. No GitHub environment is used that would change the subject to an environment-based value.

## Workflow inputs and review

Set repository Variables `TERRAFORM_ROLE_ARN` and `TERRAFORM_TFVARS_JSON`. The latter is the JSON equivalent of the documented tfvars and contains configuration/ARNs only. Credential values are rejected by the input-key allowlist and belong in Secrets Manager.

PRs run formatting, provider validation and offline contract tests with no AWS credentials. From `main`, manually run **Retail Terraform** with `operation=plan`. Read the `terraform-plan.txt` artifact and copy the job summary's plan run ID, attempt and SHA256. Start a separate manual **apply** run with those three values. Apply verifies the successful plan workflow, source SHA, attempt, age (24 hours) and binary-plan checksum before AWS login. A changed main commit, rerun, expired plan or mismatched checksum requires a fresh plan. Apply uses the saved plan; it does not silently create another plan.

`preflight.py` reads Singapore availability for EKS/add-ons, MySQL 8.4.11 and its instance class, Redis OSS 7.1, Amazon MQ 4.2 cluster mode and OpenSearch 3.3. It checks platform credential **metadata** and existing state-bucket access, without retrieving passwords. The exact versions and offered capacity must still pass this regional check when deployment occurs.

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

The Job reuses existing credentials when rerun. Argo CD's AWS secret holds its bcrypt admin-password hash, password modification time, server signing key and internal self-signed certificate/private key. The operator's login password is available only in that AWS secret. Argo CD's internal Redis password is separate. ESO synchronizes these into Kubernetes before the Argo CD install, following the GitOps bootstrap instructions. The generated CA/trust material for RDS is public; app credentials and private Argo CD key never enter Git or Terraform state.

Choose an email or Telegram destination separately and populate `/retail/platform/alertmanager/config` if external notifications are enabled. No recipient is invented by bootstrap. DB/messaging/search bootstrap must finish successfully before app releases are enabled. Certificate/credential rotation must update the backend and AWS secret consistently, then let ESO refresh and change the chart's `secretRevision` to restart environment-variable consumers.

## Ownership and operational checks

This is a fresh target root. If upstream Terraform already owns deployed workloads or controllers, migrate state explicitly with a reviewed `removed { lifecycle { destroy = false } }`/import plan before Argo CD takes over; do not delete their state/code and apply a destructive plan. The target does not create another ALB outside the LB Controller.

Default one-worker environments and one-AZ monitoring PVCs do not establish application HA. Management stays in AZ-A while its retained gp3 PVCs exist there. RabbitMQ's three-AZ cluster and quorum queues are separately configured for messaging durability. RDS backup retention is seven days and Redis snapshot retention seven days; perform actual restore/failover tests before claiming the design RPO/RTO.

Validation in this workspace is Terraform `fmt`/`validate` plus offline contract tests. These checks do not provision AWS, prove engine capacity, validate all CloudFormation service-side constraints or demonstrate recovery time.

References: [EKS version lifecycle](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html), [RDS MySQL versions](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/MySQL.Concepts.VersionMgmt.html), [VPC CNI policy requirements](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html), [Pod security groups](https://docs.aws.amazon.com/eks/latest/best-practices/sgpp.html), [CloudFormation secure dynamic references](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references.html).
