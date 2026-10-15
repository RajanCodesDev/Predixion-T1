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
| VPC and subnets | Isolate public load-balancer resources from private application workloads |
| Internet gateway | Provides internet connectivity for public subnets |
| NAT gateway | Allows private resources to initiate outbound connections |
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

NAT gateways and load balancers can incur charges even when request volume is low. ECS tasks, public IPv4 addresses, logs, and other resources can also contribute to the total.

To control spending:

- Use the smallest task sizes that meet the demo's needs.
- Keep minimum and desired task counts low when idle.
- Run the demonstration only for the required period.
- Avoid leaving NAT gateways, ALBs, or running tasks active after testing.
- Review CloudWatch log retention and resource costs.
- Check AWS billing after the demo.

The challenge's target budget is approximately $10–12. Actual cost depends on region, resource lifetime, traffic, and configuration. Confirm current regional pricing and monitor actual spend rather than assuming the target is guaranteed.

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