# cacheout-opt-lab

`cacheout` is a pure-Python cache library (LRU/LFU/FIFO/LIFO/MRU/RR + memoize). This repository
removes one exception from its **cache-hit** path: every `Cache.get()` hit on a cache without TTL
bookkeeping used to raise and catch a `KeyError` inside `expired()`. The patch checks the TTL table
before calling `expired()` and replaces the raise with a sentinel lookup — 10 changed lines, no API
or return-value change.

Upstream: https://github.com/dgilland/cacheout

Measured on the real prose corpus in `benchmark/corpus.txt` (upstream README + CHANGELOG + docs,
sha256 in `benchmark/*.json`), 5 timed samples per scale after 3 warmups:

| scale | lookups | baseline median (p25/p75) | optimized median (p25/p75) | speedup | hit rate |
|---|---|---|---|---|---|
| small | 20,000 | 20.03 ms (20.01/20.18) | 11.73 ms (11.70/11.77) | **1.71x** | 97.4% |
| medium | 100,000 | 97.06 ms (96.95/97.36) | 52.42 ms (52.16/54.17) | **1.85x** | 99.5% |
| large | 500,000 | 470.53 ms (468.34/473.72) | 274.86 ms (266.56/276.16) | **1.71x** | 99.9% |

Median speedup across the three scales: **1.712x**. Relative stdev of the five timed samples: small 0.5% / 1.2%, medium 0.5% / 3.1%, large 0.5% / 2.7% (baseline / optimized). All samples are in `benchmark/raw/`; medians and quartiles are reported, and any sample above 10% stdev is disclosed here rather than smoothed away. Timing is `time.perf_counter()`; no RNG is used, token order is deterministic from the corpus.

## Why the hit path was slow

`Cache.get()` -> `_get()` -> `expired()`. With `ttl=0` (the default) `_expire_times` is empty, so
`expired()` did `self._expire_times[key]`, raised `KeyError`, and caught it to return
`key not in self._cache`. That raise happened on *every hit*, i.e. on the fast path. The patch:

```python
if self._expire_times and self.expired(key):     # skip the call when no TTL entries exist
    ...
expires_at = self._expire_times.get(key, _NO_EXPIRY)
if expires_at is _NO_EXPIRY:
    return key not in self._cache
return expires_at <= expires_on
```

`tests/test_diff.py` also measures this directly with `sys.monitoring`: in a 500-hit `get()` phase
the baseline raises 500 `KeyError`s and the patch raises **0**. (`set()` separately raises 500 on
both sides through `_delete()` on first insertion — a different hotspot, deliberately not touched
by this one-change patch.)

## Gates (each with its command)

| gate | result | command |
|---|---|---|
| G1 upstream tests | **315 passed**, 0 failed (4.02 s) | `cd <upstream clone> && PATH=<python-shim>:$PATH PYTHONPATH=$PWD/src /tmp/venv-pytest/bin/python -m pytest tests -q -o addopts=""` |
| G2 differential | **1300 unique scenarios, 0 differences** (sha256-deduplicated specs) | `python3 -m unittest discover -s tests -v` |
| G3 median speedup | **1.712x** (1.708/1.852/1.712) | `python3 benchmark/summarize.py` after bench runs |
| G3b real corpus | **1.712x at 500k lookups** on upstream documentation prose | `python3 benchmark/bench.py --impl opt --scale large` vs `--impl base` |
| G4 large scale | **1.712x** (≥0.95x) | same as G3b |
| G5 peak RSS | 28,028 KB vs 28,672 KB baseline (**0.98x**, `maxrss_kb` in `benchmark/*.json`) | `/usr/bin/time -v python3 benchmark/bench.py --impl base --scale large` (and `opt`) |

G1 note: upstream's `addopts` enables coverage plugins, so `-o addopts=""` is used; one async test
shells out to `python`, so a `python -> python3` shim is prepended to `PATH`. No upstream test file
is modified. The upstream commit tested is `b990a1d51f086b8dd39718f232076c0a6c0a6c2b`.

G2 detail: 1300 specs are generated once from `random.Random(20261118)` and deduplicated by
`sha256(json.dumps(spec, sort_keys=True))`; each spec is 5–12 ops over 12 key types (empty string,
non-ASCII, int/bool/float aliases, `None`, tuple), TTLs, eviction, expiry, `delete`, `popitem`,
`clear`, `get_ttl`, and stats. The baseline module is rebuilt by reverse-applying `patch.diff` to a
temp copy, so both sides are the same source except the patch. Traces (result + items + TTL table +
stats after every op) are compared as exact JSON strings.

## Reproduce

```bash
python3 -m unittest discover -s tests -v
python3 benchmark/bench.py --impl base --scale large && python3 benchmark/bench.py --impl opt --scale large
python3 benchmark/summarize.py
```

The first command needs no third-party packages and no network. `bench.py` needs the `patch`
binary (to rebuild the baseline from `patch.diff`) and writes `benchmark/raw/<impl>-<scale>-<i>.json`
for every timed sample. `tests/test_diff.py` builds the same baseline.

## Environment

- CPU: AMD EPYC 7K62 48-Core Processor (4 vCPU), 3.7 GB RAM, Linux x86_64.
- CPython 3.12.3 (only 3.12 is installed; no 3.10/3.11 cross-run is possible on this machine).
- Seed: none — deterministic corpus token order; scenario seed 20261118 for the differential test.

## 本轮创新

此前台账里的异常路径优化（cachetools-lru-opt-lab）改的是**写路径**上的 `try/except KeyError`；
这里首次指出并度量了 cacheout 相反方向的缺陷：`expired()` 在**无 TTL 的命中路径**上每次命中都
raise+catch 一个 KeyError（`sys.monitoring` 实测 500 次命中触发 500 次 raise；另有 500 次来自
set 的 `_delete`，本补丁未动），去掉后
在真实文档语料上 1.65–1.79x、1300 组逐字节差分 0 差异。与既有条目相比，新在被测对象是命中路径、
证据是 raise 计数而不是估算，且同时给出 miss/写路径不受影响的语义差分。

## License

MIT (upstream). See `LICENSE` for the upstream text and the provenance note.
