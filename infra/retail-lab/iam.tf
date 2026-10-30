data "tls_certificate" "eks" {
  url = aws_eks_cluster.retail.identity[0].oidc[0].issuer
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
  name                 = "retail-lab-terraform"
  max_session_duration = 7200
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
}
resource "aws_iam_role_policy_attachment" "terraform" {
  role       = aws_iam_role.terraform.name
  policy_arn = var.terraform_policy_arn
}
