# Cloud Armor: answer 403 at the Google edge to `tools/call slow_report` sent with the 2026-07-28 standard headers.
# One rule for a security policy you name; Cloud Armor reads header names in lower case.
# EXAMPLE, not applied to a live project. It does not stop a 2025-06-18 or gRPC caller: see the README.
variable "security_policy" {
  description = "Name of the Cloud Armor policy attached to the group's backend services"
  type        = string
}

resource "google_compute_security_policy_rule" "block_slow_report" {
  security_policy = var.security_policy
  priority        = 900
  action          = "deny(403)"
  description     = "block tools/call slow_report by MCP standard header"
  match {
    expr {
      expression = "request.headers['mcp-method'] == 'tools/call' && request.headers['mcp-name'] == 'slow_report'"
    }
  }
}
