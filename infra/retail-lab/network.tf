resource "aws_vpc" "retail" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "retail-lab" }
}

resource "aws_subnet" "public" {
  count                   = 3
  vpc_id                  = aws_vpc.retail.id
  availability_zone       = local.azs[count.index]
  cidr_block              = local.public_cidrs[count.index]
  map_public_ip_on_launch = false
  tags                    = { Name = "retail-public-${count.index}", "kubernetes.io/role/elb" = "1" }
}

resource "aws_subnet" "app" {
  count             = 3
  vpc_id            = aws_vpc.retail.id
  availability_zone = local.azs[count.index]
  cidr_block        = local.app_cidrs[count.index]
  tags              = { Name = "retail-app-${count.index}", "kubernetes.io/role/internal-elb" = "1" }
}

resource "aws_subnet" "data" {
  count             = 3
  vpc_id            = aws_vpc.retail.id
  availability_zone = local.azs[count.index]
  cidr_block        = local.data_cidrs[count.index]
  tags              = { Name = "retail-data-${count.index}" }
}

resource "aws_internet_gateway" "retail" {
  vpc_id = aws_vpc.retail.id
}
resource "aws_eip" "nat" {
  domain = "vpc"
}
resource "aws_nat_gateway" "lab" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.retail]
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.retail.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.retail.id
  }
}
resource "aws_route_table" "app" {
  vpc_id = aws_vpc.retail.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.lab.id
  }
}
# Data subnets intentionally have only the automatic VPC-local route.
resource "aws_route_table" "data" {
  vpc_id = aws_vpc.retail.id
}
resource "aws_route_table_association" "public" {
  count          = 3
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
resource "aws_route_table_association" "app" {
  count          = 3
  subnet_id      = aws_subnet.app[count.index].id
  route_table_id = aws_route_table.app.id
}
resource "aws_route_table_association" "data" {
  count          = 3
  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.data.id
}

resource "aws_security_group" "nodes" {
  name_prefix = "retail-nodes-"
  vpc_id      = aws_vpc.retail.id
  ingress {
    from_port = 0
    to_port   = 0
    protocol  = "-1"
    self      = true
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "orders_pods" {
  name_prefix = "retail-orders-pods-"
  vpc_id      = aws_vpc.retail.id
  description = "Additional identity SG attached only to Orders pods; node SG handles other traffic."
}

resource "aws_security_group" "backends" {
  for_each    = { mysql = 3306, redis = 6379, search = 443 }
  name_prefix = "retail-${each.key}-"
  vpc_id      = aws_vpc.retail.id
  ingress {
    from_port       = each.value
    to_port         = each.value
    protocol        = "tcp"
    security_groups = [aws_security_group.nodes.id]
  }
}

resource "aws_security_group" "mq" {
  name_prefix = "retail-mq-"
  vpc_id      = aws_vpc.retail.id
  ingress {
    description     = "AMQPS from Orders pods only"
    from_port       = 5671
    to_port         = 5671
    protocol        = "tcp"
    security_groups = [aws_security_group.orders_pods.id]
  }
  ingress {
    description = "Private management API for controlled bootstrap in management AZ"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [local.app_cidrs[0]]
  }
}
