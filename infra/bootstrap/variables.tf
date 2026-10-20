variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "credit-risk"
}

variable "github_repository" {
  type    = string
  default = "Debashis2007/credit-risk-poc"
}

variable "github_immutable_subject_prefix" {
  type        = string
  default     = "repo:Debashis2007@33207849/credit-risk-poc@1403610949"
  description = "sub_claim_prefix from GET /repos/{owner}/{repo}/actions/oidc/customization/sub."
}

variable "github_environments" {
  type        = list(string)
  default     = ["ml-staging", "ml-prod"]
  description = "GitHub environments allowed to assume the deploy role."
}

variable "github_oidc_provider_arn" {
  type        = string
  default     = ""
  description = "Existing token.actions.githubusercontent.com provider; empty creates one (an account has at most one)."
}

variable "model_package_group" {
  type    = string
  default = "ModelCreditRisk"
}

variable "pipeline_name" {
  type    = string
  default = "credit-risk"
}

variable "endpoint_names" {
  type    = list(string)
  default = ["credit-risk-qa", "credit-risk-prod"]
}

variable "serving_role_arn" {
  type        = string
  description = "Role SageMaker serves the model with (the pipeline execution role in the POC)."
}
