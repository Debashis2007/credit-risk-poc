variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "credit-risk"
}

variable "environment" {
  type        = string
  description = "staging (ALL_AT_ONCE) or prod (CANARY 10%, 15-minute bake)."

  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be staging or prod."
  }
}

variable "model_package_arn" {
  type        = string
  description = "Approved model package in the ModelCreditRisk registry group."

  validation {
    condition     = can(regex("^arn:aws:sagemaker:[a-z0-9-]+:[0-9]{12}:model-package/[A-Za-z0-9-]+/[0-9]+$", var.model_package_arn))
    error_message = "model_package_arn must be a versioned SageMaker model package ARN."
  }
}

variable "endpoint_name" {
  type        = string
  description = "Stable endpoint name, kept across releases."
}

variable "release_id" {
  type        = string
  description = "Names this release's model and endpoint configuration, e.g. v4 for package version 4."

  validation {
    condition     = can(regex("^[a-zA-Z0-9-]{1,24}$", var.release_id))
    error_message = "release_id must be 1-24 letters, digits or hyphens."
  }
}

variable "execution_role_arn" {
  type        = string
  description = "Role SageMaker assumes to serve the model (the pipeline execution role in the POC)."
}

variable "instance_type" {
  type    = string
  default = "ml.t2.medium"
}

variable "instance_count" {
  type        = number
  default     = 1
  description = "Initial (and, with autoscaling, minimum) instance count. Prod needs at least 2."
}

variable "max_instance_count" {
  type    = number
  default = 4
}

variable "enable_autoscaling" {
  type    = bool
  default = false
}

variable "target_invocations_per_instance" {
  type    = number
  default = 70
}

variable "canary_percent" {
  type    = number
  default = 10
}

variable "canary_wait_seconds" {
  type    = number
  default = 900
}

variable "termination_wait_seconds" {
  type        = number
  default     = 300
  description = "How long the previous (blue) fleet is kept after traffic moves."
}

variable "maximum_execution_timeout_seconds" {
  type    = number
  default = 3600
}

variable "rollback_5xx_threshold" {
  type    = number
  default = 5
}

variable "rollback_latency_p99_ms" {
  type    = number
  default = 2000
}

variable "alarm_actions" {
  type    = list(string)
  default = []
}

variable "data_capture_s3_uri" {
  type        = string
  default     = ""
  description = "s3:// prefix for request and response capture; empty disables capture."
}

variable "data_capture_percentage" {
  type    = number
  default = 100
}
