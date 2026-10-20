output "deploy_role_arn" {
  value       = aws_iam_role.deploy.arn
  description = "GitHub secret MLP_GHA_DEPLOY_ROLE_ARN in the ml-staging and ml-prod environments."
}

output "state_bucket" {
  value       = aws_s3_bucket.state.bucket
  description = "GitHub variable MLP_TF_STATE_BUCKET."
}
