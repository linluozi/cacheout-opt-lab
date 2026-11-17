"""Benchmark harness: real-corpus token cache, baseline vs patched cacheout.

Each invocation measures ONE implementation at ONE scale, writing every timed
sample to benchmark/raw/<impl>-<scale>-<i>.json.  The baseline implementation
is reconstructed in a temp directory by reverse-applying patch.diff to the
patched source, so both sides run the same code except for the patch.
"""

import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH = os.path.join(REPO, "patch.diff")
CORPUS = os.path.join(REPO, "benchmark", "corpus.txt")
RAW = os.path.join(REPO, "benchmark", "raw")
SCALES = {"small": 20_000, "medium": 100_000, "large": 500_000}
MAXSIZE = 4096
WARMUPS = 3
SAMPLES = 5
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def corpus_sha256():
    with open(CORPUS, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def tokens(n):
    with open(CORPUS, encoding="utf-8") as fh:
        words = [w.lower() for w in TOKEN_RE.findall(fh.read())]
    if not words:
        raise SystemExit("empty corpus")
    out = []
    while len(out) < n:
        out.extend(words)
    return out[:n]


def import_impl(impl):
    if impl == "opt":
        sys.path.insert(0, os.path.join(REPO, "src"))
    else:
        tmp = tempfile.mkdtemp(prefix="cacheout-base-")
        shutil.copytree(os.path.join(REPO, "src", "cacheout"),
                        os.path.join(tmp, "src", "cacheout"))
        subprocess.run(["patch", "-R", "-p1", "-s", "-i", PATCH],
                       cwd=tmp, check=True)
        sys.path.insert(0, os.path.join(tmp, "src"))
    import cacheout  # noqa: PLC0415
    return cacheout.Cache


def one_run(Cache, words):
    cache = Cache(maxsize=MAXSIZE, ttl=0)
    hits = misses = 0
    t0 = perf_counter()
    for word in words:
        value = cache.get(word)
        if value is None:
            cache.set(word, len(word))
            misses += 1
        else:
            hits += 1
    elapsed = perf_counter() - t0
    return elapsed, hits, misses, len(cache)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", choices=["base", "opt"], required=True)
    ap.add_argument("--scale", choices=sorted(SCALES), required=True)
    args = ap.parse_args()
    Cache = import_impl(args.impl)
    words = tokens(SCALES[args.scale])
    for _ in range(WARMUPS):
        one_run(Cache, words)
    os.makedirs(RAW, exist_ok=True)
    digest = corpus_sha256()
    rows = []
    for i in range(SAMPLES):
        elapsed, hits, misses, length = one_run(Cache, words)
        row = {
            "impl": args.impl,
            "scale": args.scale,
            "i": i,
            "lookups": len(words),
            "elapsed_s": elapsed,
            "hits": hits,
            "misses": misses,
            "hit_rate": hits / (hits + misses),
            "cache_len": length,
            "corpus_sha256": digest,
            "maxsize": MAXSIZE,
            "warmups": WARMUPS,
            "python": sys.version.split()[0],
            "maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
        with open(os.path.join(RAW, "%s-%s-%d.json" % (args.impl, args.scale, i)), "w") as fh:
            json.dump(row, fh, indent=2, sort_keys=True)
            fh.write("\n")
        rows.append(row)
    print(json.dumps({"impl": args.impl, "scale": args.scale,
                      "elapsed_s": [round(r["elapsed_s"], 6) for r in rows],
                      "hit_rate": round(rows[0]["hit_rate"], 4)}))


if __name__ == "__main__":
    main()
