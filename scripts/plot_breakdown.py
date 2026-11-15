"""Draw one question's time breakdown as a PNG, one lane per part (the
agent, the LLM calls, the MCP tools, the Memory calls), from a trace under
traces/. For the README; the full span-by-span waterfall is the PNG next to
the trace (Jaeger's, README).

  python scripts/plot_breakdown.py traces/base/gpu4-q2.json docs/img/one-question.png

needs the "plot" extra. Times are taken from the agent's own spans, so the
caller's clock (which can be off by up to a second, DESIGN §7 20) is not mixed in.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tracing.published import from_otlp

_LANES = [
    ("エージェント全体", "#2a78d6"),
    ("LLM", "#eda100"),
    ("ツール(MCP)", "#1baf7a"),
    ("Memory", "#898781"),
]
_TEXT = "#0b0b0b"
_MUTED = "#52514e"
_GRID = "#e4e3df"
_SURFACE = "#fcfcfb"
# The first Japanese font that is installed (Windows, then common Linux ones).
_installed = {f.name for f in matplotlib.font_manager.fontManager.ttflist}
plt.rcParams["font.family"] = [f for f in ("Yu Gothic", "Meiryo", "Noto Sans CJK JP", "IPAexGothic")
                               if f in _installed] + ["sans-serif"]


def _lane(span: dict) -> int | None:
    service = span["resource"].get("service.name")
    name = span["name"]
    if service != "agent":
        return None
    if name.startswith("invoke_agent"):
        return 0
    if name.startswith("chat "):
        return 1
    if name.startswith("execute_tool "):
        return 2
    if name.startswith("BedrockAgentCore."):
        return 3
    return None


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float, int]]:
    """Overlapping intervals (tools called at the same time) as one bar, with how many."""
    merged: list[list] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] += 1
        else:
            merged.append([start, end, 1])
    return [tuple(m) for m in merged]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path)
    parser.add_argument("png", type=Path)
    args = parser.parse_args()

    spans = from_otlp(json.loads(args.trace.read_text(encoding="utf-8")))
    lanes: dict[int, list[tuple[float, float]]] = {i: [] for i in range(len(_LANES))}
    for span in spans:
        lane = _lane(span)
        if lane is not None:
            lanes[lane].append((span["start_time_unix_nano"] / 1e9, span["end_time_unix_nano"] / 1e9))
    t0 = min(start for start, _ in lanes[0])
    bars = {i: _merge([(s - t0, e - t0) for s, e in iv]) for i, iv in lanes.items()}
    total = sum(e - s for s, e, _ in bars[0])
    sums = {i: sum(e - s for s, e, _ in bars[i]) for i in bars}

    fig, ax = plt.subplots(figsize=(9, 2.9), dpi=150)
    fig.patch.set_facecolor(_SURFACE)
    ax.set_facecolor(_SURFACE)
    for i, (label, color) in enumerate(_LANES):
        y = len(_LANES) - 1 - i
        for start, end, count in bars[i]:
            # Memory calls take about 0.1 s; keep them visible at this scale.
            short = end - start < 0.3
            ax.barh(y, max(end - start, 0.25), left=start, height=0.56, color=color,
                    edgecolor="none" if short else _SURFACE, linewidth=1)
            if i in (1, 2) and end - start >= 0.4:
                text = f"{end - start:.1f}秒" + (f"({count}つ同時)" if count > 1 else "")
                inside = end - start > 1.5
                ax.text(start + (end - start) / 2 if inside else end + 0.25, y, text,
                        va="center", ha="center" if inside else "left", fontsize=8.5, color=_TEXT)
        ax.text(-0.6, y, f"{label}\n計{sums[i]:.1f}秒", va="center", ha="right", fontsize=9, color=_TEXT)

    ax.set_yticks([])
    ax.set_xlim(0, total * 1.02)
    ax.set_ylim(-0.6, len(_LANES) - 0.4)
    ax.set_xlabel("質問を受けてからの秒数", fontsize=9, color=_MUTED)
    ax.tick_params(axis="x", colors=_MUTED, labelsize=8.5)
    ax.grid(axis="x", color=_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(_GRID)
    fig.tight_layout()
    args.png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.png, facecolor=_SURFACE)


if __name__ == "__main__":
    main()
