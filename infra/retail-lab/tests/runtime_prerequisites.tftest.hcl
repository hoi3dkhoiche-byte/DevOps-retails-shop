mock_provider "aws" {
  mock_data "aws_iam_policy" {
    defaults = {
      policy = replace(replace(file("policies/workload-boundary.json"), "__AWS_ACCOUNT_ID__", "111122223333"), "__AWS_REGION__", "ap-southeast-1")
    }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_roles" {
    defaults = {
      names = ["AWSServiceRoleForAmazonOpenSearchService"]
      arns  = ["arn:aws:iam::111122223333:role/aws-service-role/opensearchservice.amazonaws.com/AWSServiceRoleForAmazonOpenSearchService"]
    }
  }
  mock_resource "aws_eks_cluster" {
    defaults = { identity = [{ oidc = [{ issuer = "https://oidc.eks.ap-southeast-1.amazonaws.com/id/OFFLINE" }] }] }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/offline-role" }
  }
  mock_resource "aws_launch_template" {
    defaults = { id = "lt-0123456789abcdef0", latest_version = 1 }
  }
  mock_resource "aws_eks_node_group" {
    defaults = { resources = [{ autoscaling_groups = [{ name = "offline-asg" }] }] }
  }
  mock_resource "aws_db_instance" {
    defaults = { master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:rds-master-offline" }] }
  }
}

mock_provider "tls" {
  mock_data "tls_certificate" {
    defaults = { certificates = [{ sha1_fingerprint = "1111111111111111111111111111111111111111" }] }
  }
}

variables {
  admin_cidrs                     = ["8.8.8.8/32"]
  admin_role_arn                  = "arn:aws:iam::111122223333:role/offline-admin"
  github_oidc_provider_arn        = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
  terraform_workload_boundary_arn = "arn:aws:iam::111122223333:policy/retail-lab-workload-boundary"
  terraform_policy_arn            = "arn:aws:iam::111122223333:policy/offline-infra"
  addon_versions = {
    vpc_cni    = "v1.21.1-eksbuild.1"
    coredns    = "v1.13.0-eksbuild.1"
    kube_proxy = "v1.36.0-eksbuild.1"
    ebs_csi    = "v1.51.0-eksbuild.1"
  }
  platform_secret_arns = {
    mq_admin     = "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:mq-offline"
    search_admin = "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:search-offline"
    redis_dev    = "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:redis-dev-offline"
    redis_prod   = "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:redis-prod-offline"
  }
}

run "reuse_existing_opensearch_role" {
  command = plan

  assert {
    condition     = length(aws_iam_service_linked_role.opensearch) == 0
    error_message = "Reusing the account role must not try to create another service-linked role."
  }
  assert {
    condition     = aws_vpc_endpoint.s3.vpc_endpoint_type == "Gateway" && aws_vpc_endpoint.s3.service_name == "com.amazonaws.ap-southeast-1.s3"
    error_message = "Application S3 traffic must use the regional gateway endpoint."
  }
}

run "default_architecture_capacity_includes_all_environments" {
  command = apply
  assert {
    condition     = output.deployment_profile == "architecture" && toset(keys(aws_eks_node_group.role)) == toset(["management", "dev", "prod"])
    error_message = "The architecture profile must retain management, dev and prod groups."
  }
  assert {
    condition = (
      aws_launch_template.nodes["management"].instance_type == "m6i.xlarge" &&
      aws_launch_template.nodes["dev"].instance_type == "m6i.large" &&
      !contains(keys(jsondecode(aws_eks_addon.cni.configuration_values).env), "ENABLE_PREFIX_DELEGATION") &&
      aws_db_instance.mysql.instance_class == "db.t4g.small" &&
      aws_db_instance.mysql.allocated_storage == 30 && aws_db_instance.mysql.backup_retention_period == 7 &&
      aws_db_instance.mysql.deletion_protection && !aws_db_instance.mysql.skip_final_snapshot &&
      length(aws_secretsmanager_secret.application) == 12 && length(aws_secretsmanager_secret.monitoring) == 5 &&
      alltrue([for secret in aws_secretsmanager_secret.application : secret.recovery_window_in_days == 7]) &&
      alltrue([for secret in aws_secretsmanager_secret.monitoring : secret.recovery_window_in_days == 7]) &&
      jsondecode(aws_cloudformation_stack.managed_backends.template_body).Resources.RabbitMQ.Properties.HostInstanceType == "mq.m7g.large" &&
      jsondecode(aws_cloudformation_stack.managed_backends.template_body).Resources.RabbitMQ.Properties.DeploymentMode == "CLUSTER_MULTI_AZ" &&
      length(jsondecode(aws_cloudformation_stack.managed_backends.template_body).Resources.RabbitMQ.Properties.SubnetIds) == 3 &&
      jsondecode(aws_cloudformation_stack.managed_backends.template_body).Resources.Search.Properties.EBSOptions.VolumeSize == 50
    )
    error_message = "The architecture profile must preserve the original sizing, backups, deletion guards and three-subnet broker."
  }
}

run "explicit_lab_is_small_and_disposable" {
  command = apply
  variables { deployment_profile = "lab" }
  assert {
    condition = (
      output.deployment_profile == "lab" && toset(keys(aws_eks_node_group.role)) == toset(["management", "dev"]) &&
      alltrue([for template in aws_launch_template.nodes : template.instance_type == "m7i-flex.large"]) &&
      jsondecode(aws_eks_addon.cni.configuration_values).env.ENABLE_PREFIX_DELEGATION == "true" &&
      jsondecode(aws_eks_addon.cni.configuration_values).env.POD_SECURITY_GROUP_ENFORCING_MODE == "standard" &&
      alltrue([for group in aws_eks_node_group.role : group.scaling_config[0].desired_size == 1 && group.scaling_config[0].max_size == 1]) &&
      aws_db_instance.mysql.instance_class == "db.t4g.micro" && aws_db_instance.mysql.allocated_storage == 20 &&
      aws_db_instance.mysql.backup_retention_period == 1 && !aws_db_instance.mysql.multi_az &&
      !aws_db_instance.mysql.deletion_protection && aws_db_instance.mysql.skip_final_snapshot &&
      length(aws_secretsmanager_secret.application) == 12 && length(aws_secretsmanager_secret.monitoring) == 5 &&
      alltrue([for secret in aws_secretsmanager_secret.application : secret.recovery_window_in_days == 0]) &&
      alltrue([for secret in aws_secretsmanager_secret.monitoring : secret.recovery_window_in_days == 0]) &&
      jsondecode(aws_cloudformation_stack.managed_backends.template_body).Resources.RabbitMQ.Properties.HostInstanceType == "mq.m7g.medium" &&
      jsondecode(aws_cloudformation_stack.managed_backends.template_body).Resources.RabbitMQ.Properties.DeploymentMode == "SINGLE_INSTANCE" &&
      length(jsondecode(aws_cloudformation_stack.managed_backends.template_body).Resources.RabbitMQ.Properties.SubnetIds) == 1 &&
      jsondecode(aws_cloudformation_stack.managed_backends.template_body).Resources.Search.Properties.EBSOptions.VolumeSize == 10 &&
      jsondecode(aws_cloudformation_stack.managed_backends.template_body).Resources.Redis.Properties.CacheNodeType == "cache.t4g.small"
    )
    error_message = "Lab must keep two small amd64 workers, disposable small RDS and a one-subnet broker without changing Redis."
  }
}

run "reject_an_unknown_deployment_profile" {
  command = plan
  variables { deployment_profile = "production" }
  expect_failures = [var.deployment_profile]
}

run "lab_cannot_enable_production_workers" {
  command = plan
  variables {
    deployment_profile           = "lab"
    deploy_production_node_group = true
  }
  expect_failures = [var.deploy_production_node_group]
}

run "lab_cannot_expand_mysql" {
  command = plan
  variables {
    deployment_profile   = "lab"
    mysql_instance_class = "db.t4g.small"
  }
  expect_failures = [var.mysql_instance_class]
}

run "dev_only_capacity_omits_production_workers" {
  command = plan
  variables { deploy_production_node_group = false }
  assert {
    condition = alltrue([
      toset(keys(aws_eks_node_group.role)) == toset(["management", "dev"]),
      toset(keys(aws_launch_template.nodes)) == toset(["management", "dev"]),
      toset(keys(aws_autoscaling_group_tag.discovery)) == toset(["management", "dev"]),
      toset(keys(aws_autoscaling_group_tag.enabled)) == toset(["management", "dev"]),
    ])
    error_message = "A dev-only deployment must omit production workers and their launch template/autoscaler tags."
  }
  assert {
    condition = (
      aws_eks_node_group.role["management"].node_group_name == "management" &&
      aws_eks_node_group.role["dev"].node_group_name == "dev" &&
      aws_eks_node_group.role["management"].scaling_config[0].desired_size == 1 &&
      aws_eks_node_group.role["dev"].scaling_config[0].desired_size == 1
    )
    error_message = "Dev-only mode must preserve the existing management/dev addresses, group names and initial capacity."
  }
}

run "reuse_legacy_elasticsearch_role" {
  command = plan

  override_data {
    target = data.aws_iam_roles.opensearch[0]
    values = {
      names = ["AWSServiceRoleForAmazonElasticsearchService"]
      arns  = ["arn:aws:iam::111122223333:role/aws-service-role/es.amazonaws.com/AWSServiceRoleForAmazonElasticsearchService"]
    }
  }
}

run "missing_account_role_stops_plan" {
  command = plan

  override_data {
    target = data.aws_iam_roles.opensearch[0]
    values = { names = [], arns = [] }
  }
  expect_failures = [aws_cloudformation_stack.managed_backends]
}

run "same_named_role_on_wrong_path_stops_plan" {
  command = plan

  override_data {
    target = data.aws_iam_roles.opensearch[0]
    values = {
      names = ["AWSServiceRoleForAmazonOpenSearchService"]
      arns  = ["arn:aws:iam::111122223333:role/incorrect/AWSServiceRoleForAmazonOpenSearchService"]
    }
  }
  expect_failures = [aws_cloudformation_stack.managed_backends]
}

run "fresh_account_creates_modern_role" {
  command = plan

  variables {
    create_opensearch_service_linked_role = true
  }
  assert {
    condition     = length(aws_iam_service_linked_role.opensearch) == 1 && length(data.aws_iam_roles.opensearch) == 0
    error_message = "Fresh-account mode must create one role without depending on an existing-role lookup."
  }
  assert {
    condition     = aws_iam_service_linked_role.opensearch[0].aws_service_name == "opensearchservice.amazonaws.com"
    error_message = "The created role must belong to Amazon OpenSearch Service."
  }
}

run "repository_credentials_are_only_read_by_argocd_eso" {
  command = apply

  assert {
    condition = contains(
      jsondecode(aws_iam_role_policy.eso_argocd.policy).Statement[0].Resource,
      aws_secretsmanager_secret.monitoring["argocd/repositories"].arn
    )
    error_message = "Argo CD's ESO reader must be able to read the repository credential container."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.backend_bootstrap.policy).Statement :
      !contains(statement.Resource, aws_secretsmanager_secret.monitoring["argocd/repositories"].arn)
    ])
    error_message = "The backend bootstrap Job must not read or overwrite repository credentials."
  }
}

run "all_runtime_roles_use_the_admin_owned_boundary" {
  command = plan
  assert {
    condition = alltrue(concat(
      [aws_iam_role.cluster.permissions_boundary == var.terraform_workload_boundary_arn,
      aws_iam_role.nodes.permissions_boundary == var.terraform_workload_boundary_arn],
      [for role in aws_iam_role.workload : role.permissions_boundary == var.terraform_workload_boundary_arn]
    ))
    error_message = "Every delegated EKS, node and IRSA role must retain the admin-owned permission ceiling."
  }
  assert {
    condition = toset(concat(
      [aws_iam_role.cluster.name, aws_iam_role.nodes.name],
      [for role in aws_iam_role.workload : role.name]
      )) == toset([
      for arn in one([for statement in jsondecode(file("${path.module}/policies/terraform-infra.json")).Statement : statement if statement.Sid == "ProjectIam"]).Resource :
      reverse(split("/", arn))[0]
    ])
    error_message = "The CI IAM allowlist must cover exactly the runtime roles declared in Terraform."
  }
}

run "reject_an_unreviewed_boundary_arn" {
  command = plan
  variables { terraform_workload_boundary_arn = "arn:aws:iam::111122223333:policy/unreviewed" }
  expect_failures = [var.terraform_workload_boundary_arn]
}

run "reject_a_tampered_boundary_document" {
  command = plan
  override_data {
    target = data.aws_iam_policy.workload_boundary
    values = { policy = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"*\",\"Resource\":\"*\"}]}" }
  }
  expect_failures = [data.aws_iam_policy.workload_boundary]
}
