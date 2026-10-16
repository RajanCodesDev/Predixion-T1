# Track 2 AWS infrastructure (mock inference mode)

This Terraform stack creates real AWS infrastructure in `ap-south-1` while keeping inference simulated. The mock dependency preserves the challenge's independent 20% per-request HTTP 503 injection. **It does not run an open-weight model on GPU capacity**, so this is a cost-conscious infrastructure rehearsal, not a claim that the strict GPU-inference portion of Track 2 is complete.

## What Terraform provisions

- VPC with two public and two private subnets across two available AZs.
- Internet gateway and public routing; the NAT gateway is **off by default** (see the budget plan below) and the private route tables are only used if it is enabled.
- A free DynamoDB gateway VPC endpoint so DLQ traffic stays on the AWS network without NAT.
- Public ALB and API target group with `/health` checks.
- ECS Fargate API and mock services, running in the **public subnets with a public IP** by default so they reach ECR/CloudWatch/DynamoDB through the internet gateway.
- Cloud Map private DNS so the API resolves `mock.collections-challenge.local`.
- ECR repositories for both images.
- CloudWatch log groups, a dashboard, and custom-metric target tracking for average in-flight requests per API task. Container Insights is **off by default** (billable custom metrics); scaling evidence comes from the app metric and the Application Auto Scaling `DesiredCapacity` metric.
- DynamoDB-backed DLQ for records shared across API tasks.
- Task execution role and scoped task permissions for CloudWatch custom metrics and the DLQ table.
- ECS deployment circuit breaker and rollback.

## Budget plan (defaults)

The defaults minimise cost while keeping the full challenge architecture:

| Setting | Value | Why |
|---|---|---|
| `enable_nat_gateway` | `false` | Saves the NAT gateway hourly charge (~$41/month). Tasks use public subnets + public IPs. |
| `cpu_architecture` | `ARM64` | Graviton Fargate is materially cheaper than x86. |
| `api_cpu` / `api_memory` | `256` / `512` | Fargate minimum; the API is I/O-bound. |
| `enable_container_insights` | `false` | Avoids billable Container Insights metrics. |

Consequences to be aware of: tasks receive a public IP (their security groups still allow inbound only from the ALB / API security groups, never `0.0.0.0/0`, so they are not reachable from the internet), and the Docker images **must** be built for the same platform (`linux/arm64`). `infra/scripts/build-and-push.sh` defaults `PLATFORM=linux/arm64`; set `PLATFORM=linux/amd64` and `cpu_architecture="X86_64"` to use x86. The NAT-based private-subnet variant is still available by setting `enable_nat_gateway=true` and moving the ECS services back to `aws_subnet.private[*].id`.

See `../COST_WRITEUP.md` for both plans and their assumptions.

HTTPS is optional because a fresh account without a domain cannot automatically validate a public ACM certificate. The default demo endpoint uses HTTP. For a public production endpoint, configure a domain and validated ACM certificate, then set `enable_https=true` and `acm_certificate_arn`.

## Prerequisites

- An AWS account with billing enabled.
- AWS CLI v2, Terraform >= 1.7, Docker, and this repository.
- Credentials for an IAM principal authorized to create the listed resources. Prefer IAM Identity Center/short-lived credentials. Terraform cannot create the AWS account or bootstrap its first trusted identity without already-authenticated credentials; this is the unavoidable bootstrap prerequisite.
- Region quota and service quotas sufficient for Fargate, ALB, NAT, and ECS resources. This mock-only mode does not request GPU instances.

Configure an existing IAM Identity Center profile, for example:

```bash
aws configure sso
aws sso login --profile your-profile
export AWS_PROFILE=your-profile
export AWS_REGION=ap-south-1
aws sts get-caller-identity
```

Do not create or commit access keys in the repository. Use a role/profile with enough permissions to provision and destroy the resources in this stack.

## Deploy

The ECR repositories must exist before the Docker images can be pushed. The deployment script therefore applies only the two ECR repositories first, builds/pushes the images, shows a full plan, and asks for explicit confirmation before creating the rest of the infrastructure.

```bash
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
./infra/scripts/deploy.sh
```

Review every plan. This creates billable resources, particularly the ALB (and, if you enable it, the NAT gateway). The script requires typing `APPLY` before the full deployment.

After deployment, Terraform prints the public API URL and dashboard name. Test:

```bash
API_URL="$(cd infra/terraform && terraform output -raw api_url)"
curl -fsS "$API_URL/health"
curl -fsS "$API_URL/docs" | head
```

Use the dashboard in AWS CloudWatch: `collections-challenge-overview` in `ap-south-1` (or the configured project name plus `-overview`). Logs are under `/ecs/collections-challenge/api` and `/ecs/collections-challenge/mock`.

## Failure behavior

The mock service independently returns HTTP 503 with `{ "status": "error", "reason": "downstream_timeout" }` for approximately 20% of its requests. The API's retries mean client-visible failure rate may be lower than 20%; the injection rate is measured at the dependency boundary. Test controls remain disabled in AWS.

The API publishes these CloudWatch metrics in namespace `CollectionsChallenge`:

- `InFlightRequestsPerTask` — a gauge, sample of the requests *currently* being served by one task, dimensioned by `ClusterName` and `ServiceName`. Target tracking reads the `Average` statistic, so with all tasks publishing their own value the alarm sees average concurrent requests per task.
- `ApiRequests` — request-count delta per publish interval, dimensioned by `ClusterName`, `ServiceName` and `StatusClass` (`2xx`/`4xx`/`5xx`). The dashboard plots it with `Sum`.
- `ApiLatencyMs` — one datapoint per sampled request in milliseconds, so the dashboard can plot p90/p99.

Implementation details:

- Publishing is in-memory first: request middleware only increments/decrements a counter and records status class plus monotonic latency. A single lifecycle-managed asyncio task flushes every `CLOUDWATCH_METRICS_INTERVAL` seconds (default 10) in batches of at most 100 datums; boto3's blocking call runs in a worker thread. No request ever waits on CloudWatch.
- Each task publishes only its own in-flight count — never a fleet-wide number — so the per-task average is not multiplied by the task count.
- The idle gauge is still published as `0` every interval, which keeps the alarm fresh and lets the service scale in when traffic stops. If a task dies, its datapoints age out of the alarm window within one evaluation period.
- `/health` is excluded from all metrics: the ALB target group and the ECS container health check poll it every ~30s per task, so counting probes would inflate `ApiRequests` and hold phantom in-flight slots while idle. No request paths, headers, bodies, query strings, credentials or full URLs are ever recorded, and dimensions are bounded (`ClusterName`, `ServiceName`, `StatusClass`).

Metric publication is best-effort and must never make a user request fail. Counts and latency samples are retained (bounded) until a flush succeeds, so a brief CloudWatch outage produces a catch-up flush rather than losing the interval; there is no unbounded retry queue. If custom metrics do not appear, inspect the API logs and the ECS task role's `cloudwatch:PutMetricData` permission.

Known limitations of custom-gauge target tracking:

- Scaling accuracy depends on the application actually publishing: if publication fails (for example an IAM/namespace mismatch) the metric is missing and the alarm cannot scale out. CloudWatch publication errors are logged on the API task.
- When `CLOUDWATCH_METRICS_ENABLED=true`, both `ECS_CLUSTER_NAME` and `ECS_SERVICE_NAME` are required. Without them the API fails fast at startup with `MissingMetricDimensionsError` (the task never becomes healthy and the deployment circuit breaker rolls the revision back) rather than publishing a gauge the policy cannot match.
- `ApiRequests` counts and `ApiLatencyMs` samples are buffered per task between flushes and retained until a flush succeeds. A transient CloudWatch outage therefore causes a catch-up, not data loss; a partial multi-chunk failure can republish an already-sent chunk. Memory stays bounded (one counter per status class, at most 500 latency samples).
- Latency keeps at most the 500 most recent samples per flush interval, so percentiles under sustained load are an approximation.
- The gauge must be published by exactly one process per ECS task. The API container runs a single `uvicorn` worker; if workers were ever increased, each worker would publish its own value and the average would be diluted.


## Cost controls

- One NAT gateway is used by default to limit fixed NAT cost; it is still billed hourly plus data processing.
- ALB, Fargate tasks, NAT, ECR storage, CloudWatch metrics/log ingestion, and DynamoDB requests/storage can all incur charges.
- This configuration does not create GPU capacity. It avoids GPU costs while validating the AWS infrastructure and simulated failure path.
- Keep `api_min_capacity=1`, `api_max_capacity=5`, and mock desired count 1 for the demo.
- Stop immediately after the demo. A budget alert is not a hard spending cap.

## Destroy

```bash
./infra/scripts/destroy.sh
```

The script prints a destroy plan and requires typing `DESTROY`. Then verify ECS services/tasks, ALB, NAT gateway/EIP, ENIs, CloudWatch log groups/dashboard, DynamoDB table, ECR repositories and other resources in `ap-south-1`. Terraform deletes the ECR repositories and DynamoDB table created by this stack, including their contents; preserve any evidence you need first. If an apply failed before Terraform recorded a resource, inspect the AWS console/CLI for orphans.

## Fresh-account limitations

No console setup is required for the infrastructure resources after valid AWS credentials exist. AWS account registration, MFA, initial identity/credential bootstrapping, billing activation and any quota approval are outside Terraform's ability to bypass. HTTPS with a public certificate also requires a validated domain/certificate unless an existing certificate ARN is supplied.
