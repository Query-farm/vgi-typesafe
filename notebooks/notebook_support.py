# Copyright 2026 Query Farm LLC - https://query.farm

"""Small presentation helpers for the TypeSafe notebook."""

from pathlib import Path

import matplotlib.pyplot as plt
import seaborn as sns
from IPython.display import Markdown, display

COLORS = {"same": "#267358", "changed": "#c57b40", "reference": "#456fba"}


def theme() -> None:
    """Apply the shared notebook visual style."""
    sns.set_theme(style="whitegrid", context="notebook", font="DejaVu Sans")
    plt.rcParams.update(
        {
            "figure.dpi": 130,
            "text.parse_math": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "grid.color": "#e2e8e4",
        }
    )


def prose(text: str) -> None:
    """Show a result-derived interpretation."""
    display(Markdown(text))


def finish(
    fig: plt.Figure,
    title: str,
    subtitle: str,
    filename: str,
    *,
    bottom: float = 0.08,
    source: str = "Synthetic keyword mock · VGI TypeSafe Git c1a1214 · No real model inference",
) -> None:
    """Label, embed and save a chart for visual review."""
    fig.suptitle(title, x=0.08, y=0.98, ha="left", fontsize=18, weight="bold", color="#253d31")
    fig.text(0.08, 0.90, subtitle, ha="left", fontsize=11, color="#52675a")
    fig.text(
        0.08,
        0.025,
        source,
        ha="left",
        fontsize=9,
        color="#52675a",
    )
    fig.tight_layout(rect=(0.02, bottom, 1, 0.83))
    directory = Path(__file__).resolve().parent / "validation/charts"
    directory.mkdir(parents=True, exist_ok=True)
    fig.savefig(directory / filename, dpi=150)
    plt.show()
