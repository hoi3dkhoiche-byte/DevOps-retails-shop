resource "aws_secretsmanager_secret" "monitoring" {
  for_each                = toset(["grafana/admin", "alertmanager/config", "argocd/admin", "argocd/redis"])
  name                    = "/retail/platform/${each.key}"
  recovery_window_in_days = 7
}

resource "aws_iam_role_policy" "eso_monitoring" {
  role = aws_iam_role.workload["eso-monitoring"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    Resource = [for key in ["grafana/admin", "alertmanager/config"] : aws_secretsmanager_secret.monitoring[key].arn]
  }] })
}

resource "aws_iam_role_policy" "eso_argocd" {
  role = aws_iam_role.workload["eso-argocd"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    Resource = [for key in ["argocd/admin", "argocd/redis"] : aws_secretsmanager_secret.monitoring[key].arn]
  }] })
}

resource "aws_iam_role_policy" "grafana" {
  role = aws_iam_role.workload["grafana"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow"
    Action = ["cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics",
      "cloudwatch:DescribeAlarms", "ec2:DescribeInstances", "ec2:DescribeRegions", "ec2:DescribeTags",
      "rds:DescribeDBInstances", "elasticache:DescribeCacheClusters", "elasticache:DescribeReplicationGroups",
    "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTargetGroups", "tag:GetResources"]
    Resource = "*"
  }] })
}
