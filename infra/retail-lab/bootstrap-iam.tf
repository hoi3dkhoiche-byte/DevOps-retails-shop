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
        [for secret in aws_secretsmanager_secret.monitoring : secret.arn]
      )
    },
    {
      Effect = "Allow"
      Action = ["secretsmanager:PutSecretValue"]
      Resource = concat(
        [for secret in aws_secretsmanager_secret.application : secret.arn],
        [for secret in aws_secretsmanager_secret.monitoring : secret.arn]
      )
    }
  ] })
}
