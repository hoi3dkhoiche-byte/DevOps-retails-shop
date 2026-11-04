# Admin creates and owns this policy outside the CI-managed root. CI may read it
# but cannot change its versions or remove/replace the boundary on delegated roles.
variable "terraform_workload_boundary_arn" {
  description = "Admin-owned reviewed boundary required on every CI-managed EKS/node/IRSA role."
  type        = string
  validation {
    condition = try(
      var.terraform_workload_boundary_arn == "arn:aws:iam::${split(":", var.admin_role_arn)[4]}:policy/retail-lab-workload-boundary", false
    )
    error_message = "Use the fixed retail-lab-workload-boundary policy ARN in the administration account."
  }
}

data "aws_iam_policy" "workload_boundary" {
  arn = var.terraform_workload_boundary_arn
  lifecycle {
    postcondition {
      condition     = jsondecode(self.policy) == jsondecode(replace(replace(file("${path.module}/policies/workload-boundary.json"), "__AWS_ACCOUNT_ID__", data.aws_caller_identity.current.account_id), "__AWS_REGION__", "ap-southeast-1"))
      error_message = "Admin-owned workload boundary must match the reviewed template before Terraform can change resources."
    }
  }
}
