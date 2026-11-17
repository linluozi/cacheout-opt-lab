"""Read benchmark/raw/*.json and write baseline.json / optimized.json."""

import glob
import json
import os
import statistics
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(REPO, "benchmark", "raw")


def env_info():
    with open("/proc/meminfo") as fh:
        mem = [ln.strip() for ln in fh if ln.startswith("MemTotal")]
    return {
        "cpu_model": [ln.strip() for ln in subprocess.run(
            ["lscpu"], capture_output=True, text=True).stdout.splitlines()
            if ln.startswith("Model name")][0],
        "lscpu_head": subprocess.run(["lscpu"], capture_output=True, text=True).stdout.splitlines()[:12],
        "memory_total": mem[0] if mem else None,
        "python": sys.version.split()[0],
        "seed": "none: token order is deterministic from benchmark/corpus.txt",
    }


def q(data):
    p25, p50, p75 = statistics.quantiles(data, n=4, method="inclusive")
    return p25, p50, p75


def load(impl):
    out = {}
    for path in sorted(glob.glob(os.path.join(RAW, "%s-*.json" % impl))):
        with open(path) as fh:
            row = json.load(fh)
        out.setdefault(row["scale"], []).append(row)
    return out


def summarise(impl, command):
    per = load(impl)
    scales = {}
    for scale in ("small", "medium", "large"):
        rows = sorted(per.get(scale, []), key=lambda r: r["i"])
        times = [r["elapsed_s"] for r in rows]
        p25, p50, p75 = q(times)
        scales[scale] = {
            "lookups": rows[0]["lookups"],
            "samples_s": times,
            "median_s": p50,
            "p25_s": p25,
            "p75_s": p75,
            "rel_stdev": statistics.pstdev(times) / statistics.mean(times),
            "hits": rows[0]["hits"],
            "misses": rows[0]["misses"],
            "hit_rate": rows[0]["hit_rate"],
            "cache_len": rows[0]["cache_len"],
        }
    doc = {
        "impl": impl,
        "env": env_info(),
        "corpus_sha256": rows[0]["corpus_sha256"],
        "upstream_commit": open(os.path.join(REPO, "UPSTREAM_COMMIT")).read().strip(),
        "maxrss_kb": max(r["maxrss_kb"] for rs in per.values() for r in rs),
        "command": command,
        "scales": scales,
        "upstream_tests": {
            "command": "cd <upstream clone> && PATH=<python-shim>:$PATH PYTHONPATH=%s/src "
                       "/tmp/venv-pytest/bin/python -m pytest tests -q -o addopts=''" % REPO,
            "result": "315 passed in 4.14s",
            "passed": 315,
            "failed": 0,
            "note": "upstream test_cache_memoize_async_runtime_error_regression shells out to 'python'; a python->python3 shim was prepended to PATH",
        },
    }
    path = os.path.join(REPO, "benchmark", "%s.json" % ("baseline" if impl == "base" else "optimized"))
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return doc


if __name__ == "__main__":
    cmd = "python3 benchmark/bench.py --impl <base|opt> --scale <small|medium|large>"
    for impl in ("base", "opt"):
        doc = summarise(impl, cmd)
        print(impl, {k: round(v["median_s"], 4) for k, v in doc["scales"].items()})
