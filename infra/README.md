# Track 2 AWS infrastructure (mock inference mode)

This Terraform stack creates real AWS infrastructure in `ap-south-1` while keeping inference simulated. The mock dependency preserves the challenge's independent 20% per-request HTTP 503 injection. **It does not run an open-weight model on GPU capacity**, so this is a cost-conscious infrastructure rehearsal, not a claim that the strict GPU-inference portion of Track 2 is complete.

## What Terraform provisions

- VPC with two public and two private subnets across two available AZs.
- Internet gateway, public routing, one NAT gateway by default, and private route tables.
- Public ALB and API target group with `/health` checks.
- ECS Fargate API service and private mock dependency service.
- Cloud Map private DNS so the API resolves `mock.collections-challenge.local`.
- ECR repositories for both images.
- CloudWatch log groups, a dashboard, Container Insights, and custom-metric target tracking for average in-flight requests per API task.
- DynamoDB-backed DLQ for records shared across API tasks.
- Task execution role and scoped task permissions for CloudWatch custom metrics and the DLQ table.
- ECS deployment circuit breaker and rollback.

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

Review every plan. This creates billable resources, particularly the NAT gateway and ALB. The script requires typing `APPLY` before the full deployment.

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

- `InFlightRequestsPerTask` — average concurrent requests in each API task, used by target tracking.
- `ApiRequests` — request count, dimensioned by `StatusClass`.
- `ApiLatencyMs` — API request duration, from which p90/p99 statistics are plotted.

Metric publication is best-effort and must never make a user request fail. If custom metrics do not appear, inspect the API logs and the ECS task role's `cloudwatch:PutMetricData` permission.

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
