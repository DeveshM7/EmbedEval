"""Bar chart: model performance on EmbedEval benchmark (pass rate out of 8)."""

import matplotlib.pyplot as plt
import matplotlib.ticker as mtick

models = ["Claude Opus 4.6", "GPT-5.4", "Gemini 2.5 Pro", "Qwen 3 Coder", "DeepSeek V3"]
passed = [6, 6, 5, 4, 3]
total = 8
pct = [p / total * 100 for p in passed]

colors = ["#6A4CFF", "#10A37F", "#4285F4", "#FF6B35", "#EFDA17"]

fig, ax = plt.subplots(figsize=(10, 6))
bars = ax.bar(models, pct, color=colors, width=0.55, edgecolor="white", linewidth=1.2)

# Label each bar with "X/8"
for bar, p in zip(bars, passed):
    ax.text(
        bar.get_x() + bar.get_width() / 2,
        bar.get_height() + 1.5,
        f"{p/total * 100}",
        ha="center",
        va="bottom",
        fontsize=16,
        fontweight="bold",
    )

ax.set_ylabel("Resolve Rate (%)", fontsize=14)
ax.set_title("EmbedEval Benchmark — Model Performance", fontsize=18, fontweight="bold", pad=16)
ax.set_ylim(0, 105)
ax.yaxis.set_major_formatter(mtick.PercentFormatter())
ax.tick_params(axis="both", labelsize=13)
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)

plt.tight_layout()
plt.savefig("charts/performance.png", dpi=200, bbox_inches="tight")
print("Saved charts/performance.png")
plt.show()
