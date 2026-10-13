# Replacing the mock with real open-weight models

The mock is only reachable through one seam: the `Pipeline` protocol in
[`app/pipeline.py`](../app/pipeline.py), which requires a single async method:

```python
class Pipeline(Protocol):
    async def process(self, request: PipelineRequest) -> dict: ...
```

`ResilientPipeline` (retry/backoff, circuit breaker, timeout policy), the DLQ,
event tracking and every route call this protocol — they never know whether the
impl is an HTTP stub or a real model stack. Swapping the mock means writing one
new class that implements `process()`; **none** of `app/resilience.py`,
`app/dlq.py` or `app/main.py` changes.

## Recommended open-weight stack (per challenge Appendix E)

| Stage | Model | Notes |
|---|---|---|
| STT | faster-whisper large-v3 / large-v3-turbo | CTranslate2; turbo trades a little accuracy for materially lower latency |
| LLM | Mistral-7B-Instruct-v0.3 or Llama-3.1-8B-Instruct | int8/int4 on 24 GB VRAM leaves room for STT+TTS on one g5.xlarge |
| TTS | Piper (CPU-viable) or Coqui XTTS-v2 (GPU-bound) | Piper is the safer latency choice for all-three-stages-on-one-box |

Different models are fine — note *why* (accuracy/latency/license trade-offs) in
your writeup; that reasoning scores higher than matching the list exactly.
**Check each model's license for your intended use before any Apache-2.0 public
release.**

## Two ways to implement the adapter

1. **In-process adapter** (lowest latency): wrap the model stack in one class that
   runs STT → LLM → TTS locally and returns the same dict shape
   (`status`, `transcript`, `response_audio_ref`, `latency_ms`). Keep raising
   `httpx`-style or `NonRetryableError` (from `app/resilience.py`) so the
   existing retryability classification keeps working.
2. **HTTP adapter** (like `MockPipeline` today): deploy the models behind an
   inference service exposing the mock's `/pipeline/process` contract (or a
   subset mapped inside the adapter), and point `MOCK_URL` at it — a drop-in.

## What the swap inherits for free

- Independent jittered-exponential-backoff retries and the circuit breaker.
- Failed events land in the replayable DLQ with the actual retry count.
- Latency targets stay in the mock's distribution shape: model choice must keep
  p50 ≈ 300 ms / p90 ≈ 700 ms / p99 ≈ 1200 ms *including* model time; faster-whisper
  turbo + int8 Mistral + Piper is the stack that best fits that budget on one GPU.
- No networking changes and no resilience-logic rewrites.