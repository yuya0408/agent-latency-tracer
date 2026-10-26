# A small EC2 instance that only relays Session Manager port forwarding from
# the local machine to RDS. No SSH, no public IP, no inbound rules: the SSM
# Agent on it connects out to Systems Manager through the interface endpoints
# in ssm_endpoints.tf.

# Latest Amazon Linux 2023 for ARM (Graviton). The SSM Agent comes preinstalled.
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_iam_role" "bastion" {
  name = "agent-latency-tracer-bastion"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# What the SSM Agent needs to register the instance and accept sessions.
resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  role       = aws_iam_role.bastion.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "bastion" {
  name = "agent-latency-tracer-bastion"
  role = aws_iam_role.bastion.name
}

resource "aws_security_group" "bastion" {
  name   = "agent-latency-tracer-bastion"
  vpc_id = aws_vpc.main.id
}

# No ingress rules. Outbound: HTTPS to the SSM endpoints, PostgreSQL to RDS.
resource "aws_vpc_security_group_egress_rule" "bastion_https" {
  security_group_id = aws_security_group.bastion.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "bastion_postgres" {
  security_group_id = aws_security_group.bastion.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
}

resource "aws_instance" "bastion" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = "t4g.nano"
  subnet_id              = aws_subnet.private[0].id
  vpc_security_group_ids = [aws_security_group.bastion.id]
  iam_instance_profile   = aws_iam_instance_profile.bastion.name

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  tags = { Name = "agent-latency-tracer-bastion" }
}
