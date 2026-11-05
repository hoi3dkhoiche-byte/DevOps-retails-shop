# Temporary lab deployment profiles

Use `deployment_profile = "lab"` for a short create → verify → delete session. This is the default for new Terraform inputs. Select `deployment_profile = "architecture"` explicitly to retain the full original design. Merge the selected key into the complete reviewed tfvars object; neither snippet is a complete configuration:

```json
{ "deployment_profile": "lab" }
```

```json
{ "deployment_profile": "architecture" }
```

| Component | `lab` | `architecture` |
| --- | --- | --- |
| Management worker | One `m7i-flex.large`, maximum one, 30Gi encrypted root disk | One `m6i.xlarge`, maximum two, 80Gi root disk |
| Dev worker | One `m7i-flex.large`, maximum one, 30Gi encrypted root disk | One `m6i.large`, maximum two, 50Gi root disk |
| Production workers | Absent | One `m6i.large`, maximum three; optional `deploy_production_node_group=false` omits it |
| MySQL | `db.t4g.micro`, 20Gi gp3, Single-AZ, storage autoscaling off, backup retention one day | `db.t4g.small`, 30Gi gp3 with 100Gi autoscaling ceiling, Single-AZ by default, seven-day retention |
| MySQL deletion | Deletion protection off; skip final snapshot | Deletion protection on; final snapshot required |
| Terraform-owned secret containers | 12 application + 5 monitoring/Argo CD secrets: no recovery window on deletion | The same 17 containers: seven-day recovery window |
| RabbitMQ | 4.2, `mq.m7g.medium`, `SINGLE_INSTANCE`, exactly the first private data subnet | 4.2, `mq.m7g.large`, `CLUSTER_MULTI_AZ`, three private data subnets |
| OpenSearch | 3.3, one `r8g.medium.search`, 10Gi gp3 | Same engine/type, 50Gi gp3 |
| Redis | 7.1, one `cache.t4g.small`, TLS and separate environment ACLs | Unchanged |

Both profiles retain the existing VPC/EKS, management/dev workload labels, database/user separation and IAM resource addresses. The optional plain-string Terraform output `deployment_profile` carries the selection through the allowlisted deployment context to GitOps/Ansible. Legacy output files without that key retain `architecture` semantics. Lab profile values reject production workers, Multi-AZ MySQL and larger MySQL class overrides; select `architecture` for those reviewed changes.

The lab still uses EKS, NAT, managed backend services, S3 and their supporting resources. It is smaller than the full design; it is not a promise of zero cost or universal Free-plan eligibility. Check the actual account restrictions, regional support and pricing before a session. The one-day RDS backup setting is a candidate for the Free-plan constraint observed during the previous attempt; **this revised backend profile has not been accepted by a live apply**. Regional orderability and EC2 `FreeTierEligible` metadata do not prove that every managed backend is available under the account plan.

## Account and regional checks

Preflight validates STS and matching account-plan metadata before state/IAM checks. `architecture` requires `PAID` and `ACTIVE`. `lab` allows `FREE` or `PAID`, always with `ACTIVE`; unknown, missing, mismatched or unavailable plan metadata stops deployment. Credits are independent of plan/service eligibility. No workflow or helper upgrades the account plan.

On `FREE`, lab preflight requires `DescribeInstanceTypes` to return exactly one matching worker type, `FreeTierEligible=true`, and `x86_64` support. False, missing, duplicate or wrong-architecture metadata stops the run. It then checks every selected instance/AZ pair. RDS checks the selected MySQL version/class, encrypted gp3 storage range and VPC/Multi-AZ capability. MQ checks the selected engine, type, deployment mode and subnet AZs. OpenSearch limits are checked against the selected 10Gi or 50Gi volume rather than a fixed full-profile volume. These are read-only checks; catalog support does not guarantee capacity or create permission.

Saved-plan apply derives the profile from the already checksum-verified binary plan using `terraform show -json`, after provider initialization. An unbound command-line profile cannot turn an architecture plan into a Free-plan lab. The account-plan gate and, for a Free lab, worker eligibility/offering checks run again before apply. A legacy plan without a profile uses the architecture/Paid rule.

## Session lifecycle and existing deployments

1. Review the profile, account, regional preflight and saved plan. Keep production application synchronization disabled for a lab.
2. Apply the reviewed create plan, export the deployment context and run backend/GitOps bootstrap and the dev verification steps.
3. Save the non-secret acceptance reports needed for the lab, then review a separate teardown plan. Teardown must follow the repository's IAM-preservation and resource-ownership checks; do not rerun a create operation to clean up.
4. Verify AWS APIs for remaining project resources and keep the retained IAM/state inventory consistent before a later session. A stopped or failed apply does not mean that resources were deleted.

Lab deletion deliberately skips the final MySQL snapshot, so temporary database contents are disposable. Retain the reports/data you need before cleanup. State and IAM handoff are separate from temporary workload deletion.

For an existing full deployment, pin `deployment_profile="architecture"` before planning with this code. Changing profiles preserves Terraform addresses but can replace workers/brokers, resize storage or destroy the production group. It also changes MySQL deletion behavior. Review those changes and data retention explicitly; do not switch an active deployment merely to get past account-plan checks.

## Secret names between lab sessions

For future lab deployments, Terraform sets `recovery_window_in_days=0` on all 17 application/monitoring secret containers. Their deletion is irreversible; the architecture profile retains seven days. AWS still performs permanent deletion asynchronously, so wait for the old name to disappear and retry with backoff if an immediate same-name create is rejected. This source setting does not force-delete or cancel the recovery window of secrets already scheduled by an earlier cleanup.

The four pre-Terraform secrets created by `init-platform` have a separate lifecycle: `/retail/platform/mq/admin`, `/retail/platform/search/admin`, `/retail/platform/redis/dev` and `/retail/platform/redis/prod`. The reviewed teardown helper captures only their metadata and deletes them after EKS and every backend are absent. Its default keeps seven-day recovery. Explicit `--platform-secret-recovery-days 0` during teardown preparation selects immediate deletion only when the captured Terraform secret containers prove the lab recovery policy; it never accelerates deletion of an already scheduled secret.

To reuse those four names during their recovery period, the operator can explicitly select:

```bash
python scripts/infra/bootstrap_backends.py init-platform --restore-scheduled-platform-secrets
```

The opt-in checks only the four fixed names, cancels their scheduled deletion where necessary, then reuses their existing credential values. It never restores `/retail/dev/*`, `/retail/prod/*` or the five monitoring/Argo CD containers, accepts no arbitrary secret-name argument and cannot be used with `configure`. Without the flag, a scheduled platform secret stops initialization. The normal path rechecks metadata after restoration; a failed or not-yet-visible restore does not bypass the deletion guard. Ansible exposes the same explicit boolean as `retail_restore_scheduled_platform_secrets`, default `false`.

Legacy application/monitoring secrets that already have a recovery window require a separate operator choice before another create: wait until permanent deletion completes, or manually restore the exact reviewed identities and reconcile/import them at their existing Terraform addresses. Restoring is an AWS write and is not done automatically by plan, apply, backend bootstrap or teardown. Inspect `DeletedDate` with Secrets Manager metadata APIs; do not copy credential values into tfvars, logs or chat. Keep the retained IAM/state recovery manifest when recreating the lab.

References: [EC2 instance-type metadata](https://docs.aws.amazon.com/cli/latest/reference/ec2/describe-instance-types.html), [RabbitMQ broker sizing](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/rmq-broker-instance-types.html), [private broker subnet selection](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/configuring-private-broker.html), [account-plan API](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_freetier_GetAccountPlanState.html), [asynchronous secret deletion](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_DeleteSecret.html), [explicit secret restoration](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_RestoreSecret.html).
