# A VPC with private subnets only: no internet gateway, no NAT. RDS stays
# unreachable from the internet, and AgentCore reaches AWS services through
# VPC endpoints (the paid ones are added with AgentCore, not here).
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # VPC endpoints' private DNS names need both
  tags                 = { Name = "agent-latency-tracer" }
}

resource "aws_subnet" "private" {
  count                = length(var.az_ids)
  vpc_id               = aws_vpc.main.id
  availability_zone_id = var.az_ids[count.index]
  cidr_block           = cidrsubnet(var.vpc_cidr, 8, count.index) # 10.0.0.0/24, 10.0.1.0/24
  tags                 = { Name = "agent-latency-tracer-${var.az_ids[count.index]}" }
}

# A subnet of its own for the GPU server when its AZ is not one of az_ids:
# g6.xlarge runs short in one AZ at times (2026-11-05, apne1-az1).
resource "aws_subnet" "gpu" {
  count                = contains(var.az_ids, var.gpu_az_id) ? 0 : 1
  vpc_id               = aws_vpc.main.id
  availability_zone_id = var.gpu_az_id
  cidr_block           = cidrsubnet(var.vpc_cidr, 8, 10) # 10.0.10.0/24
  tags                 = { Name = "agent-latency-tracer-gpu-${var.gpu_az_id}" }
}

locals {
  gpu_subnet_id = (contains(var.az_ids, var.gpu_az_id)
    ? aws_subnet.private[index(var.az_ids, var.gpu_az_id)].id
  : aws_subnet.gpu[0].id)
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "agent-latency-tracer-private" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# For the S3 gateway endpoint (images and weights).
resource "aws_route_table_association" "gpu" {
  count          = length(aws_subnet.gpu)
  subnet_id      = aws_subnet.gpu[count.index].id
  route_table_id = aws_route_table.private.id
}

# Free. AgentCore and the GPU server pull container image layers from S3 (ECR
# stores them there), scoped to that bucket as the AgentCore docs recommend. The
# GPU server also takes the weights from the artifacts bucket.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  policy = jsonencode({
    Statement = [
      {
        Effect    = "Allow"
        Principal = "*"
        Action    = ["s3:GetObject"]
        Resource = [
          "arn:aws:s3:::prod-${var.region}-starport-layer-bucket/*",
          "arn:aws:s3:::${local.artifacts_bucket}/*",
        ]
      },
      {
        Effect    = "Allow"
        Principal = "*"
        Action    = ["s3:ListBucket"]
        Resource  = ["arn:aws:s3:::${local.artifacts_bucket}"]
      },
    ]
  })
}
