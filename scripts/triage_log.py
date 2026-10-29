#!/usr/bin/env python3
"""Bounded read-only digest of N64to3DS/DaedalusX64 text logs.

Intentionally does not replace the project's baseline_stats.py. Never treats a
missing/unrecognized field as zero. No third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict, deque
from pathlib import Path

NUM = r"(?:0x[0-9a-fA-F]+|[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
KV_RE = re.compile(rf"(?<!\w)([A-Za-z_][\w/%.-]*)\s*[:=]\s*({NUM})(?!\w)")
EVENT_RE = re.compile(
    r"\b(?:watchdog|deadlock|guest\s+hang|data\s+abort|prefetch\s+abort|"
    r"stack\s+overflow|panic|fatal|segfault|crash|assert(?:ion)?\s+fail|"
    r"exception|cpu_halt)\b",
    re.IGNORECASE,
)
FRAGMENT_RE = re.compile(r"\b(?:FRAGLOG|fragment\s+(?:created|compiled)|late\s+frag)\b", re.I)
ALIASES = {
    "vi": {"vi/s", "vis/s", "vis_per_s", "vi_per_s", "vi_s", "vi_rate", "vis"},
    "presents": {"presents_per_s", "presents/s"},
    "flips": {"flips_per_s", "flips/s"},
    "frag_exec": {"frag_exec"},
    "interp_ops": {"interp_ops"},
    "ring_full_waits": {"ring_full_waits"},
    "ring_dropped": {"ring_dropped"},
    "underruns": {"underruns", "audio_underruns"},
    "gfx_wait": {"gfx_wait%", "gfx_wait_ms", "gfx_wait"},
    "gfx_thread": {"gfx_thread%", "gfx_thread_ms", "gfx_thread"},
    "dynarec": {"dynarecenabled", "dynarec_enabled", "g_dynarec_enabled", "gdynarecenabled"},
}
ALIAS_LOOKUP = {a: name for name, synonyms in ALIASES.items() for a in synonyms}
MAX_SAMPLES = 10_000


def number(token: str) -> float:
    return float(int(token, 16)) if token.lower().startswith("0x") else float(token)


def percentile_nearest(values: list[float], proportion: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int((len(ordered) - 1) * proportion)))]


def fmt(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.2f}".rstrip("0").rstrip(".")


def analyze(path: Path, max_events: int) -> dict:
    samples: dict[str, list[float]] = defaultdict(list)
    seen: dict[str, int] = defaultdict(int)
    first_events: list[dict] = []
    last_events: deque = deque(maxlen=max_events)
    last_fragments: deque = deque(maxlen=5)
    line_count = 0
    interp_last_positive = False
    interp_transition = False
    zero_render_window_count = 0
    zero_render_sustained = False
    low_vi_and_no_jit = False
    observed_dynarec_not_enabled = False
    # Decode defensively; do not silently discard unreadable bytes.
    with path.open("r", encoding="utf-8", errors="replace") as inp:
        for line_count, line in enumerate(inp, 1):
            parsed: dict[str, float] = {}
            for match in KV_RE.finditer(line):
                canonical = ALIAS_LOOKUP.get(match.group(1).lower())
                if canonical is None:
                    continue
                try:
                    value = number(match.group(2))
                except ValueError:
                    continue
                parsed[canonical] = value
                seen[canonical] += 1
                if len(samples[canonical]) < MAX_SAMPLES:
                    samples[canonical].append(value)
            if "dynarec" in parsed and parsed["dynarec"] != 1:
                observed_dynarec_not_enabled = True
            if "interp_ops" in parsed:
                if interp_last_positive and parsed["interp_ops"] == 0:
                    interp_transition = True
                if parsed["interp_ops"] > 0:
                    interp_last_positive = True
            if parsed.get("vi", 0) >= 40 and parsed.get("presents", -1) == 0:
                zero_render_window_count += 1
                zero_render_sustained = zero_render_window_count >= 3 or zero_render_sustained
            elif "vi" in parsed and "presents" in parsed:
                zero_render_window_count = 0
            if (0 < parsed.get("vi", 1e6) <= 5 and
                parsed.get("interp_ops") == 0 and parsed.get("frag_exec") == 0):
                low_vi_and_no_jit = True
            snippet = line.strip()[:250]
            if EVENT_RE.search(line):
                event = {"line": line_count, "text": snippet}
                if len(first_events) < max_events:
                    first_events.append(event)
                last_events.append(event)
            if FRAGMENT_RE.search(line):
                last_fragments.append({"line": line_count, "text": snippet})

    metrics: dict[str, dict] = {}
    for key, values in samples.items():
        metrics[key] = {
            "found": seen[key],
            "sampled": len(values),
            "first": values[0],
            "last": values[-1],
            "max": max(values),
            "median": statistics.median(values),
            "p5": percentile_nearest(values, .05),
        }
    notes = []
    if observed_dynarec_not_enabled:
        notes.append("Dynarec is not explicitly enabled in at least one log line")
    if low_vi_and_no_jit:
        notes.append("Low VI and zero reported JIT/interpreter counters in same window; investigate Preferences.ini")
    if interp_transition:
        notes.append("interp_ops transitioned from positive to zero; correlate with guest interrupt/exception handling")
    if zero_render_sustained:
        notes.append(">=3 windows with VI >=40 and zero presents; possible guest/render hang")
    if "ring_dropped" in metrics and metrics["ring_dropped"]["max"] > 0:
        notes.append("Nonzero ring_dropped observed; inspect sample-loss timing")
    if "ring_full_waits" in metrics and metrics["ring_full_waits"]["max"] > 0:
        notes.append("Full-ring waits observed; check whether progress resumed")
    return {
        "file": str(path), "lines": line_count,
        "metrics": metrics, "notes": notes,
        "dynarec_not_enabled_observed": observed_dynarec_not_enabled,
        "first_events": first_events,
        "last_events": list(last_events),
        "last_fragment_lines": list(last_fragments),
        "notice": "Counter values may be per-window or cumulative; first/last/max are NOT totals. Missing metrics are unknown.",
    }


def display(d: dict) -> None:
    print(f"\n== {d['file']} ({d['lines']} lines) ==")
    metrics = d["metrics"]
    for key in ("vi", "presents", "flips", "gfx_wait", "gfx_thread", "underruns", "ring_full_waits", "ring_dropped", "frag_exec", "interp_ops", "dynarec"):
        item = metrics.get(key)
        if item:
            print(f"  {key}: median={fmt(item['median'])} p5={fmt(item['p5'])} "
                  f"first={fmt(item['first'])} last={fmt(item['last'])} max={fmt(item['max'])} "
                  f"(sampled {item['sampled']} / seen {item['found']})")
    if not metrics:
        print("  No recognized numeric metrics; inspect a short sample of the original log format.")
    for n in d["notes"]:
        print(f"  FLAG: {n}")
    events = {e["line"]: e for e in d["first_events"] + d["last_events"]}
    for e in sorted(events.values(), key=lambda r: r["line"]):
        print(f"  EVENT L{e['line']}: {e['text']}")
    if d["last_fragment_lines"]:
        print("  Last matching fragment lines:")
        for e in d["last_fragment_lines"]:
            print(f"    L{e['line']}: {e['text']}")
    print(f"  NOTE: {d['notice']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="Emit compact JSON instead of human-readable output")
    parser.add_argument("--max-events", type=int, default=5, help="Number of first/last anomaly lines (1..30)")
    parser.add_argument("--require-dynarec", action="store_true",
                        help="Fail if an explicit enabled dynarec state is missing or any disabled state is seen")
    args = parser.parse_args()
    if not (1 <= args.max_events <= 30):
        parser.error("--max-events must be 1..30")
    results = []
    had_errors = False
    for path in args.logs:
        try:
            results.append(analyze(path, args.max_events))
        except (OSError, UnicodeError) as exc:
            had_errors = True
            results.append({"file": str(path), "error": str(exc)})
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for result in results:
            if "error" in result:
                print(f"ERROR {result['file']}: {result['error']}", file=sys.stderr)
            else:
                display(result)
    if had_errors:
        return 2
    if args.require_dynarec:
        unchecked = [r["file"] for r in results if
                     "dynarec" not in r["metrics"] or r["dynarec_not_enabled_observed"]]
        if unchecked:
            print("Dynarec not verified enabled in: " + ", ".join(unchecked), file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
