output "api_url" {
  description = "Public API endpoint. HTTP by default; enable HTTPS with a validated ACM certificate to use HTTPS."
  value       = var.enable_https ? "https://${aws_lb.this.dns_name}" : "http://${aws_lb.this.dns_name}"
}

output "alb_dns_name" { value = aws_lb.this.dns_name }
output "ecr_api_repository_url" { value = aws_ecr_repository.api.repository_url }
output "ecr_mock_repository_url" { value = aws_ecr_repository.mock.repository_url }
output "ecs_cluster_name" { value = aws_ecs_cluster.this.name }
output "cloudwatch_dashboard_name" { value = aws_cloudwatch_dashboard.this.dashboard_name }
output "aws_account_id" { value = data.aws_caller_identity.current.account_id }
