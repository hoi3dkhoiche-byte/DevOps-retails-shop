resource "aws_iam_role" "cluster" {
  name               = "retail-lab-eks-control-plane"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "eks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "cluster" {
  for_each   = toset(["AmazonEKSClusterPolicy", "AmazonEKSVPCResourceController"])
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/${each.value}"
}
resource "aws_eks_cluster" "retail" {
  name     = local.name
  role_arn = aws_iam_role.cluster.arn
  version  = "1.36"
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }
  vpc_config {
    subnet_ids              = aws_subnet.app[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.admin_cidrs
  }
  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  depends_on                = [aws_iam_role_policy_attachment.cluster]
}
resource "aws_eks_access_entry" "admin" {
  cluster_name  = aws_eks_cluster.retail.name
  principal_arn = var.admin_role_arn
  type          = "STANDARD"
}
resource "aws_eks_access_policy_association" "admin" {
  cluster_name  = aws_eks_cluster.retail.name
  principal_arn = aws_eks_access_entry.admin.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}
resource "aws_iam_role" "nodes" {
  name               = "retail-lab-eks-nodes"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "nodes" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly", "AmazonEKS_CNI_Policy", "AmazonSSMManagedInstanceCore"])
  role       = aws_iam_role.nodes.name
  policy_arn = "arn:aws:iam::aws:policy/${each.value}"
}
resource "aws_launch_template" "nodes" {
  for_each               = local.groups
  name_prefix            = "retail-${each.key}-"
  instance_type          = each.value.instance
  vpc_security_group_ids = [aws_security_group.nodes.id, aws_eks_cluster.retail.vpc_config[0].cluster_security_group_id]
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = each.value.disk
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
}
resource "aws_eks_node_group" "role" {
  for_each        = local.groups
  cluster_name    = aws_eks_cluster.retail.name
  node_group_name = each.key
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = [for index in each.value.subnets : aws_subnet.app[index].id]
  ami_type        = "AL2023_x86_64_STANDARD"
  capacity_type   = "ON_DEMAND"
  launch_template {
    id      = aws_launch_template.nodes[each.key].id
    version = aws_launch_template.nodes[each.key].latest_version
  }
  scaling_config {
    min_size     = 1
    desired_size = 1
    max_size     = each.value.maximum
  }
  update_config { max_unavailable = 1 }
  labels = { workload = each.key }
  taint {
    key    = "workload"
    value  = each.key
    effect = "NO_SCHEDULE"
  }
  tags = {
    "k8s.io/cluster-autoscaler/enabled"       = "true"
    "k8s.io/cluster-autoscaler/${local.name}" = "owned"
  }
  lifecycle { ignore_changes = [scaling_config[0].desired_size] }
  depends_on = [aws_iam_role_policy_attachment.nodes, aws_eks_addon.cni]
}
resource "aws_eks_addon" "cni" {
  cluster_name  = aws_eks_cluster.retail.name
  addon_name    = "vpc-cni"
  addon_version = var.addon_versions.vpc_cni
  configuration_values = jsonencode({ enableNetworkPolicy = "true", env = {
    ENABLE_POD_ENI = "true", POD_SECURITY_GROUP_ENFORCING_MODE = "standard", NETWORK_POLICY_ENFORCING_MODE = "standard"
  } })
}
resource "aws_eks_addon" "base" {
  for_each                 = { coredns = var.addon_versions.coredns, kube-proxy = var.addon_versions.kube_proxy, aws-ebs-csi-driver = var.addon_versions.ebs_csi }
  cluster_name             = aws_eks_cluster.retail.name
  addon_name               = each.key
  addon_version            = each.value
  service_account_role_arn = each.key == "aws-ebs-csi-driver" ? aws_iam_role.workload["ebs-csi"].arn : null
  configuration_values     = each.key == "coredns" ? jsonencode({ tolerations = [{ key = "workload", operator = "Exists", effect = "NoSchedule" }] }) : null
  depends_on               = [aws_eks_node_group.role]
}
