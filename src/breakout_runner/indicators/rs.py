"""R-rules: an approximate RS percentile. There is no bulk 9-month return, hence 3 windows."""

from __future__ import annotations

from dataclasses import dataclass

from ..client.endpoints import HeatmapRow
from ..config.models import Rs
from ..data.bars import WeeklyBar

METHOD = "approx_3_windows"
AVAILABLE = "available"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RsResult:
    rs: float | None
    status: str
    method: str = METHOD
    universe_size: int = 0


def weighted_score(m3: float | None, m6: float | None, m12: float | None, cfg: Rs) -> float | None:
    if None in (m3, m6, m12):
        return None
    w = cfg.weights
    return w.m3 * m3 + w.m6 * m6 + w.m12 * m12


def own_score(bars: list[WeeklyBar], t: int, cfg: Rs) -> float | None:
    """I-13: percent returns from weekly closes, matching the heatmap's percent fields."""

    def ret(weeks: int) -> float | None:
        if t - weeks < 0 or not bars[t - weeks].close:
            return None
        return (bars[t].close / bars[t - weeks].close - 1) * 100

    windows = cfg.window_weeks
    return weighted_score(ret(windows.m3), ret(windows.m6), ret(windows.m12), cfg)


def universe_scores(rows: list[HeatmapRow], cfg: Rs) -> list[float]:
    scores = (weighted_score(r.change_pct_3m, r.change_pct_6m, r.change_pct_1y, cfg) for r in rows)
    return [s for s in scores if s is not None]  # null windows leave the universe count


def percentile(candidate: float | None, scores: list[float] | None) -> RsResult:
    """R-03. Missing inputs are `unavailable`, which downstream counts as unknown, not failed."""
    if candidate is None or not scores:
        return RsResult(None, UNAVAILABLE)
    below = sum(1 for s in scores if s < candidate)
    return RsResult(100 * below / len(scores), AVAILABLE, universe_size=len(scores))
