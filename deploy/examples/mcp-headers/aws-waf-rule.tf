# AWS WAFv2: block `tools/call slow_report` sent with the 2026-07-28 standard headers, at the ALB.
# A regional rule group with one rule (both headers must match exactly); reference it from your web ACL.
# EXAMPLE, not applied to a live account. It does not stop a 2025-06-18 or gRPC caller: see the README.
resource "aws_wafv2_rule_group" "block_slow_report" {
  name     = "ramen-block-slow-report"
  scope    = "REGIONAL"
  capacity = 50 # a reservation; it must be at least what the rule costs

  rule {
    name     = "mcp-name-slow-report"
    priority = 1
    action {
      block {}
    }
    statement {
      and_statement {
        statement {
          byte_match_statement {
            search_string         = "tools/call"
            positional_constraint = "EXACTLY"
            field_to_match {
              single_header { name = "mcp-method" }
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            search_string         = "slow_report"
            positional_constraint = "EXACTLY"
            field_to_match {
              single_header { name = "mcp-name" }
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "ramen-block-slow-report"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "ramen-block-slow-report-group"
    sampled_requests_enabled   = true
  }
}
