output "endpoint_name" {
  value = aws_sagemaker_endpoint.this.name
}

output "endpoint_config_name" {
  value = aws_sagemaker_endpoint_configuration.this.name
}

output "model_name" {
  value = aws_sagemaker_model.this.name
}

output "model_package_arn" {
  value = var.model_package_arn
}
