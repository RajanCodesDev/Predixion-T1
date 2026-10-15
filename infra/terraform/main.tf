data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

locals {
  name            = var.project_name
  azs             = slice(data.aws_availability_zones.available.names, 0, 2)
  api_image        = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
  mock_image       = "${aws_ecr_repository.mock.repository_url}:${var.mock_image_tag}"
  metric_namespace = "CollectionsChallenge"
}

resource "aws_vpc" "this" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(aws_vpc.this.cidr_block, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(aws_vpc.this.cidr_block, 8, count.index + 10)
  availability_zone = local.azs[count.index]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? 1 : 0
  domain = "vpc"
  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  count         = var.enable_nat_gateway ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "private" {
  count  = 2
  vpc_id = aws_vpc.this.id
  dynamic "route" {
    for_each = var.enable_nat_gateway ? [1] : []
    content {
      cidr_block     = "0.0.0.0/0"
      nat_gateway_id = aws_nat_gateway.this[0].id
    }
  }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ALB ingress"
  vpc_id      = aws_vpc.this.id
  ingress {
    from_port = var.enable_https ? 443 : 80
    to_port   = var.enable_https ? 443 : 80
    protocol  = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "api" {
  name   = "${local.name}-api"
  vpc_id = aws_vpc.this.id
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "mock" {
  name   = "${local.name}-mock"
  vpc_id = aws_vpc.this.id
  ingress {
    from_port       = 8001
    to_port         = 8001
    protocol        = "tcp"
    security_groups = [aws_security_group.api.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}/api"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_repository" "mock" {
  name                 = "${local.name}/mock"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name}/api"
  retention_in_days = 7
}
resource "aws_cloudwatch_log_group" "mock" {
  name              = "/ecs/${local.name}/mock"
  retention_in_days = 7
}

resource "aws_iam_role" "task_execution" {
  name = "${local.name}-task-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "api_task" {
  name = "${local.name}-api-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy" "api_metrics" {
  name = "${local.name}-put-custom-metrics"
  role = aws_iam_role.api_task.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Action = ["cloudwatch:PutMetricData"],
      Resource = "*",
      Condition = { StringEquals = { "cloudwatch:namespace" = local.metric_namespace } }
    }]
  })
}

resource "aws_dynamodb_table" "dlq" {
  name         = "${local.name}-dlq"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"
  attribute {
    name = "event_id"
    type = "S"
  }
  point_in_time_recovery { enabled = false }
  server_side_encryption { enabled = true }
}

resource "aws_iam_role_policy" "api_dlq" {
  name = "${local.name}-dlq-table-access"
  role = aws_iam_role.api_task.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Scan"],
      Resource = aws_dynamodb_table.dlq.arn
    }]
  })
}

resource "aws_ecs_cluster" "this" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_lb" "this" {
  name               = substr("${local.name}-alb", 0, 32)
  load_balancer_type = "application"
  internal           = false
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]
}

resource "aws_lb_target_group" "api" {
  name        = substr("${local.name}-api-tg", 0, 32)
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.this.id
  target_type = "ip"
  health_check {
    enabled             = true
    path                = "/health"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
  }
}

resource "aws_lb_listener" "http" {
  count             = var.enable_https ? 0 : 1
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "https" {
  count             = var.enable_https ? 1 : 0
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.acm_certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
  lifecycle {
    precondition {
      condition     = var.acm_certificate_arn != null && var.acm_certificate_arn != ""
      error_message = "Set acm_certificate_arn to a validated ACM certificate when enable_https=true."
    }
  }
}

resource "aws_ecs_task_definition" "mock" {
  family                   = "${local.name}-mock"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.mock_cpu
  memory                   = var.mock_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  container_definitions = jsonencode([{
    name      = "mock"
    image     = local.mock_image
    essential = true
    portMappings = [{ containerPort = 8001, protocol = "tcp" }]
    environment = [{ name = "ENABLE_TEST_CONTROLS", value = "false" }]
    logConfiguration = { logDriver = "awslogs", options = {
      awslogs-group = aws_cloudwatch_log_group.mock.name
      awslogs-region = var.aws_region
      awslogs-stream-prefix = "mock"
    } }
    healthCheck = { command = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3)\""], interval = 30, timeout = 5, retries = 3, startPeriod = 15 }
  }])
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name = "${local.name}.local"
  vpc  = aws_vpc.this.id
}

resource "aws_service_discovery_service" "mock" {
  name = "mock"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }
  health_check_custom_config { failure_threshold = 1 }
}

resource "aws_ecs_service" "mock" {
  name            = "mock"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.mock.arn
  desired_count   = var.mock_desired_count
  launch_type     = "FARGATE"
  service_registries { registry_arn = aws_service_discovery_service.mock.arn }
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.mock.id]
    assign_public_ip = false
  }
  depends_on = [aws_iam_role_policy_attachment.execution_managed]
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.api_task.arn
  container_definitions = jsonencode([{
    name      = "api"
    image     = local.api_image
    essential = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment = [
      { name = "MOCK_URL", value = "http://mock.${local.name}.local:8001" },
      { name = "DATABASE_PATH", value = "/tmp/dlq.sqlite3" },
      { name = "DLQ_BACKEND", value = "dynamodb" },
      { name = "DLQ_TABLE_NAME", value = aws_dynamodb_table.dlq.name },
      { name = "ENABLE_TEST_CONTROLS", value = "false" },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "CLOUDWATCH_METRIC_NAMESPACE", value = local.metric_namespace },
      { name = "CLOUDWATCH_METRICS_ENABLED", value = "true" },
      { name = "ECS_CLUSTER_NAME", value = aws_ecs_cluster.this.name },
      { name = "ECS_SERVICE_NAME", value = "api" }
    ]
    logConfiguration = { logDriver = "awslogs", options = {
      awslogs-group = aws_cloudwatch_log_group.api.name
      awslogs-region = var.aws_region
      awslogs-stream-prefix = "api"
    } }
    healthCheck = { command = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)\""], interval = 30, timeout = 5, retries = 3, startPeriod = 20 }
  }])
}

resource "aws_ecs_service" "api" {
  name                              = "api"
  cluster                           = aws_ecs_cluster.this.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = var.api_desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 60
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
  depends_on = [aws_lb_listener.http, aws_lb_listener.https, aws_iam_role_policy_attachment.execution_managed, aws_iam_role_policy.api_metrics, aws_iam_role_policy.api_dlq, aws_ecs_service.mock]
}

resource "aws_appautoscaling_target" "api" {
  max_capacity       = var.api_max_capacity
  min_capacity       = var.api_min_capacity
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "api_inflight" {
  name               = "${local.name}-inflight-target-tracking"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  target_tracking_scaling_policy_configuration {
    target_value       = var.inflight_target_per_task
    scale_in_cooldown  = 60
    scale_out_cooldown = 30
    customized_metric_specification {
      metric_name = "InFlightRequestsPerTask"
      namespace   = local.metric_namespace
      statistic   = "Average"
      dimensions {
        name  = "ClusterName"
        value = aws_ecs_cluster.this.name
      }
      dimensions {
        name  = "ServiceName"
        value = aws_ecs_service.api.name
      }
    }
  }
  depends_on = [aws_ecs_service.api]
}

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = "${local.name}-overview"
  dashboard_body = jsonencode({ widgets = [
    { type = "metric", x = 0, y = 0, width = 12, height = 6, properties = {
      title = "API in-flight requests per task", view = "timeSeries", region = var.aws_region, stat = "Average", period = 60,
      metrics = [[local.metric_namespace, "InFlightRequestsPerTask", "ClusterName", aws_ecs_cluster.this.name, "ServiceName", aws_ecs_service.api.name]]
    } },
    { type = "metric", x = 12, y = 0, width = 12, height = 6, properties = {
      title = "ALB request count", view = "timeSeries", region = var.aws_region, stat = "Sum", period = 60,
      metrics = [["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.this.arn_suffix]]
    } },
    { type = "metric", x = 0, y = 6, width = 12, height = 6, properties = {
      title = "API request rate by status", view = "timeSeries", region = var.aws_region, stat = "Sum", period = 60,
      metrics = [
        [local.metric_namespace, "ApiRequests", "ClusterName", aws_ecs_cluster.this.name, "ServiceName", aws_ecs_service.api.name, "StatusClass", "2xx", { label = "API 2xx requests" }],
        [local.metric_namespace, "ApiRequests", "ClusterName", aws_ecs_cluster.this.name, "ServiceName", aws_ecs_service.api.name, "StatusClass", "5xx", { label = "API 5xx requests" }]
      ]
    } },
    { type = "metric", x = 12, y = 6, width = 12, height = 6, properties = {
      title = "API latency percentiles (ms)", view = "timeSeries", region = var.aws_region, period = 60,
      metrics = [
        [local.metric_namespace, "ApiLatencyMs", "ClusterName", aws_ecs_cluster.this.name, "ServiceName", aws_ecs_service.api.name, { stat = "p90", label = "p90 latency" }],
        [local.metric_namespace, "ApiLatencyMs", "ClusterName", aws_ecs_cluster.this.name, "ServiceName", aws_ecs_service.api.name, { stat = "p99", label = "p99 latency" }]
      ]
    } },
    { type = "metric", x = 0, y = 12, width = 24, height = 6, properties = {
      title = "ECS desired and running tasks (scaling evidence)", view = "timeSeries", region = var.aws_region, stat = "Average", period = 60,
      metrics = [["ECS/ContainerInsights", "DesiredTaskCount", "ClusterName", aws_ecs_cluster.this.name, "ServiceName", aws_ecs_service.api.name], ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", aws_ecs_cluster.this.name, "ServiceName", aws_ecs_service.api.name]]
    } }
  ] })
}
