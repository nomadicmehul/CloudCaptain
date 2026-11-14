output "role_arns" {
  description = "Role ARNs by access level: deploy and readonly."
  value       = { for key, role in aws_iam_role.this : key => role.arn }
}

output "boundary_arn" {
  description = "Permissions boundary attached to every role."
  value       = aws_iam_policy.boundary.arn
}

output "guardrail_arn" {
  description = "Explicit-deny guardrail policy attached to every role."
  value       = aws_iam_policy.guardrail.arn
}
