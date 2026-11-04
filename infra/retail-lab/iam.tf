data "tls_certificate" "eks" {
  url = aws_eks_cluster.retail.identity[0].oidc[0].issuer
}

# The role is shared by every OpenSearch VPC domain in the account. Reuse an
# existing role by default, including the legacy Elasticsearch role AWS reuses.
data "aws_iam_roles" "opensearch" {
  count       = var.create_opensearch_service_linked_role ? 0 : 1
  name_regex  = "^AWSServiceRoleForAmazon(OpenSearch|Elasticsearch)Service$"
  path_prefix = "/aws-service-role/"
}

resource "aws_iam_service_linked_role" "opensearch" {
  count            = var.create_opensearch_service_linked_role ? 1 : 0
  aws_service_name = "opensearchservice.amazonaws.com"
  lifecycle { prevent_destroy = true }
}

locals {
  opensearch_service_linked_role_arns = [
    "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/aws-service-role/opensearchservice.amazonaws.com/AWSServiceRoleForAmazonOpenSearchService",
    "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/aws-service-role/es.amazonaws.com/AWSServiceRoleForAmazonElasticsearchService",
  ]
}
resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.retail.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
}
locals {
  oidc_host = replace(aws_eks_cluster.retail.identity[0].oidc[0].issuer, "https://", "")
  service_accounts = {
    ebs-csi        = "kube-system:ebs-csi-controller-sa"
    lb             = "kube-system:aws-load-balancer-controller"
    autoscaler     = "kube-system:cluster-autoscaler"
    rollouts       = "argo-rollouts:argo-rollouts"
    eso-dev        = "retail-dev:external-secrets-reader"
    eso-prod       = "retail-prod:external-secrets-reader"
    eso-monitoring = "monitoring:external-secrets-reader"
    eso-argocd     = "argocd:external-secrets-reader"
    grafana        = "monitoring:grafana"
    loki           = "monitoring:loki"
    velero         = "velero:velero"
    bootstrap      = "retail-ops:backend-bootstrap"
  }
}
resource "aws_iam_role" "workload" {
  for_each = local.service_accounts
  name     = "retail-lab-${each.key}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }, Action = "sts:AssumeRoleWithWebIdentity"
    Condition = { StringEquals = { "${local.oidc_host}:aud" = "sts.amazonaws.com", "${local.oidc_host}:sub" = "system:serviceaccount:${each.value}" } }
  }] })
}
resource "aws_iam_role_policy_attachment" "ebs" {
  role       = aws_iam_role.workload["ebs-csi"].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}
resource "aws_iam_role_policy" "eso" {
  for_each = toset(["dev", "prod"])
  role     = aws_iam_role.workload["eso-${each.key}"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    Resource = "arn:aws:secretsmanager:ap-southeast-1:${data.aws_caller_identity.current.account_id}:secret:/retail/${each.key}/*"
  }] })
}
resource "aws_iam_role_policy" "autoscaler" {
  role = aws_iam_role.workload["autoscaler"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeAutoScalingInstances", "autoscaling:DescribeLaunchConfigurations", "autoscaling:DescribeScalingActivities", "autoscaling:DescribeTags", "ec2:DescribeImages", "ec2:DescribeInstanceTypes", "ec2:DescribeLaunchTemplateVersions", "ec2:GetInstanceTypesFromInstanceRequirements", "eks:DescribeNodegroup"], Resource = "*" },
    { Effect = "Allow", Action = ["autoscaling:SetDesiredCapacity", "autoscaling:TerminateInstanceInAutoScalingGroup"], Resource = "*", Condition = { StringEquals = { "aws:ResourceTag/k8s.io/cluster-autoscaler/enabled" = "true", "aws:ResourceTag/k8s.io/cluster-autoscaler/${local.name}" = "owned" } } }
  ] })
}
resource "aws_autoscaling_group_tag" "discovery" {
  for_each               = local.groups
  autoscaling_group_name = aws_eks_node_group.role[each.key].resources[0].autoscaling_groups[0].name
  tag {
    key                 = "k8s.io/cluster-autoscaler/${local.name}"
    value               = "owned"
    propagate_at_launch = false
  }
}
resource "aws_autoscaling_group_tag" "enabled" {
  for_each               = local.groups
  autoscaling_group_name = aws_eks_node_group.role[each.key].resources[0].autoscaling_groups[0].name
  tag {
    key                 = "k8s.io/cluster-autoscaler/enabled"
    value               = "true"
    propagate_at_launch = false
  }
}
resource "aws_iam_role_policy" "rollouts" {
  role   = aws_iam_role.workload["rollouts"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetHealth", "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeListeners", "elasticloadbalancing:DescribeRules"], Resource = "*" }] })
}
resource "aws_iam_role_policy" "object_store" {
  for_each = { loki = "logs", velero = "backup" }
  role     = aws_iam_role.workload[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"], Resource = aws_s3_bucket.data[each.value].arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], Resource = "${aws_s3_bucket.data[each.value].arn}/${each.key == "velero" ? "velero/" : ""}*" }
  ] })
}
resource "aws_iam_role_policy" "lb" {
  role   = aws_iam_role.workload["lb"].id
  policy = file("${path.module}/policies/load-balancer-controller.json")
}

resource "aws_iam_role" "terraform" {
  # Admin imports/reconciles this foundation role and attachments below.
  # The CI identity policy denies self-mutation; CI refreshes a no-op copy only.
  name                 = "retail-lab-terraform"
  max_session_duration = 7200
  lifecycle { prevent_destroy = true }
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Federated = var.github_oidc_provider_arn }, Action = "sts:AssumeRoleWithWebIdentity"
    Condition = { StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com", "token.actions.githubusercontent.com:sub" = "repo:hoi3dkhoiche-byte/DevOps-retails-shop:ref:refs/heads/main" } }
  }] })
}
# Operator supplies an explicit reviewed AWS infrastructure policy during bootstrap.
# Application CI is never granted this role or an EKS Access Entry.
variable "terraform_policy_arn" {
  description = "Reviewed infrastructure policy attached to the dedicated GitHub Terraform role."
  type        = string
  validation {
    condition = can(regex("^arn:aws:iam::[0-9]{12}:policy/[A-Za-z0-9+=,.@_/-]+$", var.terraform_policy_arn)) && !strcontains(lower(var.terraform_policy_arn), "replace") && try(
      split(":", var.terraform_policy_arn)[4] == split(":", var.admin_role_arn)[4], false
    )
    error_message = "Use the reviewed customer-managed infrastructure policy ARN in the administration role's account, without placeholders."
  }
}
variable "terraform_additional_policy_arns" {
  description = "Additional reviewed infrastructure policy parts when the complete policy exceeds the IAM managed-policy size limit."
  type        = set(string)
  default     = []
  validation {
    condition = length(var.terraform_additional_policy_arns) <= 9 && !contains(var.terraform_additional_policy_arns, var.terraform_policy_arn) && alltrue([
      for arn in var.terraform_additional_policy_arns :
      can(regex("^arn:aws:iam::[0-9]{12}:policy/[A-Za-z0-9+=,.@_/-]+$", arn)) && !strcontains(lower(arn), "replace") && try(
        split(":", arn)[4] == split(":", var.admin_role_arn)[4], false
      )
    ])
    error_message = "Use at most nine distinct additional reviewed policy ARNs in the administration role's account; exclude the primary policy and placeholders."
  }
}
resource "aws_iam_role_policy_attachment" "terraform" {
  role       = aws_iam_role.terraform.name
  policy_arn = var.terraform_policy_arn
}
resource "aws_iam_role_policy_attachment" "terraform_additional" {
  for_each   = var.terraform_additional_policy_arns
  role       = aws_iam_role.terraform.name
  policy_arn = each.value
}
