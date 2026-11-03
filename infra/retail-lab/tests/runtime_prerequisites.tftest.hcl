mock_provider "aws" {
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
  admin_cidrs              = ["8.8.8.8/32"]
  admin_role_arn           = "arn:aws:iam::111122223333:role/offline-admin"
  github_oidc_provider_arn = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
  terraform_policy_arn     = "arn:aws:iam::111122223333:policy/offline-infra"
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
