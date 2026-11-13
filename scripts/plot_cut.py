"""Draw, for questions cut by the caller, how long the agent and its LLM calls
kept going under each way of stopping, as a PNG for the README. One row per
trace; the full span-by-span waterfall is the HTML next to each trace.

  python scripts/plot_cut.py docs/img/cut.png \
      何もしない=traces/disconnect/gpu3-cut.json \
      "StopRuntimeSessionで止める=traces/disconnect/gpu4-cut-before-fix.json" \
      "＋ 処理中の要求を待たない=traces/disconnect/gpu4-cut-llm.json"

needs the "plot" extra. Times are seconds from when AgentCore received the
question (the start of the agent Runtime's AgentCore.Runtime.Invoke), so every
time is on the AWS side's clocks; the caller's clock can be off by up to a
second (DESIGN §7 20). The cut is placed by the caller's caller.cut_after_s,
a duration on its own clock, after that start.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Same colors as tracing/viewer/waterfall.py and plot_breakdown.py.
_AGENT = "#2a78d6"
_LLM = "#eda100"
_CUT = "#d0453b"
_TEXT = "#0b0b0b"
_MUTED = "#52514e"
_GRID = "#e4e3df"
_SURFACE = "#fcfcfb"
# The service name of the agent Runtime, as AgentCore reports it.
_AGENT_RUNTIME = "agent_latency_tracer_agent."
_installed = {f.name for f in matplotlib.font_manager.fontManager.ttflist}
plt.rcParams["font.family"] = [f for f in ("Yu Gothic", "Meiryo", "Noto Sans CJK JP", "IPAexGothic")
                               if f in _installed] + ["sans-serif"]


def _row(path: Path) -> dict:
    spans = json.loads(path.read_text(encoding="utf-8"))["spans"]

    def times(service: str, prefix: str) -> list[tuple[float, float]]:
        return sorted((s["start_time_unix_nano"] / 1e9, s["end_time_unix_nano"] / 1e9) for s in spans
                      if s["resource"].get("service.name") == service and s["name"].startswith(prefix))

    received = min(s["start_time_unix_nano"] / 1e9 for s in spans
                   if s["name"] == "AgentCore.Runtime.Invoke" and s["resource"].get("service.name", "").startswith(_AGENT_RUNTIME))
    cut_after, = (s["attributes"]["caller.cut_after_s"] for s in spans if s["name"] == "InvokeAgentRuntime")
    (agent_start, agent_end), = times("agent", "invoke_agent")
    return {
        "cut": cut_after,
        "agent": (agent_start - received, agent_end - received),
        "llm": [(s - received, e - received) for s, e in times("agent", "chat ")],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("png", type=Path)
    parser.add_argument("rows", nargs="+", help="label=trace.json, top to bottom")
    args = parser.parse_args()
    rows = [(label, _row(Path(path))) for label, path in (r.split("=", 1) for r in args.rows)]
    end = max(row["agent"][1] for _, row in rows)

    fig, ax = plt.subplots(figsize=(9, 0.75 * len(rows) + 0.9), dpi=150)
    fig.patch.set_facecolor(_SURFACE)
    ax.set_facecolor(_SURFACE)
    for i, (label, row) in enumerate(rows):
        y = len(rows) - 1 - i
        start, stop = row["agent"]
        ax.barh(y, stop - start, left=start, height=0.56, color=_AGENT, alpha=0.25, edgecolor="none")
        for s, e in row["llm"]:
            ax.barh(y, e - s, left=s, height=0.3, color=_LLM, edgecolor=_SURFACE, linewidth=1)
        ax.plot([row["cut"]] * 2, [y - 0.38, y + 0.38], color=_CUT, linewidth=2)
        ax.text(-0.6, y, label, va="center", ha="right", fontsize=9, color=_TEXT)

    handles = [
        plt.Rectangle((0, 0), 1, 1, color=_AGENT, alpha=0.25),
        plt.Rectangle((0, 0), 1, 1, color=_LLM),
        plt.Line2D([0], [0], color=_CUT, linewidth=2),
    ]
    ax.legend(handles, ["エージェントの処理", "LLMの生成", "利用者が切った"], loc="lower center",
              bbox_to_anchor=(0.5, 1.0), ncol=3, frameon=False, fontsize=8.5)
    ax.set_yticks([])
    ax.set_xlim(0, end * 1.02)
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_xlabel("AgentCoreが質問を受けてからの秒数", fontsize=9, color=_MUTED)
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
