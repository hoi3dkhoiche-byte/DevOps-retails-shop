variable "admin_cidrs" {
  description = "Actual public IPv4 /32 addresses allowed to administer EKS and access the lab UI."
  type        = list(string)
  validation {
    condition = length(var.admin_cidrs) > 0 && alltrue([for cidr in var.admin_cidrs : try(
      can(cidrnetmask(cidr)) && cidr == "${cidrhost(cidr, 0)}/32" && alltrue([
        for blocked in local.admin_blocked_ipv4_cidrs :
        sum([for index, octet in split(".", cidrhost(cidr, 0)) : tonumber(octet) * pow(256, 3 - index)]) <
        sum([for index, octet in split(".", cidrhost(blocked, 0)) : tonumber(octet) * pow(256, 3 - index)]) ||
        sum([for index, octet in split(".", cidrhost(cidr, 0)) : tonumber(octet) * pow(256, 3 - index)]) >
        sum([for index, octet in split(".", cidrhost(blocked, -1)) : tonumber(octet) * pow(256, 3 - index)])
      ]), false)
    ])
    error_message = "Supply real public administration IPv4 /32 addresses; private, documentation, multicast and reserved ranges are not allowed."
  }
}

variable "admin_role_arn" {
  description = "IAM role ARN (not an STS session ARN) granted cluster-admin through an EKS Access Entry."
  type        = string
  validation {
    condition = can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$", var.admin_role_arn)) && !can(regex("(?i)REPLACE|CHANGE_ME|YOUR_", var.admin_role_arn)) && try(
      !contains(["000000000000", "123456789012"], regex("^arn:aws:iam::([0-9]{12}):role/", var.admin_role_arn)[0]) &&
      regex("^arn:aws:iam::([0-9]{12}):role/", var.admin_role_arn)[0] == data.aws_caller_identity.current.account_id, false
    )
    error_message = "Use a real administration IAM role ARN in the active AWS account, without example account IDs or placeholders."
  }
}

variable "addon_versions" {
  description = "Explicit EKS 1.36-compatible builds returned by aws eks describe-addon-versions. No most_recent updates."
  type = object({
    vpc_cni    = string
    coredns    = string
    kube_proxy = string
    ebs_csi    = string
  })
  validation {
    condition     = alltrue([for version in values(var.addon_versions) : can(regex("^v[0-9].*-eksbuild\\.[0-9]+$", version))])
    error_message = "Pin every add-on to a complete EKS build version."
  }
}

variable "platform_secret_arns" {
  description = "Pre-created Secrets Manager credentials; Terraform stores ARNs only. Each secret has username/password."
  type        = object({ mq_admin = string, search_admin = string, redis_dev = string, redis_prod = string })
  validation {
    condition = alltrue([for arn in values(var.platform_secret_arns) :
      can(regex("^arn:aws:secretsmanager:ap-southeast-1:${try(regex("^arn:aws:iam::([0-9]{12}):role/", var.admin_role_arn)[0], "INVALID")}:secret:[A-Za-z0-9/_+=.@-]+$", arn)) &&
      !can(regex("(?i)REPLACE|CHANGE_ME|YOUR_", arn))
    ])
    error_message = "Platform secret ARNs must be non-placeholder Secrets Manager ARNs in the administration account and Singapore."
  }
}

variable "domain" {
  description = "Optional controlled DNS domain. Empty means the IP-restricted HTTP lab ingress."
  type        = string
  default     = ""
  validation {
    condition = (var.domain == "" && var.hosted_zone_id == "") || (
      length(var.domain) <= 253 && can(regex("^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$", var.domain)) &&
      !anytrue([for suffix in [".example", ".test", ".invalid", ".localhost", ".local"] : endswith(lower(var.domain), suffix)]) &&
      !can(regex("(^|\\.)example\\.(com|net|org)$", lower(var.domain))) && can(regex("^Z[A-Z0-9]+$", var.hosted_zone_id))
    )
    error_message = "Supply a real controlled domain together with its Route 53 hosted zone ID, or leave both empty."
  }
}

variable "hosted_zone_id" {
  type    = string
  default = ""
}

variable "mysql_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "mysql_multi_az" {
  type    = bool
  default = false
}

variable "create_opensearch_service_linked_role" {
  description = "Create the account-wide OpenSearch service-linked role only for a fresh account. False reuses an existing OpenSearch or legacy Elasticsearch role; import before enabling for an existing role."
  type        = bool
  default     = false
}

variable "github_oidc_provider_arn" {
  description = "Existing account-wide GitHub OIDC provider ARN, created once by the administrator."
  type        = string
  validation {
    condition     = var.github_oidc_provider_arn == "arn:aws:iam::${try(regex("^arn:aws:iam::([0-9]{12}):role/", var.admin_role_arn)[0], "INVALID")}:oidc-provider/token.actions.githubusercontent.com"
    error_message = "Use the token.actions.githubusercontent.com OIDC provider in the administration account."
  }
}

locals {
  admin_blocked_ipv4_cidrs = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
  ]
  name         = "retail-lab-eks"
  azs          = ["ap-southeast-1a", "ap-southeast-1b", "ap-southeast-1c"]
  public_cidrs = ["10.0.10.0/24", "10.0.20.0/24", "10.0.30.0/24"]
  app_cidrs    = ["10.0.40.0/22", "10.0.60.0/22", "10.0.80.0/22"]
  data_cidrs   = ["10.0.100.0/24", "10.0.110.0/24", "10.0.120.0/24"]
  groups = {
    management = { instance = "m6i.xlarge", disk = 80, maximum = 2, subnets = [0] }
    dev        = { instance = "m6i.large", disk = 50, maximum = 2, subnets = [0, 1, 2] }
    prod       = { instance = "m6i.large", disk = 50, maximum = 3, subnets = [0, 1, 2] }
  }
}
