variable "aws_region" {
  description = "Challenge-required AWS region."
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  type        = string
  default     = "collections-challenge"
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
  description = "NAT has an hourly charge. Keep true for private-subnet egress; disable only for a deliberately offline test."
  type        = bool
  default     = true
}

variable "enable_https" {
  description = "Optional HTTPS listener. Requires a certificate ARN already validated in ACM in this region. Defaults to HTTP to avoid manual certificate/domain prerequisites."
  type        = bool
  default     = false
}

variable "acm_certificate_arn" {
  type      = string
  default   = null
  nullable  = true
}

variable "api_cpu" {
  type    = number
  default = 512
}

variable "api_memory" {
  type    = number
  default = 1024
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
