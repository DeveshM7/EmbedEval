"""Grouped bar chart: API calls (steps) per instance, by model."""

import json
import glob
import os

import matplotlib.pyplot as plt
import numpy as np

OUTPUTS = os.path.join(os.path.dirname(__file__), "..", "outputs")

MODEL_DIRS = {
    "Claude Opus 4.6": "anthropic_claude-opus-4-6",
    "GPT-5.4": "openai_gpt-5.4",
    "Gemini 2.5 Flash": "gemini_gemini-2.5-flash",
    "DeepSeek V3": "together_ai_deepseek-ai_DeepSeek-V3",
}

COLORS = {
    "Claude Opus 4.6": "#6A4CFF",
    "GPT-5.4": "#10A37F",
    "Gemini 2.5 Flash": "#4285F4",
    "DeepSeek V3": "#FF6B35",
}

# Collect data: {model: {instance_short: api_calls}}
data = {}
all_instances = set()

for label, dirname in MODEL_DIRS.items():
    data[label] = {}
    for path in sorted(glob.glob(os.path.join(OUTPUTS, dirname, "*.trajectory.json"))):
        instance = os.path.basename(path).replace(".trajectory.json", "")
        short = instance.replace("zephyr__zephyr-", "#")
        traj = json.load(open(path))
        data[label][short] = traj["info"]["model_stats"]["api_calls"]
        all_instances.add(short)

instances = sorted(all_instances)
model_names = list(MODEL_DIRS.keys())
n_models = len(model_names)
n_instances = len(instances)

x = np.arange(n_instances)
bar_width = 0.18

fig, ax = plt.subplots(figsize=(14, 6))

for i, model in enumerate(model_names):
    vals = [data[model].get(inst, 0) for inst in instances]
    offset = (i - (n_models - 1) / 2) * bar_width
    bars = ax.bar(x + offset, vals, bar_width, label=model, color=COLORS[model], edgecolor="white", linewidth=0.8)
    # Small value labels on top of each bar
    for bar, v in zip(bars, vals):
        if v > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.5,
                str(v),
                ha="center",
                va="bottom",
                fontsize=8,
                fontweight="bold",
            )

ax.set_xlabel("Instance (PR number)", fontsize=13)
ax.set_ylabel("API Calls (steps)", fontsize=13)
ax.set_title("EmbedEval Benchmark — Agent Steps per Instance", fontsize=18, fontweight="bold", pad=16)
ax.set_xticks(x)
ax.set_xticklabels(instances, fontsize=11)
ax.legend(fontsize=12, loc="upper left")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.tick_params(axis="both", labelsize=11)

plt.tight_layout()
plt.savefig("charts/steps_per_instance.png", dpi=200, bbox_inches="tight")
print("Saved charts/steps_per_instance.png")
plt.show()
