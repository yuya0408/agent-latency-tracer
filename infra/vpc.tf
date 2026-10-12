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

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "agent-latency-tracer-private" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Free. AgentCore pulls container image layers from S3 (ECR stores them
# there). Scoped to that bucket only, as the AgentCore docs recommend.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  policy = jsonencode({
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = ["s3:GetObject"]
      Resource  = ["arn:aws:s3:::prod-${var.region}-starport-layer-bucket/*"]
    }]
  })
}
