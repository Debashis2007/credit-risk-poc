# Starts the model-deploy workflow when a credit-risk model package is approved.
#
# SageMaker emits "SageMaker Model Package State Change" when a package's approval status
# changes; this EventBridge rule matches Approved packages in the model's registry group and
# invokes the platform's trigger_github_deploy Lambda, which sends a GitHub repository_dispatch
# (event type model-approved) to this repo. The GitHub token lives in Secrets Manager.
# Applied once, by hand, in the POC account; state stays with whoever applies it.

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
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

data "archive_file" "trigger" {
  type        = "zip"
  source_file = "${var.lambda_source_dir}/handler.py"
  output_path = "${path.module}/.build/trigger_github_deploy.zip"
}

resource "aws_secretsmanager_secret" "github_token" {
  name        = var.github_token_secret_name
  description = "Fine-grained GitHub token with Contents: write on ${var.github_owner}/${var.github_repo}, used to send repository_dispatch."
}

resource "aws_iam_role" "trigger" {
  name = "${var.project}-deploy-trigger"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "trigger" {
  name = "deploy-trigger"
  role = aws_iam_role.trigger.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.github_token.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.trigger.arn}:*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "trigger" {
  name              = "/aws/lambda/${var.project}-deploy-trigger"
  retention_in_days = 30
}

resource "aws_lambda_function" "trigger" {
  function_name    = "${var.project}-deploy-trigger"
  role             = aws_iam_role.trigger.arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.trigger.output_path
  source_code_hash = data.archive_file.trigger.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      GITHUB_OWNER            = var.github_owner
      GITHUB_REPO             = var.github_repo
      GITHUB_TOKEN_SECRET_ARN = aws_secretsmanager_secret.github_token.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.trigger, aws_iam_role_policy.trigger]
}

resource "aws_cloudwatch_event_rule" "approved" {
  name        = "${var.project}-model-approved"
  description = "Approved packages in ${var.model_package_group} start the model-deploy workflow"
  event_pattern = jsonencode({
    source        = ["aws.sagemaker"]
    "detail-type" = ["SageMaker Model Package State Change"]
    detail = {
      ModelPackageGroupName = [var.model_package_group]
      ModelApprovalStatus   = ["Approved"]
    }
  })
}

resource "aws_cloudwatch_event_target" "trigger" {
  rule = aws_cloudwatch_event_rule.approved.name
  arn  = aws_lambda_function.trigger.arn

  retry_policy {
    maximum_retry_attempts       = 4
    maximum_event_age_in_seconds = 3600
  }
}

resource "aws_lambda_permission" "events" {
  statement_id   = "AllowEventBridgeModelApproved"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.trigger.function_name
  principal      = "events.amazonaws.com"
  source_arn     = aws_cloudwatch_event_rule.approved.arn
  source_account = data.aws_caller_identity.current.account_id
}
