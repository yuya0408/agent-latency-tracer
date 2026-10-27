# CloudWatch Transaction Search: spans sent to X-Ray are stored in full in the
# aws/spans log group, where they can be searched. AgentCore Observability needs
# this too. It is one setting per account and Region, so it lives apart from
# ../ (which is destroyed after each stage) and stays on: it costs nothing
# until spans are sent.
#
# Destroying this root would not switch it off. Terraform's docs say removing
# the destination or the indexing rule leaves the AWS setting as it is, while
# the log resource policy would be deleted -- spans would still go to
# CloudWatch Logs, with nothing allowing X-Ray to write them. To switch off:
#   aws xray update-trace-segment-destination --destination XRay

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# Lets X-Ray write into the two log groups Transaction Search uses. The
# statement is the one in the CloudWatch docs (Enable transaction search).
resource "aws_cloudwatch_log_resource_policy" "transaction_search" {
  policy_name = "TransactionSearchXRayAccess"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "TransactionSearchXRayAccess"
      Effect    = "Allow"
      Principal = { Service = "xray.amazonaws.com" }
      Action    = "logs:PutLogEvents"
      Resource = [
        "arn:aws:logs:${var.region}:${local.account_id}:log-group:aws/spans:*",
        "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/application-signals/data:*",
      ]
      Condition = {
        ArnLike      = { "aws:SourceArn" = "arn:aws:xray:${var.region}:${local.account_id}:*" }
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

# This is the switch: spans go to CloudWatch Logs instead of X-Ray's own store.
resource "aws_xray_trace_segment_destination" "this" {
  destination = "CloudWatchLogs"

  depends_on = [aws_cloudwatch_log_resource_policy.transaction_search]
}

# Share of spans also turned into trace summaries (for the trace list and
# aggregates). Every span is searchable regardless; 1% is the free share.
resource "aws_xray_indexing_rule" "default" {
  name = "Default"

  rule {
    probabilistic {
      desired_sampling_percentage = 1
    }
  }

  depends_on = [aws_xray_trace_segment_destination.this]
}
