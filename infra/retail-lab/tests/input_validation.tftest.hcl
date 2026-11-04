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

run "accept_public_host_and_matching_account_inputs" {
  command = plan
}

run "reject_private_admin_address" {
  command = plan
  variables { admin_cidrs = ["10.0.0.1/32"] }
  expect_failures = [var.admin_cidrs]
}

run "reject_documentation_admin_address" {
  command = plan
  variables { admin_cidrs = ["203.0.113.10/32"] }
  expect_failures = [var.admin_cidrs]
}

run "reject_shared_nat_admin_address" {
  command = plan
  variables { admin_cidrs = ["100.64.0.1/32"] }
  expect_failures = [var.admin_cidrs]
}

run "reject_multicast_admin_address" {
  command = plan
  variables { admin_cidrs = ["224.0.0.1/32"] }
  expect_failures = [var.admin_cidrs]
}

run "reject_non_host_admin_network" {
  command = plan
  variables { admin_cidrs = ["8.8.8.0/24"] }
  expect_failures = [var.admin_cidrs]
}

run "reject_ipv6_admin_address" {
  command = plan
  variables { admin_cidrs = ["2001:4860:4860::8888/128"] }
  expect_failures = [var.admin_cidrs]
}

run "reject_placeholder_role" {
  command = plan
  variables { admin_role_arn = "arn:aws:iam::111122223333:role/REPLACE_ADMIN_ROLE" }
  expect_failures = [var.admin_role_arn]
}

run "reject_oidc_provider_in_another_account" {
  command = plan
  variables { github_oidc_provider_arn = "arn:aws:iam::999988887777:oidc-provider/token.actions.githubusercontent.com" }
  expect_failures = [var.github_oidc_provider_arn]
}

run "reject_secret_in_another_region" {
  command = plan
  variables {
    platform_secret_arns = {
      mq_admin     = "arn:aws:secretsmanager:us-east-1:111122223333:secret:mq-offline"
      search_admin = "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:search-offline"
      redis_dev    = "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:redis-dev-offline"
      redis_prod   = "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:redis-prod-offline"
    }
  }
  expect_failures = [var.platform_secret_arns]
}

run "reject_domain_without_hosted_zone" {
  command = plan
  variables { domain = "company.vn" }
  expect_failures = [var.domain]
}

run "reject_example_domain" {
  command = plan
  variables {
    domain         = "example.com"
    hosted_zone_id = "ZABC123"
  }
  expect_failures = [var.domain]
}

run "reject_primary_policy_in_another_account" {
  command = plan
  variables { terraform_policy_arn = "arn:aws:iam::999988887777:policy/retail-lab-terraform" }
  expect_failures = [var.terraform_policy_arn]
}

run "reject_duplicate_additional_policy" {
  command = plan
  variables { terraform_additional_policy_arns = ["arn:aws:iam::111122223333:policy/offline-infra"] }
  expect_failures = [var.terraform_additional_policy_arns]
}
