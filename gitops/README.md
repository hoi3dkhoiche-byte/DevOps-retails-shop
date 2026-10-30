# Retail GitOps seed

Copy the **contents of this directory** into `hoi3dkhoiche-byte/DevOps-retails-shop-gitops` on `main` after preparing its account-specific values. This directory is the reviewable seed; creating it does not publish the GitOps repository or deploy AWS resources.

The target is one EKS 1.36 cluster in Singapore, namespaces `retail-dev` and `retail-prod`, one shared RDS MySQL 8.4 server with separate database/users for Catalog, Cart and Orders in each environment, shared Redis with distinct ACLs/prefixes, OpenSearch with separate environment indices, and private multi-AZ RabbitMQ. Terraform manages the AWS resources and EKS managed add-ons. Argo CD manages platform charts and application resources.

## Prepare and bootstrap

1. Apply the reviewed infrastructure configuration from the source repository and run its backend bootstrap from inside the VPC. The backend bootstrap populates `/retail/platform/grafana/admin` with JSON `username`/`password`, `/retail/platform/argocd/admin` with the administration password/hash, timestamp, server signing key and self-signed TLS certificate/key, and `/retail/platform/argocd/redis` with its internal Redis password. ESO maps only the bcrypt hash, timestamp, server key and TLS fields into `argocd-secret`; the clear administration password stays in Secrets Manager for the operator to retrieve securely. Application secret paths and JSON keys are defined in `applications/templates/secrets.yaml`.
2. From the source checkout, export non-secret Terraform outputs to an ignored local file and fill this seed:

   ```powershell
   terraform -chdir=infra/retail-lab output -json | Set-Content -Encoding utf8 .ci-tools/terraform-outputs.json
   python -m pip install -r scripts/requirements-gitops.txt
   python scripts/prepare-gitops.py --terraform-output .ci-tools/terraform-outputs.json
   python gitops/validate.py
   ```

   `prepare-gitops.py` verifies account IDs, IAM roles, real administration `/32` CIDRs, SGs and optional ACM ARN before writing values. It copies only ARNs, IDs, bucket names and routing configuration. Password values are never inputs to this script.
3. Review and commit the prepared seed to the GitOps repository. Keep `environments/*/images.yaml` disabled until CI produces a complete accepted release. Configure Argo CD repository access separately if the repository is private; its credential must come from Secrets Manager/ESO rather than Git.
4. From your administration machine, using the IAM role granted the EKS administration Access Entry, run the source repository's bootstrap script:

   ```powershell
   ./scripts/bootstrap-argocd.ps1 -AwsProfile <admin-profile>
   ```

   This installs pinned ESO first, applies the Argo CD SecretStore/ExternalSecrets and waits for AWS credentials to synchronize, then installs the pinned Argo CD chart and creates `retail-root`. The root subsequently reconciles the same ESO release name, version and namespace. Use the actual Python executable with `-Python` if `python` is not on PATH. The EKS public endpoint accepts only the configured administration IPs. The script checks the AWS account and Kubernetes administration access before installation.
5. Check the root Application and child Applications, ESO readiness, metrics API and storage provisioning. Then enable the source repository's `GITOPS_ENABLED` variable and configure its `GITOPS_TOKEN` so a successful CI release can create the dev image PR.

The seed's `config.yaml` contains placeholders on purpose. `prepare-gitops.py --check` refuses an unprepared target. Add-on build versions must already have passed the infrastructure preflight for EKS 1.36.

## Reconciliation and releases

- Bootstrap creates `gp3-retain` before monitoring PVCs. Argo CD admin/TLS and its internal Redis credentials originate in Secrets Manager; its chart secret creation and Redis random-password Job are disabled. ESO and Rollouts controllers synchronize before monitoring credentials; Grafana waits for its ExternalSecret before the monitoring stack starts. Prometheus CRDs exist before PodMonitors, dashboards and the Rollouts ServiceMonitor are applied.
- Child application charts are read from the **same source commit as the CI release**, with `repository@sha256:...` for all five images. The seed does not deploy placeholder or `latest` application images.
- Merge the CI-created dev image PR to trigger dev automatic synchronization. UI dev promotes automatically. Production application synchronization and UI production promotion are manual; production promotion uses the complete image set accepted in dev and never builds another image set.
- Roll back by reverting the GitOps image change to the previous verified digest set and synchronizing. For an unpromoted UI revision, abort the Rollout. UI services are `ui-active` and `ui-preview`; the API services remain RollingUpdate Deployments.
- Each service uses its environment node-group affinity and matching toleration. HPA targets the UI Rollout, with dev max 3, prod max 7, CPU target 70%, controlled scale-up and a 300-second scale-down stabilization window.
- Application Services and container ports both use **8080**, matching the native VPC CNI NetworkPolicy requirement. Default deny policies allow only declared same-environment API calls, DNS, monitoring and the relevant private backend ports. Orders receives its dedicated pod SG in addition to the normal node SG. The VPC CNI preflight requires NetworkPolicy/SGP-capable builds and standard enforcement mode.

## Access and credentials

With a controlled domain, `dev.retail.<domain>` and `retail.<domain>` share the public `retail-public` ALB group with HTTPS. Dev adds an administration source-IP rule. Without a domain, only the production lab Ingress uses the IP-restricted ALB HTTP endpoint; access dev and preview through port-forward. Route 53 aliases must be pointed at the ALB after the controller creates it; Terraform must not create a second ALB.

```powershell
kubectl -n retail-dev port-forward service/ui-active 8080:8080
kubectl -n retail-prod port-forward service/ui-preview 8081:8080
kubectl -n argocd port-forward service/argocd-server 8443:443
kubectl -n monitoring port-forward service/kube-prometheus-stack-grafana 3000:80
```

ESO uses namespace-scoped SecretStores and distinct IRSA readers for dev, prod, monitoring and Argo CD. Application pod environment variables come from the resulting Secrets. Change `secretRevision` in the corresponding environment values after the Secret has refreshed to restart processes that read credentials from their environment. The included RDS PEM and PKCS12 truststore contain **public CA certificates only**; the truststore password `changeit` protects no private key or application credential.

## Monitoring, logs and backups

The pinned chart versions are recorded in `versions.yaml`. The autoscaler chart's controller image is explicitly `v1.36.1` to match the cluster minor. Its IRSA and discovery tags come from Terraform. No controller version automatically advances outside a reviewed change.

Prometheus retains 7 days with a 24GB cap on a 30Gi gp3 PVC. Loki runs one SingleBinary instance with a 10Gi working PVC and S3 storage; the Compactor enforces 7-day retention. Alloy runs on every relevant node, tolerates taints and discovers only pods assigned to its own node, avoiding duplicate log collection. Grafana has Prometheus, Loki and IRSA-backed CloudWatch data sources, the standard cluster dashboards and a Retail dashboard with workload/HPA/Rollout/HTTP/ALB/RDS panels.

Alert rules cover repeated crashes, unavailable API replicas, degraded UI Rollouts, low monitoring volume space, HTTP 5xx and failed Velero backups. Outbound notification is **opt-in** because the actual email/Telegram target is account-specific: store a complete Alertmanager configuration as JSON property `alertmanager.yaml` in `/retail/platform/alertmanager/config`, then rerun `prepare-gitops.py --enable-alerting` and review the resulting GitOps change. No example address or token is deployed.

Velero 1.18.2 uses AWS plugin 1.14.0, the compatible plugin line, to back up Kubernetes resources daily at 19:00 UTC and retain 14 days in S3 under `velero/`. It excludes Secrets, which ESO recreates. CSI snapshots and the node agent are disabled for this resource-backup scope. RDS MySQL automated backups/PITR and ElastiCache snapshots are managed by AWS; Velero does not back up those external databases. Monitoring data follows its retention policy. EFS is disabled until a real shared-file consumer is defined.

Before accepting the AWS deployment, exercise ESO rotation, blocked dev→prod connections, HPA/Cluster Autoscaler, UI preview/promote/abort, MySQL persistence, broker failover, and a measured restore. The design's MySQL RPO 15 minutes/RTO 2 hours remains an acceptance target until the restore has been timed.

## Validation evidence and limits

`python gitops/validate.py` renders both environments against the source charts and checks the digest, namespace, 8080 routing, scheduling, UI Rollout/HPA and reconciliation-order contracts. During implementation, all ten exact external chart packages were also downloaded and rendered with these values for Kubernetes 1.36. Static rendering does not assert that an EKS cluster, IAM trust, AWS regional capacity, backend credentials or restore performance have been tested in a real account.

Primary references: [native VPC CNI policy requirements](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html), [Rollouts metrics](https://argo-rollouts.readthedocs.io/en/stable/features/controller-metrics/), [Velero AWS plugin 1.14 compatibility](https://github.com/velero-io/velero-plugin-for-aws/blob/v1.14.0/README.md).
