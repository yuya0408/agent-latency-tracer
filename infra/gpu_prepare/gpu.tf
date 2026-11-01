# A throwaway GPU server with internet access, for scripts/gpu_prepare.sh: it
# moves the images and weights into ../artifacts/, and tries the engine once.
# Destroy it when done (about $1.17 an hour).

locals {
  instance_type = "g6.xlarge"
  account_id    = data.aws_caller_identity.current.account_id
  bucket_arn    = "arn:aws:s3:::agent-latency-tracer-artifacts-${local.account_id}"
}

data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

# Only the AZs that offer g6.xlarge (not every one in Tokyo does).
data "aws_ec2_instance_type_offerings" "gpu" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [local.instance_type]
  }
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = data.aws_ec2_instance_type_offerings.gpu.locations
  }
}

# Driver and Docker only: vLLM and its PyTorch come in the image.
data "aws_ssm_parameter" "dlami" {
  name = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
}

# No ingress rules: Session Manager connects outward from the server.
resource "aws_security_group" "gpu" {
  name   = "agent-latency-tracer-gpu-prepare"
  vpc_id = data.aws_vpc.default.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role" "gpu" {
  name = "agent-latency-tracer-gpu-prepare"
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
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
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
        Action   = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
        Resource = "${local.bucket_arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = local.bucket_arn
      },
      {
        # The Collector's spans (system/serving/otel-collector.yaml). The OTLP
        # endpoint answered 403 asking for PutTraceSegments with PutSpans alone.
        Effect   = "Allow"
        Action   = ["xray:PutSpans", "xray:PutTraceSegments"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "gpu" {
  name = "agent-latency-tracer-gpu-prepare"
  role = aws_iam_role.gpu.name
}

resource "aws_instance" "gpu" {
  ami                         = data.aws_ssm_parameter.dlami.value
  instance_type               = local.instance_type
  subnet_id                   = element(data.aws_subnets.default.ids, 0)
  vpc_security_group_ids      = [aws_security_group.gpu.id]
  iam_instance_profile        = aws_iam_instance_profile.gpu.name
  associate_public_ip_address = true

  # The backstop for a forgotten server: stops and terminates 3 hours after boot.
  instance_initiated_shutdown_behavior = "terminate"
  user_data                            = <<-EOF
    #!/bin/bash
    shutdown -h +180
  EOF

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_size           = 100
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = { Name = "agent-latency-tracer-gpu-prepare" }
}

output "instance_id" {
  value = aws_instance.gpu.id
}
