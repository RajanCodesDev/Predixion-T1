variable "aws_region" {
  description = "Challenge-required AWS region."
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  type    = string
  default = "collections-challenge"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.project_name))
    error_message = "Use 3-25 lowercase letters, digits, or hyphens; start with a letter."
  }
}

variable "environment" {
  type    = string
  default = "track2"
}

variable "api_image_tag" {
  description = "Tag pushed to the API ECR repository before terraform apply."
  type        = string
  default     = "track2"
}

variable "mock_image_tag" {
  description = "Tag pushed to the mock ECR repository before terraform apply."
  type        = string
  default     = "track2"
}

variable "api_desired_count" {
  type    = number
  default = 1
}

variable "api_min_capacity" {
  type    = number
  default = 1
}

variable "api_max_capacity" {
  type    = number
  default = 5
}

variable "mock_desired_count" {
  type    = number
  default = 1
}

variable "enable_nat_gateway" {
  description = "NAT gateway for private-subnet egress. Defaults off (budget plan): tasks run in public subnets with assign_public_ip so they reach ECR/CloudWatch/DynamoDB over the internet gateway, and the NAT gateway's ~$41/month hourly charge is avoided. Set true only if you also intend to run tasks in private subnets."
  type        = bool
  default     = false
}

variable "enable_https" {
  description = "Optional HTTPS listener. Requires a certificate ARN already validated in ACM in this region. Defaults to HTTP to avoid manual certificate/domain prerequisites."
  type        = bool
  default     = false
}

variable "acm_certificate_arn" {
  type     = string
  default  = null
  nullable = true
}

variable "api_cpu" {
  description = "Fargate CPU units for the API task (256 = 0.25 vCPU, the Fargate minimum)."
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "Fargate memory (MiB) for the API task."
  type        = number
  default     = 512
}

variable "mock_cpu" {
  type    = number
  default = 256
}

variable "mock_memory" {
  type    = number
  default = 512
}

variable "inflight_target_per_task" {
  description = "Target average in-flight requests per API task for custom-metric target tracking."
  type        = number
  default     = 5
}

variable "cpu_architecture" {
  description = "Fargate CPU architecture. ARM64 (Graviton) is materially cheaper than X86_64; the Docker images must be built for the same platform (see infra/scripts/build-and-push.sh)."
  type        = string
  default     = "ARM64"
  validation {
    condition     = contains(["ARM64", "X86_64"], var.cpu_architecture)
    error_message = "cpu_architecture must be ARM64 or X86_64."
  }
}

variable "enable_container_insights" {
  description = "ECS Container Insights publishes billable CloudWatch custom metrics. Off for the budget demo; the app's own target-tracking metric and the Application Auto Scaling DesiredCapacity metric still provide scaling evidence."
  type        = bool
  default     = false
}
