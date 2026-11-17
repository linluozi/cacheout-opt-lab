"""Differential test: patched cacheout vs upstream, 1000+ unique scenarios.

The baseline is rebuilt from this repository's patched source by reverse
applying patch.diff in a temp directory, then both packages are exercised with
the same deterministic op sequence and their full traces compared byte for
byte.  Deduplication is by sha256 of the scenario spec, not by loop count.
"""

import hashlib
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src", "cacheout")
PATCH = os.path.join(REPO, "patch.diff")
sys.path.insert(0, os.path.join(REPO, "src"))
import cacheout as opt_mod  # noqa: E402

N_SCENARIOS = 1300
SEED = 20261118
KEYS = ["a", "b", "", "héllo", "极值", 0, 1, True, 1.0, -1, None, ("t", 1)]
VALUES = [None, 0, "v", "值", True, 3.5, [1, 2]]


def load_baseline():
    tmp = tempfile.mkdtemp(prefix="cacheout-diff-base-")
    dst = os.path.join(tmp, "src", "cacheout")
    shutil.copytree(SRC, dst)
    subprocess.run(["patch", "-R", "-p1", "-s", "-i", PATCH], cwd=tmp, check=True)
    init = os.path.join(dst, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "cacheout_base", init, submodule_search_locations=[dst])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cacheout_base"] = mod
    spec.loader.exec_module(mod)
    return mod


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def norm(value):
    if isinstance(value, dict):
        return {str(k): norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [norm(v) for v in value]
    return repr(value)


def make_specs():
    rng = random.Random(SEED)
    specs, seen = [], set()
    while len(specs) < N_SCENARIOS:
        ops = []
        for _ in range(rng.randint(5, 12)):
            op = rng.choice(["get", "set", "delete", "expired", "get_ttl",
                             "delete_expired", "len", "popitem", "clear"])
            entry = {"op": op, "advance": rng.choice([0, 0, 0, 1, 6, 60])}
            if op in ("get", "set", "delete", "expired", "get_ttl"):
                entry["k"] = rng.choice(KEYS)
            if op == "set":
                entry["v"] = rng.choice(VALUES)
                entry["ttl"] = rng.choice([None, 0, 5, 10, 100])
            ops.append(entry)
        spec = {"maxsize": rng.choice([1, 2, 8, 256, None]),
                "ttl": rng.choice([0, 0, 5, 100]),
                "default": rng.choice([None, "D", 0]),
                "enable_stats": True, "ops": ops}
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        specs.append(spec)
    return specs


def run(mod, spec):
    clock = Clock()
    cache = mod.Cache(maxsize=spec["maxsize"], ttl=spec["ttl"],
                      timer=clock, default=spec["default"],
                      enable_stats=spec["enable_stats"])
    trace = []
    for entry in spec["ops"]:
        clock.t += entry["advance"]
        op = entry["op"]
        if op == "get":
            result = cache.get(entry["k"])
        elif op == "set":
            cache.set(entry["k"], entry["v"], ttl=entry["ttl"])
            result = "set"
        elif op == "delete":
            result = cache.delete(entry["k"])
        elif op == "expired":
            result = cache.expired(entry["k"])
        elif op == "get_ttl":
            result = cache.get_ttl(entry["k"])
        elif op == "delete_expired":
            result = cache.delete_expired()
        elif op == "len":
            result = len(cache)
        elif op == "popitem":
            try:
                result = cache.popitem()
            except (KeyError, StopIteration):
                result = "empty"
        else:
            cache.clear()
            result = "clear"
        state = {"items": norm(dict(cache.copy())),
                 "ttl": norm(cache.expire_times()),
                 "stats": norm(cache.stats.info()) if hasattr(cache.stats, "info") else None}
        trace.append({"op": op, "result": norm(result), "state": state})
    return trace


class DiffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = load_baseline()
        cls.specs = make_specs()
        cls.unique = len({hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest()
                          for s in cls.specs})
        print("\nunique differential scenarios: %d (seed %d, sha256-deduplicated)" % (cls.unique, SEED))

    def test_unique_sample_count(self):
        self.assertGreaterEqual(self.unique, 1000)
        self.assertEqual(self.unique, len(self.specs))

    def test_traces_are_byte_identical(self):
        differences = []
        for idx, spec in enumerate(self.specs):
            a = json.dumps(run(self.base, spec), sort_keys=True, ensure_ascii=False)
            b = json.dumps(run(opt_mod, spec), sort_keys=True, ensure_ascii=False)
            if a != b:
                differences.append(idx)
        self.assertEqual(differences, [], "first differing scenario indices: %s" % differences[:5])

    def test_patch_has_no_reverse_conflict(self):
        proc = subprocess.run(["git", "apply", "--check", "--reverse", PATCH], cwd=REPO,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipUnless(hasattr(sys, "monitoring"), "sys.monitoring needs CPython 3.12+")
    def test_plain_hits_no_longer_raise(self):
        mon = sys.monitoring
        ev = mon.events.RAISE
        counts = {"base": 0, "opt": 0, "warm": 0}
        current = ["warm"]

        def cb(code, offset, exc):
            counts[current[0]] += 1

        mon.use_tool_id(2, "cacheout-diff")
        mon.register_callback(2, ev, cb)
        try:
            mon.set_events(2, ev)
            for name, mod in (("base", self.base), ("opt", opt_mod)):
                current[0] = "warm"
                cache = mod.Cache(maxsize=600, ttl=0)
                for i in range(500):
                    cache.set(i, i)
                current[0] = name
                for i in range(500):
                    cache.get(i)
        finally:
            mon.set_events(2, 0)
            mon.register_callback(2, ev, None)
            mon.free_tool_id(2)
        self.assertGreaterEqual(counts["base"], 500, counts)
        self.assertEqual(counts["opt"], 0, counts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
