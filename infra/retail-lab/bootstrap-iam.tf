locals {
  # The backend Job initializes generated platform credentials. Git repository
  # credentials are operator-supplied and must not be readable/writable by it.
  backend_bootstrap_platform_secret_keys = ["grafana/admin", "alertmanager/config", "argocd/admin", "argocd/redis"]
}

resource "aws_iam_role_policy" "backend_bootstrap" {
  role = aws_iam_role.workload["bootstrap"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource = concat(
        [aws_db_instance.mysql.master_user_secret[0].secret_arn],
        values(var.platform_secret_arns),
        [for secret in aws_secretsmanager_secret.application : secret.arn],
        [for key in local.backend_bootstrap_platform_secret_keys : aws_secretsmanager_secret.monitoring[key].arn]
      )
    },
    {
      Effect = "Allow"
      Action = ["secretsmanager:PutSecretValue"]
      Resource = concat(
        [for secret in aws_secretsmanager_secret.application : secret.arn],
        [for key in local.backend_bootstrap_platform_secret_keys : aws_secretsmanager_secret.monitoring[key].arn]
      )
    }
  ] })
}
