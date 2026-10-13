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