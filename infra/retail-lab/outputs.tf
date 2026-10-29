output "cluster_name" { value = aws_eks_cluster.retail.name }
output "vpc_id" { value = aws_vpc.retail.id }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "mysql_endpoint" { value = aws_db_instance.mysql.address }
output "mysql_master_secret_arn" { value = aws_db_instance.mysql.master_user_secret[0].secret_arn }
output "backend_endpoints" { value = aws_cloudformation_stack.managed_backends.outputs }
output "workload_roles" { value = { for name, role in aws_iam_role.workload : name => role.arn } }
output "node_security_group_id" { value = aws_security_group.nodes.id }
output "orders_security_group_id" { value = aws_security_group.orders_pods.id }
output "buckets" { value = { for name, bucket in aws_s3_bucket.data : name => bucket.id } }
output "terraform_role_arn" { value = aws_iam_role.terraform.arn }
output "certificate_arn" { value = try(aws_acm_certificate_validation.ui[0].certificate_arn, "") }
