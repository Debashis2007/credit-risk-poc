variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "credit-risk"
}

variable "model_package_group" {
  type    = string
  default = "ModelCreditRisk"
}

variable "github_owner" {
  type    = string
  default = "Debashis2007"
}

variable "github_repo" {
  type    = string
  default = "credit-risk-poc"
}

variable "github_token_secret_name" {
  type    = string
  default = "credit-risk/github-deploy-token"
}

variable "lambda_source_dir" {
  type        = string
  default     = "../../.platform/lambdas/trigger_github_deploy"
  description = "The platform's trigger_github_deploy Lambda (ML-Platform checked out at .platform)."
}
