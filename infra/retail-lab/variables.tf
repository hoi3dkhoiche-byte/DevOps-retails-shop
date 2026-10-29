variable "admin_cidrs" {
  description = "Actual public IPv4 /32 addresses allowed to administer EKS and access the lab UI."
  type        = list(string)
  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for cidr in var.admin_cidrs : can(cidrhost(cidr, 0)) && endswith(cidr, "/32")])
    error_message = "Supply at least one real administration IPv4 /32; public 0.0.0.0/0 is not allowed."
  }
}

variable "admin_role_arn" {
  description = "IAM role ARN (not an STS session ARN) granted cluster-admin through an EKS Access Entry."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/.+", var.admin_role_arn))
    error_message = "Use your administration IAM role ARN."
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
}

variable "domain" {
  description = "Optional controlled DNS domain. Empty means the IP-restricted HTTP lab ingress."
  type        = string
  default     = ""
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

variable "github_oidc_provider_arn" {
  description = "Existing account-wide GitHub OIDC provider ARN, created once by the administrator."
  type        = string
}

locals {
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
