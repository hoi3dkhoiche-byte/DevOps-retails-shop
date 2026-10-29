resource "aws_db_subnet_group" "mysql" {
  name       = "retail-lab-mysql"
  subnet_ids = aws_subnet.data[*].id
}
resource "aws_db_parameter_group" "mysql" {
  name_prefix = "retail-mysql84-"
  family      = "mysql8.4"
  parameter {
    name  = "require_secure_transport"
    value = "ON"
  }
}
resource "aws_db_instance" "mysql" {
  identifier                      = "retail-lab-mysql"
  engine                          = "mysql"
  engine_version                  = "8.4.11"
  instance_class                  = var.mysql_instance_class
  allocated_storage               = 30
  max_allocated_storage           = 100
  storage_type                    = "gp3"
  storage_encrypted               = true
  username                        = "retailadmin"
  manage_master_user_password     = true
  db_subnet_group_name            = aws_db_subnet_group.mysql.name
  vpc_security_group_ids          = [aws_security_group.backends["mysql"].id]
  parameter_group_name            = aws_db_parameter_group.mysql.name
  publicly_accessible             = false
  multi_az                        = var.mysql_multi_az
  backup_retention_period         = 7
  backup_window                   = "18:00-19:00"
  maintenance_window              = "sun:19:00-sun:20:00"
  auto_minor_version_upgrade      = false
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "retail-lab-mysql-final"
  enabled_cloudwatch_logs_exports = ["error", "slowquery"]
}

locals {
  application_secrets = toset(flatten([for env in ["dev", "prod"] : [
    "/retail/${env}/catalog/mysql", "/retail/${env}/cart/mysql", "/retail/${env}/orders/mysql",
    "/retail/${env}/checkout/redis", "/retail/${env}/orders/rabbitmq", "/retail/${env}/catalog/opensearch"
  ]]))
}
resource "aws_secretsmanager_secret" "application" {
  for_each                = local.application_secrets
  name                    = each.key
  recovery_window_in_days = 7
}

# CloudFormation resolves Secrets Manager references server-side. Password values
# never enter a Terraform variable, data source, plan, output or state file.
# Populate the platform secrets before applying this stack.
resource "aws_cloudformation_stack" "managed_backends" {
  name = "retail-lab-managed-backends"
  template_body = jsonencode({
    AWSTemplateFormatVersion = "2010-09-09"
    Resources = {
      RabbitMQ = {
        Type = "AWS::AmazonMQ::Broker"
        Properties = {
          BrokerName              = "retail-lab-rabbitmq"
          EngineType              = "RABBITMQ"
          EngineVersion           = "4.2"
          HostInstanceType        = "mq.m7g.large"
          DeploymentMode          = "CLUSTER_MULTI_AZ"
          PubliclyAccessible      = false
          AutoMinorVersionUpgrade = true
          SubnetIds               = aws_subnet.data[*].id
          SecurityGroups          = [aws_security_group.mq.id]
          Logs                    = { General = true }
          Users = [{
            Username = "{{resolve:secretsmanager:${var.platform_secret_arns.mq_admin}:SecretString:username}}"
            Password = "{{resolve:secretsmanager:${var.platform_secret_arns.mq_admin}:SecretString:password}}"
          }]
        }
      }
      RedisSubnetGroup = {
        Type       = "AWS::ElastiCache::SubnetGroup"
        Properties = { Description = "Retail private data subnets", SubnetIds = aws_subnet.data[*].id }
      }
      RedisDefaultUser = {
        Type       = "AWS::ElastiCache::User"
        Properties = { Engine = "redis", UserId = "retail-disabled-default", UserName = "default", AccessString = "off -@all", NoPasswordRequired = true }
      }
      RedisDevUser = {
        Type = "AWS::ElastiCache::User"
        Properties = {
          Engine       = "redis", UserId = "retail-checkout-dev", UserName = "checkout-dev"
          AccessString = "on ~retail:dev:* +get +set +del +ping +info +client"
          Passwords    = ["{{resolve:secretsmanager:${var.platform_secret_arns.redis_dev}:SecretString:password}}"]
        }
      }
      RedisProdUser = {
        Type = "AWS::ElastiCache::User"
        Properties = {
          Engine       = "redis", UserId = "retail-checkout-prod", UserName = "checkout-prod"
          AccessString = "on ~retail:prod:* +get +set +del +ping +info +client"
          Passwords    = ["{{resolve:secretsmanager:${var.platform_secret_arns.redis_prod}:SecretString:password}}"]
        }
      }
      RedisUsers = {
        Type       = "AWS::ElastiCache::UserGroup"
        Properties = { Engine = "redis", UserGroupId = "retail-checkout", UserIds = [{ Ref = "RedisDefaultUser" }, { Ref = "RedisDevUser" }, { Ref = "RedisProdUser" }] }
      }
      Redis = {
        Type = "AWS::ElastiCache::ReplicationGroup"
        Properties = {
          ReplicationGroupId       = "retail-lab-redis", ReplicationGroupDescription = "Shared checkout cache with per-environment ACLs"
          Engine                   = "redis", EngineVersion = "7.1", CacheNodeType = "cache.t4g.small", NumCacheClusters = 1
          AutomaticFailoverEnabled = false, MultiAZEnabled = false
          CacheSubnetGroupName     = { Ref = "RedisSubnetGroup" }, SecurityGroupIds = [aws_security_group.backends["redis"].id]
          AtRestEncryptionEnabled  = true, TransitEncryptionEnabled = true, TransitEncryptionMode = "required"
          SnapshotRetentionLimit   = 7, SnapshotWindow = "18:00-19:00", UserGroupIds = [{ Ref = "RedisUsers" }]
        }
      }
      Search = {
        Type = "AWS::OpenSearchService::Domain"
        Properties = {
          DomainName              = "retail-lab-search", EngineVersion = "OpenSearch_3.3"
          ClusterConfig           = { InstanceType = "r8g.medium.search", InstanceCount = 1, ZoneAwarenessEnabled = false }
          EBSOptions              = { EBSEnabled = true, VolumeType = "gp3", VolumeSize = 50 }
          VPCOptions              = { SubnetIds = [aws_subnet.data[0].id], SecurityGroupIds = [aws_security_group.backends["search"].id] }
          EncryptionAtRestOptions = { Enabled = true }, NodeToNodeEncryptionOptions = { Enabled = true }
          DomainEndpointOptions   = { EnforceHTTPS = true, TLSSecurityPolicy = "Policy-Min-TLS-1-2-2019-07" }
          AdvancedSecurityOptions = {
            Enabled = true, InternalUserDatabaseEnabled = true
            MasterUserOptions = {
              MasterUserName     = "{{resolve:secretsmanager:${var.platform_secret_arns.search_admin}:SecretString:username}}"
              MasterUserPassword = "{{resolve:secretsmanager:${var.platform_secret_arns.search_admin}:SecretString:password}}"
            }
          }
          AccessPolicies = { Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { AWS = "*" }, Action = "es:ESHttp*", Resource = "arn:aws:es:ap-southeast-1:${data.aws_caller_identity.current.account_id}:domain/retail-lab-search/*" }] }
        }
      }
    }
    Outputs = {
      RabbitMQEndpoint = { Value = { "Fn::Select" = [0, { "Fn::GetAtt" = ["RabbitMQ", "AmqpEndpoints"] }] } }
      RedisEndpoint    = { Value = { "Fn::GetAtt" = ["Redis", "PrimaryEndPoint.Address"] } }
      SearchEndpoint   = { Value = { "Fn::GetAtt" = ["Search", "DomainEndpoint"] } }
    }
  })
}

resource "aws_s3_bucket" "data" {
  for_each = toset(["logs", "backup"])
  bucket   = "retail-lab-${data.aws_caller_identity.current.account_id}-${each.value}"
}
resource "aws_s3_bucket_public_access_block" "data" {
  for_each                = aws_s3_bucket.data
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}
resource "aws_s3_bucket_versioning" "state" {
  bucket = "manhnguyen1131"
  versioning_configuration { status = "Enabled" }
}

resource "aws_acm_certificate" "ui" {
  count                     = var.domain == "" ? 0 : 1
  domain_name               = "retail.${var.domain}"
  subject_alternative_names = ["dev.retail.${var.domain}"]
  validation_method         = "DNS"
  lifecycle { create_before_destroy = true }
}
resource "aws_route53_record" "certificate" {
  for_each = var.domain == "" ? {} : { for option in aws_acm_certificate.ui[0].domain_validation_options : option.domain_name => option }
  zone_id  = var.hosted_zone_id
  name     = each.value.resource_record_name
  type     = each.value.resource_record_type
  records  = [each.value.resource_record_value]
  ttl      = 60
}
resource "aws_acm_certificate_validation" "ui" {
  count                   = var.domain == "" ? 0 : 1
  certificate_arn         = aws_acm_certificate.ui[0].arn
  validation_record_fqdns = [for record in aws_route53_record.certificate : record.fqdn]
}
