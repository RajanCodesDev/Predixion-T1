# Track 2 — Production-Style Deployment on AWS ECS Fargate

## Overview

Track 2 extends the Collections Agent from Track 1 with AWS infrastructure, container orchestration, custom-metric autoscaling, observability, and a repeatable load-testing and teardown workflow.

The goal is to demonstrate how an existing agent service can be deployed and operated using production-oriented cloud infrastructure while keeping application changes minimal.

**Current implementation status:** Terraform configuration has passed `terraform validate`. A successful validation confirms configuration consistency, not successful provisioning or a working end-to-end deployment. AWS deployment, load testing, scaling behavior, and teardown must be demonstrated and recorded separately.

## Architecture

```text
                         Load Generator
                               |
                               v
                       Application Load Balancer
                               |
                      Public Subnets (AZs)
                               |
                               v
                     ECS Fargate API Service
                     Private Subnets (AZs)
                         |           |
                         |           +----> Mock Inference Service
                         |                  (Service Discovery)
                         |
                         +----> DynamoDB DLQ
                         |
                         +----> CloudWatch Logs
                         |
                         +----> Custom In-Flight Metric
                                      |
                                      v
                           CloudWatch / Auto Scaling
                                      |
                                      v
                           ECS Service Desired Count

Private task egress --> NAT Gateway --> External AWS APIs
```

The diagram describes the intended architecture. Verify each connection against the Terraform configuration and deployed AWS resources before treating it as a live architecture.

## Infrastructure

Terraform configuration is located in `infra/terraform/`.

| Component | Purpose |
|---|---|
| VPC and subnets | Public subnets host the ALB and (in the budget plan) the tasks; private subnets exist for the NAT-based variant |
| Internet gateway | Provides internet connectivity for public subnets |
| NAT gateway | Optional. Disabled by default in the budget plan, where tasks run in public subnets with a public IP and reach ECR/CloudWatch/DynamoDB over the internet gateway |
| DynamoDB gateway VPC endpoint | Free endpoint that keeps DLQ traffic on the AWS network (no NAT required) |
| Security groups | Restrict network traffic between components |
| Application Load Balancer | Provides an HTTP/HTTPS entry point and distributes requests |
| Amazon ECS / Fargate | Runs containerized API and mock-inference services |
| Amazon ECR | Stores container images |
| IAM roles and policies | Grant tasks the permissions required for execution, metrics, and DLQ access |
| AWS Cloud Map | Provides private service discovery for the mock service |
| Amazon DynamoDB | Stores dead-letter records |
| Amazon CloudWatch | Collects logs and exposes the monitoring dashboard |
| Application Auto Scaling | Adjusts API service capacity using the configured in-flight metric |

### Security model

The intended deployment uses private subnets for application tasks and a public-facing ALB. Security groups should allow application traffic only from the ALB and mock-service traffic only from authorized callers.

IAM policies should follow least privilege. Do not place AWS credentials in container images, source code, or committed Terraform variable files.

HTTPS is conditional on supplying a valid ACM certificate ARN and enabling the corresponding Terraform option.

## Custom-Metric Autoscaling

The API service is configured with an Application Auto Scaling policy based on an in-flight request metric.

The intended control loop is:

1. Requests enter through the ALB.
2. The API processes requests and tracks in-flight work.
3. The configured metric is published to CloudWatch.
4. Application Auto Scaling evaluates the metric against its target.
5. ECS adjusts the API service's desired task count within the configured minimum and maximum capacity.

Before the demo, verify that the metric publisher emits the exact namespace, metric name, and dimensions expected by the scaling policy. Confirm that the metric is visible in CloudWatch and that the ECS service scales in response to load.

Metric semantics and limitations:

- `InFlightRequestsPerTask` is a per-task gauge of requests currently being served, not a cumulative count or a time-window rate. Each task publishes only its own value with the `ClusterName`/`ServiceName` dimensions; target tracking averages those datapoints to obtain average concurrent requests per task.
- The gauge is republished (as `0` when idle) on every publish interval, so target tracking can scale in when traffic stops and stale values age out within one evaluation period.
- Scaling therefore depends on the application successfully publishing metrics; a missing metric cannot scale the service. Publication is best-effort, never blocks or fails a request, and retains buffered counts/latency until a flush succeeds. See `infra/README.md` for the full set of limitations.
- If metrics are enabled, both `ECS_CLUSTER_NAME` and `ECS_SERVICE_NAME` are required; the API fails fast at startup when they are missing so it cannot publish a gauge the scaling policy is unable to match.
- `/health` is excluded from all metrics so ALB and ECS health probes never look like application load.

### Demonstrating scale-out

`InFlightRequestsPerTask` measures **concurrency**, so the target must be met by *concurrent in-flight requests*, not by request rate alone. With the measured mean service time (W ≈ 0.43 s), a target of 5 is ~11.6 requests/second (~700/minute) *per task*. The documented 50 calls/minute profile peaks at only ~0.36 in-flight per task, so it will not (and should not) scale out — a single task is genuinely sufficient. Do not lower the target to force scaling: a sub-1 target on an integer gauge oscillates and is misleading.

To demonstrate scaling, drive sustained concurrency above the target and watch the desired count:

```bash
# ~30 concurrent in-flight requests for several minutes -> desired climbs toward 5
BASE_URL="$API_URL" CONCURRENCY=30 TOTAL_EVENTS=30000 python scripts/load_test.py
```

Expected: target tracking adds tasks until average in-flight ≈ 5 (clamped to `api_max_capacity`). When the load stops, the gauge republishes `0`, and after the 60 s scale-in cooldown plus the conservative target-tracking evaluation the service returns to `api_min_capacity`. Capture evidence from the dashboard's "API desired capacity" widget (Application Auto Scaling `DesiredCapacity`), plus `aws application-autoscaling describe-scaling-activities` for the audit trail.

## Dead-Letter Queue

Failed events are persisted so they can be inspected (`GET /dlq`, `GET /dlq/{id}`) and replayed (`POST /dlq/{id}/replay`) with an atomic, exclusive claim.

Two interchangeable backends are selected by `DLQ_BACKEND`:

- `sqlite` (default) — one file per task at `DATABASE_PATH`. Intended for local development, `docker compose` and tests; it needs no AWS access.
- `dynamodb` — a single shared table named by `DLQ_TABLE_NAME`. **This is what the ECS API task uses** so dead letters survive task replacement and are visible to every task behind the load balancer. The SQLite file is per-task and ephemeral on Fargate, which would make the `/dlq` endpoints non-deterministic once the service scales past one task.

When `DLQ_BACKEND=dynamodb`, `DLQ_TABLE_NAME` is required and the task fails fast without it. The API calls the (synchronous) DLQ through `asyncio.to_thread` so DynamoDB latency never blocks the event loop.

## Failure Simulation

The mock-inference service can be used to exercise application resilience and error-handling behavior without running a local large language model.

For a failure-injection demo, configure the mock service to simulate a 20% inference failure rate, if supported by the deployed application and mock configuration.

Record:
- The configured failure rate and how it was applied.
- Request success and failure counts.
- Retry and circuit-breaker behavior.
- Dead-letter queue activity, if applicable.
- Service health and scaling behavior during the test.

A simulated inference service is a testing substitute, not a live open-weight GPU inference deployment. If the challenge requires live GPU-backed inference, that requirement remains outstanding until an actual model-serving endpoint is integrated and demonstrated.

## Observability

Use the CloudWatch dashboard and service logs to inspect:

- API request behavior and errors.
- In-flight request metrics.
- ECS service task count and scaling activity.
- Container logs.
- Mock-inference failures.
- Dead-letter queue behavior.

For the final demonstration, capture dashboard screenshots or exported evidence showing the system before, during, and after load.

## Load-Test Plan

Use the existing load-testing scripts where compatible with the deployed API.

The challenge traffic profile is:

| Period | Traffic |
|---|---|
| 00:00–08:00 | 10 calls/minute |
| 08:00–10:00 | Ramp toward 50 calls/minute over 15 minutes, then hold |
| 10:00–14:00 | 10 calls/minute |
| 14:00–16:00 | Ramp and hold at the specified peak |
| 16:00–24:00 | 10 calls/minute |

**Important:** Confirm the exact ramp and hold timing from the challenge specification before executing the profile. If the demo compresses the schedule, document the compression factor and preserve the relative traffic phases.

This profile is a *rate* profile and, as noted above, intentionally stays within one task. To exercise autoscaling in the same run, add a bounded concurrency burst (for example `CONCURRENCY=30` with a large `TOTAL_EVENTS`) during the peak window, then stop it and observe scale-in.

Measure request volume, latency, errors, task count, scaling response, and approximate infrastructure cost.

## Deployment Workflow

### Prerequisites

- An AWS account with billing configured.
- AWS CLI installed and authenticated.
- Terraform installed.
- Docker installed and running.
- Appropriate IAM permissions for ECR, ECS, networking, IAM, CloudWatch, DynamoDB, and Application Auto Scaling.
- A valid ACM certificate if HTTPS is enabled.
- Reviewed Terraform variables and image build configuration.

### 1. Configure variables

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` to suit the environment. Review region, image tags, task counts, capacity limits, NAT configuration, HTTPS settings, and certificate ARN.

Never commit secrets or account credentials.

### 2. Validate Terraform

```bash
terraform fmt -check -recursive
terraform init
terraform validate
terraform plan
```

Review the plan before making changes. Confirm that resource counts, IAM permissions, networking, and estimated costs are acceptable.

### 3. Build and publish images

Review `infra/scripts/build-and-push.sh` and ensure the image build contexts and Dockerfiles match the actual repository. Then execute the script according to its documented usage.

Confirm that both required images exist in ECR before provisioning ECS services that depend on them.

### 4. Deploy

Review `infra/scripts/deploy.sh` before running it. Confirm that it uses the intended Terraform directory, variables, image tags, and deployment order.

After deployment, verify:
- ECS tasks reach a healthy running state.
- The ALB target group reports healthy targets.
- API health endpoints respond.
- The API can reach the mock-inference service.
- CloudWatch logs and metrics are available.

### 5. Run tests and load

Run the existing tests locally first. Review the load-test script's arguments and rate controls before targeting AWS.

Capture evidence for baseline behavior, failure injection, peak traffic, autoscaling, recovery, and teardown.

## Cost Management

NAT gateways and load balancers incur charges even when request volume is low. ECS tasks, public IPv4 addresses, logs, and metrics also contribute.

Two configurations are documented in `COST_WRITEUP.md`:

- **Plan A (private-subnet + NAT):** ALB, NAT gateway, and one API + one mock task on x86. Roughly **$0.13/hour** at `ap-south-1` list prices.
- **Plan B (budget plan, the default here):** tasks in public subnets with a public IP (no NAT), ARM64 Fargate at 0.25 vCPU / 0.5 GiB, and Container Insights off. Roughly **$0.06/hour** — about half the hourly burn.

To control spending:

- The defaults are already the budget plan; keep `enable_nat_gateway=false`, `cpu_architecture="ARM64"` and `enable_container_insights=false` unless you specifically need the other variant.
- Keep minimum and desired task counts low when idle.
- Run the demonstration only for the required period.
- Tear down promptly — the hourly charges accrue whether or not requests are served.
- Review CloudWatch log retention and metric counts.

The challenge's target budget is approximately $10–12. At the Plan B burn rate that is roughly 170–200 hours of continuous running; at the Plan A rate it is roughly 75–90 hours. Actual cost depends on region, resource lifetime, traffic, and configuration — confirm current regional pricing and monitor actual spend rather than assuming the target is guaranteed.

## Teardown

After collecting the required evidence, inspect and run the reviewed teardown script:

```bash
# From the repository root
sed -n '1,240p' infra/scripts/destroy.sh
```

Run it only after confirming that the selected Terraform workspace and variables refer to the demo environment:

```bash
./infra/scripts/destroy.sh
```

Verify in the AWS console that the created ECS services, tasks, ALB, NAT gateway, and other billable resources have been removed. Check for retained ECR images, log groups, public IP allocations, or other resources not covered by the teardown.

Do not delete shared resources that existed before the demo.

## Validation and Evidence Checklist

- [ ] Terraform formatting and validation pass.
- [ ] Terraform plan reviewed.
- [ ] Container images built and pushed to ECR.
- [ ] ECS services start and pass health checks.
- [ ] ALB routes requests successfully.
- [ ] Private API-to-mock communication works.
- [ ] In-flight metric appears in CloudWatch.
- [ ] Autoscaling responds to load.
- [ ] Failure injection and resilience behavior recorded.
- [ ] Load profile and results documented.
- [ ] Cost estimate and actual spend recorded.
- [ ] Teardown completed and residual resources checked.
- [ ] Live GPU/open-weight inference demonstrated if required by the challenge.

## Repository Layout

```text
collections-agent-track1/
├── app/                         # Track 1 application
├── mock/                        # Existing mock service, if present
├── tests/                       # Existing application tests
├── scripts/                     # Existing application/load scripts
├── infra/
│   ├── README.md
│   ├── scripts/
│   │   ├── build-and-push.sh
│   │   ├── deploy.sh
│   │   └── destroy.sh
│   └── terraform/
│       ├── main.tf
│       ├── variables.tf
│       ├── outputs.tf
│       ├── versions.tf
│       └── terraform.tfvars.example
└── README_TRACK2.md
```

## Final Status

This document describes the intended Track 2 deployment and validation procedure. Record actual deployment results, screenshots, metrics, load-test outputs, cost, and teardown evidence before marking the corresponding checklist items complete.