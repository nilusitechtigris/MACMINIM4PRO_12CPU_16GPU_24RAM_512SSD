#!/usr/bin/env python3
"""
Aggregate results/raw_runs.jsonl into:
  results/benchmark_results.json   - full results (metadata + every run + per-model stats)
  results/summary.csv              - one row per model (means of the 3 rounds)
  charts/*.svg                     - bar charts used in the README
Standard library only.
"""
import csv
import json
import os
import statistics as st
import urllib.request
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
CHARTS = os.path.join(HERE, "charts")
GB = 1024 ** 3

VENDOR = {
    "gemma3": "Google", "gemma4": "Google", "lfm3": "Liquid AI", "qwen3.5": "Alibaba (Qwen)",
    "qwen3": "Alibaba (Qwen)", "llama3.2": "Meta", "llama3.1": "Meta", "granite4.1": "IBM",
    "ministral-3": "Mistral AI", "mistral-small3.2": "Mistral AI", "phi4-mini": "Microsoft",
    "phi4": "Microsoft", "nemotron-3-nano": "NVIDIA", "olmo-3": "Allen Institute for AI (Ai2)",
    "deepseek-r1": "DeepSeek", "gpt-oss": "OpenAI",
}


def disk_sizes():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=10) as r:
            return {m["name"]: m["size"] for m in json.loads(r.read())["models"]}
    except Exception:
        return {}


def stats(vals, nd=2):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {
        "mean": round(st.mean(vals), nd),
        "median": round(st.median(vals), nd),
        "min": round(min(vals), nd),
        "max": round(max(vals), nd),
        "stdev": round(st.stdev(vals), nd) if len(vals) > 1 else 0.0,
        "cv_percent": round(100 * st.stdev(vals) / st.mean(vals), 1) if len(vals) > 1 and st.mean(vals) else 0.0,
        "n": len(vals),
    }


def load():
    meta = json.load(open(os.path.join(RES, "run_metadata.json")))
    runs = [json.loads(l) for l in open(os.path.join(RES, "raw_runs.jsonl")) if l.strip()]
    return meta, runs


def build(meta, runs):
    sizes = disk_sizes()
    out_models = []
    for model in meta["models"]:
        mr = sorted([r for r in runs if r["model"] == model], key=lambda r: r["round"])
        ok = [r for r in mr if r.get("ok")]
        ps = next((r.get("ollama_ps") for r in ok if r.get("ollama_ps")), {}) or {}
        compact = []
        for r in mr:
            if not r.get("ok"):
                compact.append({"round": r["round"], "ok": False, "error": r.get("error"),
                                "started_at": r.get("started_at")})
                continue
            t = r["test"]
            compact.append({
                "round": r["round"], "ok": True, "started_at": r["started_at"],
                "load_time_s": r["load_time_s"],
                "ttft_ms": round(t["ttft_s"] * 1000, 1) if t["ttft_s"] is not None else None,
                "decode_tokens_per_s": t["decode_tokens_per_s"],
                "prefill_tokens_per_s": t["prefill_tokens_per_s"],
                "end_to_end_tokens_per_s": t["end_to_end_tokens_per_s"],
                "generated_tokens": t["generated_tokens"],
                "prompt_tokens": t["prompt_tokens"],
                "total_wall_s": t["total_wall_s"],
                "done_reason": t["done_reason"],
                "thinking_output": t["thinking_chars"] > 0,
                "memory": {
                    "ollama_reported_gib": round((r.get("ollama_ps") or {}).get("size_bytes", 0) / GB, 3),
                    "ollama_reported_vram_gib": round((r.get("ollama_ps") or {}).get("size_vram_bytes", 0) / GB, 3),
                    "runner_rss_after_load_gib": round(r["memory_after_load"]["rss_bytes"] / GB, 3) if r.get("memory_after_load") else None,
                    "runner_peak_rss_gib": round(r["memory_peak"]["rss_bytes"] / GB, 3),
                    "runner_peak_phys_footprint_gib": round(r["memory_peak"]["lifetime_max_phys_footprint_bytes"] / GB, 3),
                    "system_used_before_load_gib": round(r["system_memory_before"]["used_bytes"] / GB, 3),
                    "system_used_peak_gib": round(r["memory_peak"]["system_used_bytes"] / GB, 3),
                    "swap_used_after_test_gib": round((r["system_memory_after_test"].get("swap_used_bytes") or 0) / GB, 3),
                },
            })
        okc = [c for c in compact if c["ok"]]
        s = {
            "decode_tokens_per_s": stats([c["decode_tokens_per_s"] for c in okc]),
            "ttft_ms": stats([c["ttft_ms"] for c in okc], 1),
            "prefill_tokens_per_s": stats([c["prefill_tokens_per_s"] for c in okc]),
            "end_to_end_tokens_per_s": stats([c["end_to_end_tokens_per_s"] for c in okc]),
            "load_time_s": stats([c["load_time_s"] for c in okc], 3),
            "runner_peak_rss_gib": stats([c["memory"]["runner_peak_rss_gib"] for c in okc], 3),
            "runner_peak_phys_footprint_gib": stats([c["memory"]["runner_peak_phys_footprint_gib"] for c in okc], 3),
            "ollama_reported_gib": stats([c["memory"]["ollama_reported_gib"] for c in okc], 3),
            "system_used_peak_gib": stats([c["memory"]["system_used_peak_gib"] for c in okc], 3),
            "generated_tokens": stats([c["generated_tokens"] for c in okc], 0),
        }
        name = model.split(":")[0]
        dec = s["decode_tokens_per_s"]["mean"] if s["decode_tokens_per_s"] else None
        rss = s["runner_peak_rss_gib"]["mean"] if s["runner_peak_rss_gib"] else None
        out_models.append({
            "model": model,
            "vendor": VENDOR.get(name, ""),
            "family": ps.get("family"),
            "parameters": ps.get("parameter_size"),
            "quantization": ps.get("quantization_level"),
            "format": ps.get("format"),
            "ollama_runner": ps.get("runner"),
            "context_length": ps.get("context_length"),
            "speculative_decoding_active": any(r.get("speculative_decoding") for r in ok),
            "vision_projector_loaded": any("--mmproj" in (r.get("runner_command") or "") for r in ok),
            "engine_command": next((r.get("runner_command") for r in ok if r.get("runner_command")), None),
            "disk_size_gib": round(sizes[model] / GB, 2) if model in sizes else None,
            "successful_runs": len(okc),
            "stats": s,
            "decode_tokens_per_s_per_gib": round(dec / rss, 2) if dec and rss else None,
            "runs": compact,
        })

    ok_models = [m for m in out_models if m["stats"]["decode_tokens_per_s"]]

    def rank(key, reverse):
        vals = [(m["model"], m["stats"][key]["mean"]) for m in ok_models if m["stats"][key]]
        return [{"rank": i + 1, "model": n, "value": v}
                for i, (n, v) in enumerate(sorted(vals, key=lambda x: x[1], reverse=reverse))]

    return {
        "title": "Local LLM benchmark - Mac mini M4 Pro (12-core CPU, 16-core GPU, 24 GB, 512 GB SSD)",
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
        "machine": meta["machine"],
        "methodology": {
            "framework": f"Ollama {meta['machine'].get('ollama_version')} (llama.cpp engine for every model)",
            "prompt": meta["prompt"],
            "system_prompt": meta.get("system_prompt"),
            "prompt_api": "/api/chat, one system + one user message, streaming",
            "options": meta["options"],
            "rounds": meta["rounds"],
            "order": "round-robin: all models in round 1, then all in round 2, then all in round 3",
            "per_test_procedure": [
                "unload everything and wait until no Ollama runner process exists",
                f"cool-down {meta.get('cooldown_s')} s",
                "LOAD the model (POST /api/generate without prompt, same num_ctx) - timed",
                "warm-up: one raw prompt of similar length, 8 tokens (initialises GPU kernels; not measured)",
                "TEST: the benchmark prompt via streaming /api/chat - measured",
                "unload the model",
            ],
            "metrics": {
                "ttft_ms": "client-side time from sending the request to receiving the first generated token (answer or reasoning token)",
                "decode_tokens_per_s": "eval_count / eval_duration reported by Ollama (pure generation speed)",
                "prefill_tokens_per_s": "prompt_eval_count / prompt_eval_duration reported by Ollama",
                "end_to_end_tokens_per_s": "generated tokens / total client wall time of the request",
                "load_time_s": "wall time of the load request (reading weights from SSD / OS file cache into GPU memory)",
                "runner_peak_rss_gib": "peak resident memory of the Ollama inference process (llama-server), sampled every 100 ms during load + test; includes the memory-mapped weights",
                "runner_peak_phys_footprint_gib": "peak macOS phys_footprint of the inference process (private memory: KV cache, compute buffers, heap - excludes the mmapped weights)",
                "ollama_reported_gib": "model memory reported by Ollama's /api/ps - reference only, under-reported by Ollama 0.40 for models with a vision projector",
                "system_used_peak_gib": "peak 'Memory Used' of the whole Mac (app + wired + compressed) during load + test",
            },
            "warmup_prompt": meta.get("warmup_prompt"),
        },
        "rankings": {
            "fastest_decode_tokens_per_s": rank("decode_tokens_per_s", True),
            "lowest_ttft_ms": rank("ttft_ms", False),
            "lowest_peak_rss_gib": rank("runner_peak_rss_gib", False),
            "fastest_load_time_s": rank("load_time_s", False),
        },
        "models": out_models,
    }


# --------------------------------------------------------------------------
# SVG charts (horizontal bars with min-max whiskers)
# --------------------------------------------------------------------------
def bar_chart(path, title, subtitle, rows, unit, color, fmt="{:.1f}", lower_is_better=False):
    """rows: list of (label, mean, min, max)."""
    rows = sorted(rows, key=lambda r: r[1], reverse=not lower_is_better)
    left, right, top, bar_h, gap = 230, 120, 74, 20, 8
    width = 900
    plot_w = width - left - right
    height = top + len(rows) * (bar_h + gap) + 40
    vmax = max(r[3] for r in rows) * 1.08
    sx = lambda v: left + plot_w * v / vmax
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
         f'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
         f'<rect width="{width}" height="{height}" rx="12" fill="#ffffff"/>',
         f'<text x="24" y="34" font-size="20" font-weight="700" fill="#111827">{escape(title)}</text>',
         f'<text x="24" y="56" font-size="13" fill="#6b7280">{escape(subtitle)}</text>']
    # grid
    step = nice_step(vmax)
    v = 0
    while v <= vmax:
        x = sx(v)
        o.append(f'<line x1="{x:.1f}" y1="{top-6}" x2="{x:.1f}" y2="{height-34}" stroke="#e5e7eb"/>')
        o.append(f'<text x="{x:.1f}" y="{height-16}" font-size="11" fill="#9ca3af" text-anchor="middle">{fmt_tick(v)}</text>')
        v += step
    for i, (label, mean, lo, hi) in enumerate(rows):
        y = top + i * (bar_h + gap)
        o.append(f'<text x="{left-10}" y="{y+bar_h*0.72:.1f}" font-size="13" fill="#374151" text-anchor="end">{escape(label)}</text>')
        o.append(f'<rect x="{left}" y="{y}" width="{max(sx(mean)-left,1):.1f}" height="{bar_h}" rx="4" fill="{color}"/>')
        # whisker min..max
        cy = y + bar_h / 2
        o.append(f'<line x1="{sx(lo):.1f}" y1="{cy}" x2="{sx(hi):.1f}" y2="{cy}" stroke="#111827" stroke-width="1.2" opacity="0.55"/>')
        for xv in (lo, hi):
            o.append(f'<line x1="{sx(xv):.1f}" y1="{cy-5}" x2="{sx(xv):.1f}" y2="{cy+5}" stroke="#111827" stroke-width="1.2" opacity="0.55"/>')
        o.append(f'<text x="{max(sx(mean), sx(hi))+8:.1f}" y="{y+bar_h*0.72:.1f}" font-size="12" font-weight="600" fill="#111827">{fmt.format(mean)} {escape(unit)}</text>')
    o.append("</svg>")
    open(path, "w").write("\n".join(o))


def nice_step(vmax):
    import math
    raw = vmax / 6
    p = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * p:
            return m * p
    return 10 * p


def fmt_tick(v):
    return f"{v:g}" if v < 10 else f"{v:.0f}"


def short(name):
    return name.replace("-it-q4_K_M", "").replace("-q4_K_M", "")


def scatter_chart(path, models):
    """Decode speed vs. memory, one dot per model, labels placed without overlaps."""
    pts = [(short(m["model"]), m["stats"]["runner_peak_rss_gib"]["mean"], m["stats"]["decode_tokens_per_s"]["mean"])
           for m in models if m["stats"]["decode_tokens_per_s"]]
    width, height, l, r, t, b = 900, 640, 70, 40, 74, 60
    xmax = max(p[1] for p in pts) * 1.12
    ymax = max(p[2] for p in pts) * 1.1
    sx = lambda v: l + (width - l - r) * v / xmax
    sy = lambda v: height - b - (height - t - b) * v / ymax
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
         f'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
         f'<rect width="{width}" height="{height}" rx="12" fill="#ffffff"/>',
         '<text x="24" y="34" font-size="20" font-weight="700" fill="#111827">Speed vs. memory</text>',
         '<text x="24" y="56" font-size="13" fill="#6b7280">Decode tokens/s (mean of 3 runs) vs. peak resident memory of the inference process</text>']
    for v in frange(0, xmax, nice_step(xmax)):
        o.append(f'<line x1="{sx(v):.1f}" y1="{t}" x2="{sx(v):.1f}" y2="{height-b}" stroke="#eef0f3"/>')
        o.append(f'<text x="{sx(v):.1f}" y="{height-b+18}" font-size="11" fill="#9ca3af" text-anchor="middle">{fmt_tick(v)}</text>')
    for v in frange(0, ymax, nice_step(ymax)):
        o.append(f'<line x1="{l}" y1="{sy(v):.1f}" x2="{width-r}" y2="{sy(v):.1f}" stroke="#eef0f3"/>')
        o.append(f'<text x="{l-8}" y="{sy(v)+4:.1f}" font-size="11" fill="#9ca3af" text-anchor="end">{fmt_tick(v)}</text>')
    o.append(f'<text x="{(l+width-r)/2}" y="{height-16}" font-size="12" fill="#4b5563" text-anchor="middle">Peak memory (GiB)</text>')
    o.append(f'<text x="18" y="{(t+height-b)/2}" font-size="12" fill="#4b5563" text-anchor="middle" transform="rotate(-90 18 {(t+height-b)/2})">Decode tokens / s</text>')

    # greedy label placement: try candidate offsets, keep the first that hits nothing
    dots = [(sx(x), sy(y)) for _, x, y in pts]
    placed = []  # label boxes (x0, y0, x1, y1)

    def hits(box):
        x0, y0, x1, y1 = box
        if x0 < l or x1 > width - 8 or y0 < t - 8 or y1 > height - b:
            return True
        for bx in placed:
            if not (x1 < bx[0] or x0 > bx[2] or y1 < bx[1] or y0 > bx[3]):
                return True
        for cx, cy in dots:
            if x0 - 6 < cx < x1 + 6 and y0 - 6 < cy < y1 + 6:
                return True
        return False

    labels = []
    order = sorted(range(len(pts)), key=lambda i: -pts[i][2])
    for i in order:
        name = pts[i][0]
        cx, cy = dots[i]
        w, h = 6.3 * len(name), 12
        cands = [(9, -4), (-9 - w, -4), (9, -14), (-9 - w, -14), (9, 6), (-9 - w, 6),
                 (-w / 2, -22), (-w / 2, 10), (9, -26), (9, 18), (-9 - w, -26), (-9 - w, 18),
                 (-w / 2, -34), (-w / 2, 22)]
        best = None
        for dx, dy in cands:
            box = (cx + dx, cy + dy - h + 3, cx + dx + w, cy + dy + 3)
            if not hits(box):
                best = (dx, dy, box)
                break
        if best is None:
            dx, dy = 9, -4
            best = (dx, dy, (cx + dx, cy + dy - h + 3, cx + dx + w, cy + dy + 3))
        placed.append(best[2])
        labels.append((name, cx, cy, best[0], best[1]))
    for cx, cy in dots:
        o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5.5" fill="#2563eb" opacity="0.85"/>')
    for name, cx, cy, dx, dy in labels:
        o.append(f'<text x="{cx+dx:.1f}" y="{cy+dy:.1f}" font-size="11" fill="#374151">{escape(name)}</text>')
    o.append("</svg>")
    open(path, "w").write("\n".join(o))


def frange(a, b, s):
    v = a
    while v <= b:
        yield v
        v += s


def main():
    meta, runs = load()
    res = build(meta, runs)
    json.dump(res, open(os.path.join(RES, "benchmark_results.json"), "w"), indent=2)

    with open(os.path.join(RES, "summary.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["model", "vendor", "parameters", "quantization", "disk_size_gib", "runs_ok",
                    "decode_tok_s_mean", "decode_tok_s_min", "decode_tok_s_max",
                    "ttft_ms_mean", "ttft_ms_min", "ttft_ms_max", "prefill_tok_s_mean",
                    "load_time_s_mean", "peak_rss_gib_mean", "peak_phys_footprint_gib_mean",
                    "ollama_reported_gib", "system_used_peak_gib_mean"])
        for m in res["models"]:
            s = m["stats"]
            g = lambda k, f="mean": (s[k] or {}).get(f)
            w.writerow([m["model"], m["vendor"], m["parameters"], m["quantization"], m["disk_size_gib"],
                        m["successful_runs"], g("decode_tokens_per_s"), g("decode_tokens_per_s", "min"),
                        g("decode_tokens_per_s", "max"), g("ttft_ms"), g("ttft_ms", "min"), g("ttft_ms", "max"),
                        g("prefill_tokens_per_s"), g("load_time_s"), g("runner_peak_rss_gib"),
                        g("runner_peak_phys_footprint_gib"), g("ollama_reported_gib"), g("system_used_peak_gib")])

    os.makedirs(CHARTS, exist_ok=True)
    ok = [m for m in res["models"] if m["stats"]["decode_tokens_per_s"]]
    row = lambda m, k: (m["model"], m["stats"][k]["mean"], m["stats"][k]["min"], m["stats"][k]["max"])
    bar_chart(os.path.join(CHARTS, "decode_tokens_per_sec.svg"), "Generation speed (decode tokens / s)",
              "Higher is better - mean of 3 runs, whiskers show min-max", [row(m, "decode_tokens_per_s") for m in ok],
              "tok/s", "#2563eb")
    bar_chart(os.path.join(CHARTS, "ttft_ms.svg"), "Time to first token (ms)",
              "Lower is better - model already loaded and warmed up, mean of 3 runs, whiskers show min-max",
              [row(m, "ttft_ms") for m in ok], "ms", "#d97706", fmt="{:.0f}", lower_is_better=True)
    bar_chart(os.path.join(CHARTS, "memory_gib.svg"), "Peak memory of the inference process (GiB)",
              "Lower is better - resident memory incl. weights, KV cache (4096 ctx) and buffers, mean of 3 runs",
              [row(m, "runner_peak_rss_gib") for m in ok], "GiB", "#7c3aed", fmt="{:.2f}", lower_is_better=True)
    bar_chart(os.path.join(CHARTS, "load_time_s.svg"), "Model load time (s)",
              "Lower is better - from load request until ready, mean of 3 runs, whiskers show min-max",
              [row(m, "load_time_s") for m in ok], "s", "#059669", fmt="{:.2f}", lower_is_better=True)
    scatter_chart(os.path.join(CHARTS, "speed_vs_memory.svg"), ok)
    write_tables(res, os.path.join(RES, "tables.md"))
    print(f"wrote results for {len(res['models'])} models ({len(ok)} with successful runs)")


def write_tables(res, path):
    ok = [m for m in res["models"] if m["stats"]["decode_tokens_per_s"]]
    ok.sort(key=lambda m: m["stats"]["decode_tokens_per_s"]["mean"], reverse=True)
    L = ["### Main results (mean of 3 runs, sorted by generation speed)", "",
         "| # | Model | Vendor | Params | Quant | Decode tok/s | TTFT (ms) | Prefill tok/s | Load (s) | Peak memory (GiB) | Disk (GiB) |",
         "|--:|---|---|--:|---|--:|--:|--:|--:|--:|--:|"]
    for i, m in enumerate(ok, 1):
        s = m["stats"]
        L.append(f"| {i} | `{m['model']}` | {m['vendor']} | {m['parameters']} | {m['quantization']} | "
                 f"**{s['decode_tokens_per_s']['mean']:.1f}** ± {s['decode_tokens_per_s']['stdev']:.1f} | "
                 f"{s['ttft_ms']['mean']:.0f} | {s['prefill_tokens_per_s']['mean']:.0f} | "
                 f"{s['load_time_s']['mean']:.2f} | {s['runner_peak_rss_gib']['mean']:.2f} | {m['disk_size_gib']} |")
    L += ["", "### Every run (round 1 / round 2 / round 3)", "",
          "| Model | Decode tok/s | TTFT (ms) | Load (s) | Peak memory (GiB) |", "|---|---|---|---|---|"]
    for m in ok:
        rs = {r["round"]: r for r in m["runs"] if r["ok"]}
        f = lambda k, fmt: " / ".join(fmt.format(rs[i][k]) if i in rs else "–" for i in (1, 2, 3))
        mem = " / ".join(f"{rs[i]['memory']['runner_peak_rss_gib']:.2f}" if i in rs else "–" for i in (1, 2, 3))
        L.append(f"| `{m['model']}` | {f('decode_tokens_per_s', '{:.1f}')} | {f('ttft_ms', '{:.0f}')} | "
                 f"{f('load_time_s', '{:.2f}')} | {mem} |")
    failed = [m for m in res["models"] if m["successful_runs"] < res["methodology"]["rounds"]]
    if failed:
        L += ["", "### Failed / incomplete", ""]
        for m in failed:
            errs = [r.get("error") for r in m["runs"] if not r["ok"]]
            L.append(f"- `{m['model']}`: {m['successful_runs']} successful runs; errors: {errs}")
    open(path, "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
