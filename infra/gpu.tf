# The inference server: vLLM on a GPU, inside the VPC. Boots into serving with
# the images and weights from ../artifacts/ (gpu_boot.sh.tftpl). Only the agent's
# runtime calls it (agentcore_agent.tf). About $1.17 an hour, so destroy it after
# each session.

locals {
  artifacts_bucket = "agent-latency-tracer-artifacts-${local.account_id}"
}

# Driver and Docker only: vLLM and its PyTorch come in the image.
data "aws_ssm_parameter" "dlami" {
  name = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
}

resource "aws_security_group" "gpu" {
  name   = "agent-latency-tracer-gpu"
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "gpu_https" {
  security_group_id = aws_security_group.gpu.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# The weights and image layers come from S3 through the gateway endpoint, which
# keeps S3's public addresses: the VPC CIDR rule above doesn't cover them.
resource "aws_vpc_security_group_egress_rule" "gpu_s3" {
  security_group_id = aws_security_group.gpu.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "ssm_endpoints_from_gpu" {
  security_group_id            = aws_security_group.ssm_endpoints.id
  referenced_security_group_id = aws_security_group.gpu.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "agentcore_endpoints_from_gpu" {
  security_group_id            = aws_security_group.agentcore_endpoints.id
  referenced_security_group_id = aws_security_group.gpu.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_iam_role" "gpu" {
  name = "agent-latency-tracer-gpu"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "gpu_ssm" {
  role       = aws_iam_role.gpu.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "gpu" {
  role = aws_iam_role.gpu.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = "arn:aws:ecr:${var.region}:${local.account_id}:repository/agent-latency-tracer/*"
      },
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::${local.artifacts_bucket}/*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = "arn:aws:s3:::${local.artifacts_bucket}"
      },
      {
        # The Collector's spans. The OTLP endpoint answered 403 asking for
        # PutTraceSegments with PutSpans alone.
        Effect   = "Allow"
        Action   = ["xray:PutSpans", "xray:PutTraceSegments"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "gpu" {
  name = "agent-latency-tracer-gpu"
  role = aws_iam_role.gpu.name
}

resource "aws_instance" "gpu" {
  ami                    = data.aws_ssm_parameter.dlami.value
  instance_type          = "g6.xlarge"
  subnet_id              = local.gpu_subnet_id
  vpc_security_group_ids = [aws_security_group.gpu.id]
  iam_instance_profile   = aws_iam_instance_profile.gpu.name

  # The backstop for a forgotten server: the boot script shuts it down after 3
  # hours, and shutting down terminates it.
  instance_initiated_shutdown_behavior = "terminate"
  user_data_replace_on_change          = true
  user_data = templatefile("${path.module}/gpu_boot.sh.tftpl", {
    region           = var.region
    registry         = "${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"
    bucket           = local.artifacts_bucket
    collector_config = file("${path.module}/../system/serving/otel-collector.yaml")
  })

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_size           = 100
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = { Name = "agent-latency-tracer-gpu" }

  # The boot script uses all of these once, without retrying. Nothing here refers
  # to them, so a -target on this instance needs them spelled out.
  depends_on = [
    aws_route_table_association.private,
    aws_vpc_endpoint.ssm,
    aws_vpc_endpoint.agentcore,
    aws_vpc_security_group_ingress_rule.ssm_endpoints_from_gpu,
    aws_vpc_security_group_ingress_rule.agentcore_endpoints_from_gpu,
    aws_vpc_security_group_egress_rule.gpu_https,
    aws_vpc_security_group_egress_rule.gpu_s3,
    aws_iam_role_policy.gpu,
    aws_iam_role_policy_attachment.gpu_ssm,
  ]
}

output "gpu_instance_id" {
  value = aws_instance.gpu.id
}
