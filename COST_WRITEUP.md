# Fargate Cost Estimate — Track 1 (Appendix D)

Estimate for running this service on AWS Fargate against the synthetic traffic
profile: **10 calls/min baseline; two 2-hour campaign windows/day spiking to
50 calls/min** (10 → 50 over 15 min, hold, then back to 10). At ~300 ms median
per inference call, one small task sustains the baseline trivially; the spike is
~0.8 calls/s, comfortably absorbed by a handful of tasks.

Rates are the appendix's approximate 2026 ap-south-1 on-demand Fargate numbers
(≈10–15% above us-east-1's $0.04048/vCPU-hr and $0.004445/GB-hr); verify current
pricing before finalizing. Per-task hourly cost used below:
`(vCPUs × 0.046) + (GB × 0.0051)`.

## Configuration A — 1 vCPU / 2 GB, scale 1 → 5 on the spike

| Component | Math | Monthly |
|---|---|---|
| Baseline compute (20 h/day, 1 task) | `20 h × (1×0.046 + 2×0.0051)` | ≈ $1.13/day |
| Spike compute (4 h/day, avg ~3 tasks) | `4 h × 3 × (1×0.046 + 2×0.0051)` | ≈ $0.68/day |
| Compute subtotal | ≈ $1.81/day | **≈ $54/mo** |
| ALB (~$0.025/hr) | fixed overhead | **≈ $18/mo** |
| NAT gateway (~$0.05/hr) + egress/data | fixed overhead | **≈ $40–60/mo** |
| **Total** | | **≈ $112–132/mo** |

## Configuration B — 0.5 vCPU / 1 GB, scale 1 → 10 on the spike

Half the per-task compute cost and up to double the peak task count, so peak
compute lands close to Configuration A. The advantages are a **lower baseline
floor** (a 0.5 vCPU task comfortably serves 10 calls/min, so you idle less on
cold months) and **finer scale-out granularity** when traffic is spiky at a finer
grain than the table shows. ALB and NAT are the same fixed ~$58–78/mo overhead.

## Recommendation

**Start with Configuration B** for this workload: the profile is mostly flat with
two short, doubling-in-15-min spikes, which is exactly the case where fine-grained
0.5 vCPU tasks and aggressive-but-many tasks cost the least at idle and react
fastest to the ramp. Requirements before committing: load-test that 0.5 vCPU /
1 GB meets the Appendix B latency budget under the mock's p99 ≈ 1.2 s tail at
peak concurrency, and confirm per-task memory (httpx pools + SQLite writes are
small). **Prefer Configuration A** if request/second profiling shows the service
needs >0.5 vCPU per in-flight event or if fewer, larger tasks simplify operations.

Either way, the **fixed overhead (ALB + NAT, ~$58–78/mo) dominates the compute
bill**, so for this low-volume profile the honest takeaway is that monthly cost is
overwhelmingly plumbing, not compute — cut it with a private/NAT-free deployment
model if this shape persists in production.

Not measured: actual CPU/memory utilization per call, data processing/egress
volume, and CloudWatch/pricing drift — these are assumptions, not a quote.

---

# Track 2 Pre-Deployment Cost Review (verified ap-south-1 list prices)

The section above is the original Appendix D estimate and used the appendix's
*approximate* rates. The numbers below were re-derived from the AWS Price List
API / pricing calculator for **`ap-south-1`, September–October 2026**, and are
the ones used for the two deployment plans. They are list prices for a brand-new
account, before any free tier.

## Verified unit prices (ap-south-1)

| Item | Price |
|---|---|
| Fargate vCPU (x86) | $0.04256 /hr |
| Fargate memory (x86) | $0.004655 /GB-hr |
| Fargate vCPU (ARM/Graviton) | $0.02383 /hr |
| Fargate memory (ARM/Graviton) | $0.00261 /GB-hr |
| Fargate ephemeral storage | $0.000127 /GB-hr |
| ALB | $0.0239 /hr + $0.008 /LCU-hr + $0.008 /GB |
| NAT gateway | $0.056 /hr + $0.056 /GB |
| Public IPv4 address | $0.005 /hr |
| VPC interface endpoint | $0.013 /hr + $0.01 /GB |
| VPC gateway endpoint (DynamoDB/S3) | **free** |
| DynamoDB on-demand writes | $0.71 /million WRU |
| DynamoDB on-demand reads | $0.1425 /million RRU |
| DynamoDB storage | $0.285 /GB-mo (25 GB free) |
| CloudWatch custom metric | $0.30 /metric-mo |
| CloudWatch alarm | $0.10 /alarm-mo |
| CloudWatch API requests | $0.01 /1,000 |
| CloudWatch Logs ingest | $0.67 /GB |
| ECR storage | $0.10 /GB-mo |

## Plan A — private subnets + NAT gateway (original architecture)

Basis: 730 h/month, one API task (x86, 0.5 vCPU / 1 GB) and one mock task (x86,
0.25 vCPU / 0.5 GB) at `desired=1`, Container Insights on, ALB + NAT gateway.

| Component | Math | Monthly |
|---|---|---|
| API task (x86) | `(0.5×0.04256 + 1×0.004655) × 730` | ≈ $18.93 |
| Mock task (x86) | `(0.25×0.04256 + 0.5×0.004655) × 730` | ≈ $9.47 |
| NAT gateway | `0.056 × 730` (+ data) | ≈ $40.88 |
| ALB | `0.0239 × 730` + ~$1–2 LCU | ≈ $18–19 |
| Public IPv4 (ALB, 2 AZs) | `2 × 0.005 × 730` | ≈ $7.30 |
| CloudWatch metrics + alarm | 3 custom metrics + 1 alarm | ≈ $1.00 |
| Container Insights | estimated | ≈ $3–6 |
| Logs + DDB + ECR | low volume | ≈ $0.60 |
| **Total** | | **≈ $99–105/mo (~$0.14/hr)** |

## Plan B — budget plan (Terraform defaults after these changes)

Basis: same hours and task count, but tasks run in **public subnets with a public
IP** (no NAT), images are **ARM64**, API/mock at the **0.25 vCPU / 0.5 GB**
Fargate minimum, and **Container Insights is off**.

| Component | Math | Monthly |
|---|---|---|
| API task (ARM) | `(0.25×0.02383 + 0.5×0.00261) × 730` | ≈ $5.30 |
| Mock task (ARM) | `(0.25×0.02383 + 0.5×0.00261) × 730` | ≈ $5.30 |
| NAT gateway | removed | $0 |
| ALB | `0.0239 × 730` + ~$1–2 LCU | ≈ $18–19 |
| Public IPv4 (ALB 2 + tasks 2) | `4 × 0.005 × 730` | ≈ $14.60 |
| CloudWatch metrics + alarm | 3 custom metrics + 1 alarm | ≈ $1.00 |
| Container Insights | disabled | $0 |
| Logs + DDB + ECR | low volume | ≈ $0.60 |
| **Total** | | **≈ $44–46/mo (~$0.06/hr)** |

Plan B is roughly **half** the hourly burn. The ALB hourly charge and the public
IPv4 addresses now dominate; compute is only ~$10.60/month.

### Trade-offs of Plan B

- Tasks have a public IP. Their security groups still allow inbound only from the
  ALB / API security groups (never `0.0.0.0/0`), so they are **not** reachable
  from the internet — but the subnet is no longer an isolation boundary.
- Images must be built for `linux/arm64` (`build-and-push.sh` default). An x86
  image on an ARM task fails to start; the two settings must move together.
- Container Insights metrics are gone; scaling evidence comes from the app's
  `InFlightRequestsPerTask` metric and Application Auto Scaling `DesiredCapacity`.
- The DynamoDB **gateway** VPC endpoint (free) keeps DLQ traffic off the public
  internet, so dropping NAT does not weaken DLQ access.

### Budget headroom (README target ≈ $10–12)

- Plan A: ≈ 70–90 hours of continuous running.
- Plan B: ≈ 160–200 hours of continuous running.
- A 4-hour demo: Plan A ≈ $0.55; Plan B ≈ $0.25.

All figures are **estimates** from list prices and stated assumptions, not a
quote. Excluded: data-transfer beyond the first free GB, LCU usage at high
request rates, CloudWatch Logs above light demo volume, and any pricing change.
Confirm current regional pricing and watch actual spend.