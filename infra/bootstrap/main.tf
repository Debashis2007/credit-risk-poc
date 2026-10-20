# One-time AWS setup for the model-deploy workflow in the POC account, applied by hand:
#   - GitHub Actions OIDC provider (unless one already exists)
#   - S3 bucket for the endpoint Terraform state
#   - deploy role, assumable only from this repo's ml-staging and ml-prod environments,
#     allowed only what infra/endpoint and scripts/endpoint_check.py need
# Its own state stays local with whoever applies it.

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project    = var.project
      managed_by = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account  = data.aws_caller_identity.current.account_id
  region   = var.aws_region
  sm       = "arn:aws:sagemaker:${local.region}:${local.account}"
  oidc_arn = var.github_oidc_provider_arn != "" ? var.github_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn
  # GitHub sends repo:<owner>@<id>/<repo>@<id>:environment:<env> when the repo uses immutable
  # subject claims, and repo:<owner>/<repo>:environment:<env> otherwise; trust both.
  subjects = flatten([for env in var.github_environments : [
    "repo:${var.github_repository}:environment:${env}",
    "${var.github_immutable_subject_prefix}:environment:${env}",
  ]])
  endpoint_arns = [for e in var.endpoint_names : "${local.sm}:endpoint/${e}"]
  state_bucket  = "${var.project}-tfstate-${local.account}"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.github_oidc_provider_arn == "" ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.state]
}

resource "aws_iam_role" "deploy" {
  name = "${var.project}-gha-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = local.subjects }
      }
    }]
  })
  max_session_duration = 7200
}

resource "aws_iam_role_policy" "deploy" {
  name = "endpoint-release"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadApprovedPackagesAndPipeline"
        Effect = "Allow"
        Action = ["sagemaker:DescribeModelPackage", "sagemaker:ListModelPackages", "sagemaker:DescribePipeline"]
        Resource = [
          "${local.sm}:model-package/${lower(var.model_package_group)}/*",
          "${local.sm}:model-package/${var.model_package_group}/*",
          "${local.sm}:model-package-group/${lower(var.model_package_group)}",
          "${local.sm}:pipeline/${var.pipeline_name}",
        ]
      },
      {
        Sid    = "ReleaseModelsConfigsAndEndpoints"
        Effect = "Allow"
        Action = [
          "sagemaker:CreateModel", "sagemaker:DescribeModel", "sagemaker:DeleteModel",
          "sagemaker:CreateEndpointConfig", "sagemaker:DescribeEndpointConfig", "sagemaker:DeleteEndpointConfig",
          "sagemaker:CreateEndpoint", "sagemaker:UpdateEndpoint", "sagemaker:DescribeEndpoint",
          "sagemaker:AddTags", "sagemaker:ListTags", "sagemaker:InvokeEndpoint",
        ]
        Resource = concat(
          local.endpoint_arns,
          [for e in var.endpoint_names : "${local.sm}:model/${e}-*"],
          [for e in var.endpoint_names : "${local.sm}:endpoint-config/${e}-*"],
        )
      },
      {
        Sid       = "PassTheServingRole"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = var.serving_role_arn
        Condition = { StringEquals = { "iam:PassedToService" = "sagemaker.amazonaws.com" } }
      },
      {
        Sid      = "RollbackAlarms"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:TagResource", "cloudwatch:ListTagsForResource"]
        Resource = [for e in var.endpoint_names : "arn:aws:cloudwatch:${local.region}:${local.account}:alarm:${e}-rollback-*"]
      },
      {
        Sid      = "DescribeAlarms"
        Effect   = "Allow"
        Action   = "cloudwatch:DescribeAlarms"
        Resource = "*"
      },
      {
        Sid      = "StateBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.state.arn
      },
      {
        Sid      = "StateObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.state.arn}/${var.project}/*"
      },
    ]
  })
}
