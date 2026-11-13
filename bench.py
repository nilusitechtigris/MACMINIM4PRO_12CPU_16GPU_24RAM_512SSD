#!/usr/bin/env python3
"""
Local LLM benchmark for Apple Silicon using Ollama.

For every model listed in models.txt it, one model at a time:
  1. makes sure nothing else is loaded,
  2. LOADS the model (timed), then does a tiny warm-up,
  3. runs the TEST: the same short prompt with identical options,
     measuring time-to-first-token (TTFT), decode tokens/sec, prefill
     tokens/sec and memory usage of the inference process,
  4. unloads the model.

A "round" is one pass over all models. The script runs ROUNDS rounds
(round 1 for all models, then round 2 for all, then round 3), so every
model is measured 3 times, never back-to-back.

Results are appended to results/raw_runs.jsonl after every single test,
so the run can be interrupted and resumed (finished tests are skipped).

Only the Python standard library is used.
"""
import ctypes
import ctypes.util
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Benchmark configuration - identical for every model
# --------------------------------------------------------------------------
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
PROMPT = "Explain how a computer CPU works, step by step."
# Same explicit system message for every model. Without it some Ollama packages inject
# their own long default system prompt (e.g. ~500 tokens for the Mistral models), which
# would make the effective prompt differ between models.
SYSTEM_PROMPT = "You are a helpful assistant."
NUM_CTX = 4096
OPTIONS = {
    "temperature": 0,      # greedy decoding -> deterministic, sampler-independent
    "seed": 42,
    "num_predict": 512,    # max generated tokens per test
    "num_ctx": NUM_CTX,    # same context window (and KV-cache size) for all models
    "draft_num_predict": 0,  # disable speculative decoding (Ollama ships MTP drafts for some
                             # models, e.g. Gemma 4) so we measure the model itself
}
LOAD_OPTIONS = {"num_ctx": NUM_CTX, "draft_num_predict": 0}   # load-time options must match
# raw (no chat template) warm-up text of similar length to the templated test prompt,
# so prefill + decode GPU kernels are initialised but no prompt-cache prefix is shared
WARMUP_PROMPT = "The quick brown fox jumps over the lazy dog while the sun sets slowly behind the quiet green hills of the old village."
# Ollama 0.40 has two engines (llama.cpp and MLX) and silently picks MLX builds for some
# default tags on Apple Silicon. All models must run on llama.cpp (GGUF) for a fair comparison.
REQUIRED_RUNNER = "llamacpp"
ROUNDS = int(os.environ.get("ROUNDS", "3"))
COOLDOWN_S = float(os.environ.get("COOLDOWN_S", "5"))
SAMPLE_INTERVAL_S = 0.1
REQUEST_TIMEOUT_S = 900

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_FILE = os.environ.get("MODELS_FILE", os.path.join(HERE, "models.txt"))
RESULTS_DIR = os.environ.get("RESULTS_DIR", os.path.join(HERE, "results"))
RAW_FILE = os.path.join(RESULTS_DIR, "raw_runs.jsonl")
META_FILE = os.path.join(RESULTS_DIR, "run_metadata.json")

GB = 1024 ** 3

# --------------------------------------------------------------------------
# macOS process memory via proc_pid_rusage (RUSAGE_INFO_V4)
# --------------------------------------------------------------------------
_libc = ctypes.CDLL(ctypes.util.find_library("c"))


class RUsageInfoV4(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups",
            "pageins", "wired_size", "resident_size", "phys_footprint",
            "proc_start_abstime", "proc_exit_abstime", "child_user_time",
            "child_system_time", "child_pkg_idle_wkups", "child_interrupt_wkups",
            "child_pageins", "child_elapsed_abstime", "diskio_bytesread",
            "diskio_byteswritten", "cpu_time_qos_default",
            "cpu_time_qos_maintenance", "cpu_time_qos_background",
            "cpu_time_qos_utility", "cpu_time_qos_legacy",
            "cpu_time_qos_user_initiated", "cpu_time_qos_user_interactive",
            "billed_system_time", "serviced_system_time", "logical_writes",
            "lifetime_max_phys_footprint", "instructions", "cycles",
            "billed_energy", "serviced_energy", "interval_max_phys_footprint",
            "runnable_time",
        )
    ]


def proc_mem(pid):
    """Return (resident_size, phys_footprint, lifetime_max_phys_footprint) in bytes."""
    info = RUsageInfoV4()
    if _libc.proc_pid_rusage(int(pid), 4, ctypes.byref(info)) != 0:
        return None
    return info.resident_size, info.phys_footprint, info.lifetime_max_phys_footprint


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def api(path, payload=None, timeout=REQUEST_TIMEOUT_S):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(OLLAMA + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ollama_serve_pid():
    for line in sh("ps -axo pid=,command=").splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if re.search(r"(^|/)ollama serve$", cmd.strip()):
            return int(pid)
    return None


def runner_pids():
    """Inference subprocesses spawned by `ollama serve` (llama-server / mlx runner)."""
    parent = ollama_serve_pid()
    pids = []
    for line in sh("ps -axo pid=,ppid=,command=").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, cmd = int(parts[0]), int(parts[1]), parts[2]
        if ppid == parent and ("llama-server" in cmd or "runner" in cmd):
            pids.append(pid)
    return pids


def system_memory():
    """Activity-Monitor-style memory numbers (bytes) + swap usage."""
    out = sh("vm_stat")
    page = int(re.search(r"page size of (\d+) bytes", out).group(1))

    def pages(label):
        m = re.search(re.escape(label) + r":\s+(\d+)", out)
        return int(m.group(1)) if m else 0

    wired = pages("Pages wired down") * page
    compressed = pages("Pages occupied by compressor") * page
    app = (pages("Anonymous pages") - pages("Pages purgeable")) * page
    cached = (pages("File-backed pages") + pages("Pages purgeable")) * page
    free = pages("Pages free") * page
    swap = sh("sysctl -n vm.swapusage")
    m = re.search(r"used = ([\d.]+)M", swap)
    swap_used = float(m.group(1)) * 1024 ** 2 if m else None
    return {
        "used_bytes": app + wired + compressed,   # = "Memory Used" in Activity Monitor
        "app_bytes": app,
        "wired_bytes": wired,
        "compressed_bytes": compressed,
        "cached_files_bytes": cached,
        "free_bytes": free,
        "swap_used_bytes": swap_used,
    }


class MemSampler(threading.Thread):
    """Samples memory of the Ollama inference subprocess every SAMPLE_INTERVAL_S."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_evt = threading.Event()
        self.pid = None
        self.peak_rss = 0
        self.peak_footprint = 0
        self.lifetime_max_footprint = 0
        self.samples = 0
        self.sys_peak_used = 0
        self._last_sys = 0.0

    def current(self):
        if self.pid is None:
            return None
        return proc_mem(self.pid)

    def run(self):
        while not self.stop_evt.is_set():
            if self.pid is None:
                pids = runner_pids()
                if pids:
                    self.pid = pids[-1]
            if self.pid is not None:
                m = proc_mem(self.pid)
                if m:
                    rss, fp, life = m
                    self.peak_rss = max(self.peak_rss, rss)
                    self.peak_footprint = max(self.peak_footprint, fp)
                    self.lifetime_max_footprint = max(self.lifetime_max_footprint, life)
                    self.samples += 1
            t = time.time()
            if t - self._last_sys > 1.0:   # system-wide view once per second
                self._last_sys = t
                try:
                    self.sys_peak_used = max(self.sys_peak_used, system_memory()["used_bytes"])
                except Exception:
                    pass
            self.stop_evt.wait(SAMPLE_INTERVAL_S)

    def stop(self):
        self.stop_evt.set()
        self.join(timeout=5)


def unload_all(timeout=120):
    try:
        loaded = api("/api/ps", timeout=10).get("models", [])
    except Exception:
        loaded = []
    for m in loaded:
        try:
            api("/api/generate", {"model": m["name"], "keep_alive": 0}, timeout=60)
        except Exception:
            pass
    t0 = time.time()
    while time.time() - t0 < timeout:
        busy = api("/api/ps", timeout=10).get("models", [])
        if not busy and not runner_pids():
            return time.time() - t0
        time.sleep(0.25)
    raise RuntimeError("models did not unload in time")


def stream_chat(model):
    """Run the test prompt; return client-side timings + Ollama's final stats."""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": PROMPT}],
        "options": OPTIONS,
        "stream": True,
        "keep_alive": -1,
    }
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    content, thinking = [], []
    t_first_chunk = t_first_token = None
    final = None
    t_send = time.perf_counter()
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:
        for raw in r:
            line = raw.strip()
            if not line:
                continue
            t = time.perf_counter()
            if t_first_chunk is None:
                t_first_chunk = t
            obj = json.loads(line)
            if "error" in obj:
                raise RuntimeError(obj["error"])
            msg = obj.get("message", {}) or {}
            c, th = msg.get("content") or "", msg.get("thinking") or ""
            if (c or th) and t_first_token is None:
                t_first_token = t
            if c:
                content.append(c)
            if th:
                thinking.append(th)
            if obj.get("done"):
                final = obj
    t_end = time.perf_counter()
    return {
        "t_send": t_send, "t_first_chunk": t_first_chunk, "t_first_token": t_first_token,
        "t_end": t_end, "content": "".join(content), "thinking": "".join(thinking),
        "final": final,
    }


def ns(v):
    return (v or 0) / 1e9


def bench_one(model, round_no):
    rec = {"model": model, "round": round_no, "started_at": now_iso(), "ok": False}

    # 1. clean slate + cool-down
    unload_all()
    time.sleep(COOLDOWN_S)
    rec["system_memory_before"] = system_memory()

    sampler = MemSampler()
    sampler.start()
    try:
        # 2. LOAD (no prompt -> Ollama only loads the weights)
        t0 = time.perf_counter()
        api("/api/generate", {"model": model, "options": LOAD_OPTIONS, "keep_alive": -1})
        rec["load_time_s"] = round(time.perf_counter() - t0, 3)

        ps = [m for m in api("/api/ps", timeout=10)["models"] if m["name"] == model]
        if ps:
            p = ps[0]
            rec["ollama_ps"] = {
                "size_bytes": p.get("size"), "size_vram_bytes": p.get("size_vram"),
                "context_length": p.get("context_length"), "runner": p.get("runner"),
                "format": p.get("details", {}).get("format"),
                "family": p.get("details", {}).get("family"),
                "parameter_size": p.get("details", {}).get("parameter_size"),
                "quantization_level": p.get("details", {}).get("quantization_level"),
            }
        # same-framework guard: every model must run on the same engine
        runner = (rec.get("ollama_ps") or {}).get("runner")
        if runner != REQUIRED_RUNNER:
            raise RuntimeError(f"model runs on engine '{runner}', expected '{REQUIRED_RUNNER}' "
                               f"(use an explicit GGUF tag, e.g. '<model>-q4_K_M')")
        # wait until the sampler has attached to the inference process
        for _ in range(50):
            if sampler.pid:
                break
            time.sleep(0.1)
        rec["runner_pid"] = sampler.pid
        if sampler.pid:
            # inference-engine command line (blob paths shortened) - documents the exact
            # engine flags, e.g. that no speculative decoding is active
            cmd = sh(f"ps -o command= -p {sampler.pid}")
            rec["runner_command"] = re.sub(r"/\S*/(sha256-[0-9a-f]{12})[0-9a-f]*", r"\1", cmd)
            rec["speculative_decoding"] = "--spec-" in cmd
        cur = sampler.current()
        rec["memory_after_load"] = (
            {"rss_bytes": cur[0], "phys_footprint_bytes": cur[1]} if cur else None)
        rec["system_memory_after_load"] = system_memory()

        # 3. tiny warm-up so one-time GPU kernel setup does not pollute the test
        w = api("/api/generate", {"model": model, "prompt": WARMUP_PROMPT, "raw": True,
                                  "stream": False, "keep_alive": -1,
                                  "options": {**OPTIONS, "num_predict": 8}})
        rec["warmup"] = {"load_duration_s": ns(w.get("load_duration")),
                         "total_duration_s": ns(w.get("total_duration"))}

        # 4. TEST
        r = stream_chat(model)
        f = r["final"] or {}
        eval_count = f.get("eval_count") or 0
        eval_s = ns(f.get("eval_duration"))
        pe_count = f.get("prompt_eval_count") or 0
        pe_s = ns(f.get("prompt_eval_duration"))
        wall = r["t_end"] - r["t_send"]
        rec["test"] = {
            "ttft_s": round(r["t_first_token"] - r["t_send"], 4) if r["t_first_token"] else None,
            "first_chunk_s": round(r["t_first_chunk"] - r["t_send"], 4) if r["t_first_chunk"] else None,
            "total_wall_s": round(wall, 3),
            "decode_tokens_per_s": round(eval_count / eval_s, 2) if eval_s else None,
            "prefill_tokens_per_s": round(pe_count / pe_s, 2) if pe_s else None,
            "end_to_end_tokens_per_s": round(eval_count / wall, 2) if wall else None,
            "generated_tokens": eval_count,
            "prompt_tokens": pe_count,
            "server_load_duration_s": round(ns(f.get("load_duration")), 4),
            "server_prompt_eval_s": round(pe_s, 4),
            "server_eval_s": round(eval_s, 4),
            "server_total_s": round(ns(f.get("total_duration")), 4),
            "done_reason": f.get("done_reason"),
            "thinking_chars": len(r["thinking"]),
            "content_chars": len(r["content"]),
            "output_preview": (r["thinking"] or r["content"])[:240],
        }
        time.sleep(SAMPLE_INTERVAL_S * 3)   # let the sampler catch the final state
        cur = sampler.current()
        rec["memory_after_test"] = (
            {"rss_bytes": cur[0], "phys_footprint_bytes": cur[1]} if cur else None)
        rec["system_memory_after_test"] = system_memory()
        rec["ok"] = True
    except Exception as e:  # keep going with the next model
        rec["error"] = f"{type(e).__name__}: {e}"
    finally:
        sampler.stop()
        rec["memory_peak"] = {
            "rss_bytes": sampler.peak_rss,
            "phys_footprint_bytes": sampler.peak_footprint,
            "lifetime_max_phys_footprint_bytes": sampler.lifetime_max_footprint,
            "system_used_bytes": sampler.sys_peak_used,
            "samples": sampler.samples,
        }
        # 5. UNLOAD
        try:
            rec["unload_time_s"] = round(unload_all(), 3)
        except Exception as e:
            rec["unload_error"] = str(e)
        rec["finished_at"] = now_iso()
    return rec


def machine_info():
    def sysctl(k):
        return sh(f"sysctl -n {k}")
    gpu_cores = re.search(r"Total Number of Cores:\s*(\d+)", sh("system_profiler SPDisplaysDataType"))
    plist = os.path.expanduser("~/Library/LaunchAgents/sh.brew.ollama.plist")
    server_env = {}
    if os.path.exists(plist):
        txt = open(plist).read()
        server_env = dict(re.findall(r"<key>(OLLAMA_[A-Z_]+)</key>\s*<string>([^<]*)</string>", txt))
    return {
        "model_identifier": sysctl("hw.model"),
        "chip": sysctl("machdep.cpu.brand_string"),
        "cpu_cores_total": int(sysctl("hw.ncpu")),
        "cpu_performance_cores": int(sysctl("hw.perflevel0.physicalcpu") or 0),
        "cpu_efficiency_cores": int(sysctl("hw.perflevel1.physicalcpu") or 0),
        "gpu_cores": int(gpu_cores.group(1)) if gpu_cores else None,
        "memory_bytes": int(sysctl("hw.memsize")),
        "macos": f'{sh("sw_vers -productVersion")} ({sh("sw_vers -buildVersion")})',
        "ollama_version": api("/api/version", timeout=10).get("version"),
        "ollama_server_env": server_env,
    }


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    models = [l.strip() for l in open(MODELS_FILE) if l.strip() and not l.startswith("#")]

    done = set()
    if os.path.exists(RAW_FILE):
        for line in open(RAW_FILE):
            r = json.loads(line)
            if r.get("ok"):
                done.add((r["model"], r["round"]))

    if not os.path.exists(META_FILE):
        meta = {
            "started_at": now_iso(),
            "machine": machine_info(),
            "prompt": PROMPT,
            "system_prompt": SYSTEM_PROMPT,
            "warmup_prompt": WARMUP_PROMPT,
            "options": OPTIONS,
            "rounds": ROUNDS,
            "cooldown_s": COOLDOWN_S,
            "models": models,
        }
        json.dump(meta, open(META_FILE, "w"), indent=2)

    total = len(models) * ROUNDS
    i = 0
    for rnd in range(1, ROUNDS + 1):
        for model in models:
            i += 1
            if (model, rnd) in done:
                print(f"[{i}/{total}] round {rnd} {model}: already done, skipping", flush=True)
                continue
            print(f"[{i}/{total}] round {rnd} {model} ...", end=" ", flush=True)
            rec = bench_one(model, rnd)
            with open(RAW_FILE, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            if rec["ok"]:
                t = rec["test"]
                print(f"load {rec['load_time_s']:.1f}s | TTFT {t['ttft_s']*1000:.0f} ms | "
                      f"{t['decode_tokens_per_s']} tok/s | {t['generated_tokens']} tok | "
                      f"peak RSS {rec['memory_peak']['rss_bytes']/GB:.2f} GiB", flush=True)
            else:
                print(f"FAILED: {rec.get('error')}", flush=True)

    meta = json.load(open(META_FILE))
    meta["finished_at"] = now_iso()
    json.dump(meta, open(META_FILE, "w"), indent=2)
    print("ALL ROUNDS FINISHED", flush=True)


if __name__ == "__main__":
    sys.exit(main())
