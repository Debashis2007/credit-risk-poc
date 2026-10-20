output "github_token_secret_arn" {
  value       = aws_secretsmanager_secret.github_token.arn
  description = "Put the GitHub token here: aws secretsmanager put-secret-value --secret-id <arn> --secret-string <token>"
}

output "event_rule_name" {
  value = aws_cloudwatch_event_rule.approved.name
}

output "lambda_function_name" {
  value = aws_lambda_function.trigger.function_name
}
