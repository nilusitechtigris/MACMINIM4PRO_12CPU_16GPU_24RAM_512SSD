# Methodology

The goal: **change only the model**. Everything else (hardware, inference engine, engine settings,
prompt, decoding settings, context size, measurement procedure) is identical for all 20 models.

## Fixed setup

| Item | Value |
|---|---|
| Machine | Mac mini (`Mac16,11`), Apple M4 Pro: 12-core CPU (8 performance + 4 efficiency), 16-core GPU, 24 GB unified memory, 512 GB SSD |
| OS | macOS 27.2.1 |
| Framework | Ollama 0.40.16 (Homebrew), **llama.cpp engine (`llama-server`) for every model** |
| Engine settings (server-wide) | Flash attention on, KV cache `q8_0` (`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`), batch 512, 1 parallel slot |
| GPU offload | 100 % of every model on the Metal GPU (Ollama `size_vram == size` for all runs) |
| Model format | GGUF; `Q4_K_M` weights for 19 models, `gpt-oss:20b` uses its native `MXFP4` (the only format it ships in) |
| Context window | `num_ctx = 4096` for every model (so the KV-cache size rule is the same) |
| Speculative decoding | **Off** (`draft_num_predict = 0`). Ollama ships MTP draft heads for Gemma 4 and turns them on by default; this was disabled so every number is the model's own speed. The exact `llama-server` command line of every run is stored in the raw data. |
| Decoding | `temperature = 0` (greedy), `seed = 42`, `num_predict = 512` |
| Prompt | system: `You are a helpful assistant.` / user: `Explain how a computer CPU works, step by step.` |

### Why an explicit system message?
Some Ollama model packages inject their own default system prompt when none is given
(`mistral-small3.2` ≈ 500 tokens, `ministral-3` ≈ 540 tokens). Without an explicit system message
those two models would have received a ~25× longer prompt than the others. With the same short
system message for everyone, the prompt is 21-92 tokens for all models (the difference is only each
model's own chat-template tokens, e.g. the gpt-oss "harmony" header).

### Why the explicit `-q4_K_M` tags for some models?
On Apple Silicon, Ollama 0.40 resolves the default tags of several new models (`qwen3.5:*`, `gemma4:*`,
`lfm3:*`, `nemotron-3-nano:*`) to **MLX** builds with different quantizations (MXFP8 / NVFP4) that run on
a different engine (Ollama's MLX runner instead of llama.cpp). To keep one engine and one quantization,
the explicit GGUF `Q4_K_M` tags were used for these models. The benchmark script also refuses to
measure a model unless Ollama reports the `llamacpp` runner for it, so an engine mix cannot slip in.

## Procedure

Each model is measured **3 times**, in 3 separate rounds: round 1 runs all 20 models one after the
other, then round 2 runs all 20 again, then round 3. A model is never tested twice in a row.

For every single test:

1. **Clean slate** - every model is unloaded and the script waits until no Ollama runner process exists.
2. **Cool-down** - 5 s pause.
3. **Load** - `POST /api/generate` without a prompt (Ollama only loads the weights onto the GPU),
   with the same `num_ctx` / `draft_num_predict` as the test so the test never triggers a reload.
   The wall time of this request is the *load time*.
4. **Warm-up** - one raw (no chat template) prompt of similar length, 8 tokens. This initialises the
   GPU kernels for prefill and decode so the first measured token is not slowed by one-time setup.
   It shares no prompt-cache prefix with the test. Not measured.
5. **Test** - the benchmark prompt via streaming `/api/chat`. Measured (see below).
6. **Unload** - the model is unloaded before the next one starts.

Memory of the inference process is sampled every 100 ms from step 3 to the end of step 5.

## Metrics

| Metric | How it is measured |
|---|---|
| **Decode tokens/s** | `eval_count / eval_duration` from Ollama's final response - pure generation speed |
| **TTFT** (time to first token) | Client side: from sending the HTTP request until the first streamed chunk that contains a generated token (answer *or* reasoning token). Model is already loaded and warmed up. |
| Prefill tokens/s | `prompt_eval_count / prompt_eval_duration` |
| End-to-end tokens/s | generated tokens / total client wall time of the request |
| **Load time** | wall time of the load request (weights from SSD - or the macOS file cache if still cached - into GPU memory) |
| **Peak memory (RSS)** | peak resident memory of the `llama-server` process: memory-mapped weights + KV cache + compute buffers + heap. This is the main memory number. |
| Peak phys_footprint | peak macOS `phys_footprint` of the process (private memory only: KV cache, compute buffers, heap; excludes the mmapped weights) |
| Ollama-reported size | what Ollama's `/api/ps` reports for the loaded model. Kept for reference only: for models that also load a vision projector, Ollama 0.40 under-reports this badly (e.g. 0.27 GiB for `gemma4:e4b`), so the measured RSS is used instead. |
| System memory used | Activity-Monitor-style "Memory Used" of the whole Mac (before load and peak) and swap usage |

All values are reported per run and as mean / median / min / max / standard deviation over the 3 rounds.
