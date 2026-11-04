"""What a probe will cost and how long it will take, before anyone approves it (Phase 1).

Two different numbers, and the difference between them matters:

- **The ceiling** is arithmetic, not a guess: the fixed price the check is given
  (`product_scope.PriceRule`, $2 + $0.35 a page, at least $5) plus the page reviewers' own pot
  (`page_reviewer.review_money`, $1.50 a vendor page by default). The run can never spend more. It
  is roughly twice what a run really spends, so quoting it as "the cost" would stop experiments
  that are affordable.
- **The estimate** is fitted to runs already paid for (`calibration.json`). Only the rows marked
  `picked` — runs that were given a page list, which is the shape a probe uses — are fitted,
  because a whole-set run shares its prompt cache across fifty pages and a four-page run does not.
  Whole-set rows set the upper bound instead.

A probe's own cost is read back from the run afterwards (`actual_from_events`) and appended to the
calibration, so the estimate is wrong by less every time. Until the first real probe the estimate
rests on four picked runs of another job and should be read as a range, not a price.

Nothing here spends anything or calls a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CALIBRATION",
    "Estimate",
    "Fit",
    "Point",
    "actual_from_events",
    "append_point",
    "ceiling_usd",
    "estimate",
    "fit",
    "load_points",
]

DIR: Final = Path(__file__).resolve().parent
CALIBRATION: Final = DIR / "calibration.json"

#: What the money rules say when the app's own modules cannot be imported (a bare checkout, or a
#: test). Kept in step with `product_scope.PriceRule` and `page_reviewer.DEFAULT_PER_PAGE_MICROS`;
#: `ceiling_usd` prefers the real ones and says which it used.
FALLBACK_BASE_USD: Final = 2.0
FALLBACK_PER_PAGE_USD: Final = 0.35
FALLBACK_MINIMUM_USD: Final = 5.0
FALLBACK_REVIEWER_PER_PAGE_USD: Final = 1.50


@dataclass(frozen=True)
class Point:
    """One check that has already been paid for."""

    run: str
    pages: int
    cost_usd: float
    seconds: float
    picked: bool
    note: str = ""


def load_points(path: Path | None = None) -> list[Point]:
    """Every calibration row, in file order."""
    raw = json.loads((path or CALIBRATION).read_text(encoding="utf-8"))
    return [
        Point(
            run=str(row["run"]),
            pages=int(row["pages"]),
            cost_usd=float(row["cost_usd"]),
            seconds=float(row["seconds"]),
            picked=bool(row.get("picked")),
            note=str(row.get("note") or ""),
        )
        for row in raw.get("points") or []
    ]


def append_point(point: Point, path: Path | None = None) -> None:
    """Add a probe's own measured cost and time to the calibration.

    Called after a real probe, so the next estimate is fitted to this job and this page shape
    rather than to another job's picked runs.
    """
    target = path or CALIBRATION
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw.setdefault("points", []).append(
        {
            "run": point.run,
            "pages": point.pages,
            "cost_usd": round(point.cost_usd, 2),
            "seconds": round(point.seconds),
            "picked": point.picked,
            "note": point.note,
        }
    )
    target.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Fit:
    """A straight line through the calibration: a fixed part plus a part for each page."""

    fixed_usd: float
    per_page_usd: float
    fixed_s: float
    per_page_s: float
    points: int
    #: The cheapest and dearest dollars-per-page seen, as an honest spread around the line.
    low_rate: float
    high_rate: float

    def cost(self, pages: int) -> float:
        return max(0.0, self.fixed_usd + self.per_page_usd * pages)

    def seconds(self, pages: int) -> float:
        return max(0.0, self.fixed_s + self.per_page_s * pages)


def _line(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least squares through the points; a flat average when every x is the same."""
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return mean_y, 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    return mean_y - slope * mean_x, slope


def fit(points: list[Point], *, picked_only: bool = True) -> Fit:
    """Fit the cost and the time against the page count.

    `picked_only`: fit to the runs that were given a page list, which is what a probe is. Falls
    back to every row when there are fewer than two of them, because a line needs two points.
    """
    rows = [point for point in points if point.picked] if picked_only else list(points)
    if len(rows) < 2:
        rows = list(points)
    if not rows:
        return Fit(0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0)
    xs = [float(point.pages) for point in rows]
    fixed_usd, per_page_usd = _line(xs, [point.cost_usd for point in rows])
    fixed_s, per_page_s = _line(xs, [point.seconds for point in rows])
    rates = [point.cost_usd / point.pages for point in rows if point.pages]
    return Fit(
        fixed_usd=fixed_usd,
        per_page_usd=per_page_usd,
        fixed_s=fixed_s,
        per_page_s=per_page_s,
        points=len(rows),
        low_rate=min(rates) if rates else 0.0,
        high_rate=max(rates) if rates else 0.0,
    )


def ceiling_usd(pages: int, *, reviewer_per_page_usd: float | None = None) -> tuple[float, str]:
    """The most a probe on `pages` vendor pages could possibly spend, and where the rule came from.

    The fixed price plus the page reviewers' pot. This is the number to compare an account balance
    against before approving a run; the estimate below is the number to expect on the bill.
    """
    try:
        from app.review.assistant import page_reviewer, product_scope

        rule = product_scope.PriceRule()
        price = rule.price(pages) / 1_000_000
        per_page = (
            reviewer_per_page_usd
            if reviewer_per_page_usd is not None
            else page_reviewer.DEFAULT_PER_PAGE_MICROS / 1_000_000
        )
        return price + per_page * max(0, pages), "the repo's own money rules"
    except Exception:  # noqa: BLE001 - a bare checkout still has to be able to price a probe
        price = max(FALLBACK_MINIMUM_USD, FALLBACK_BASE_USD + FALLBACK_PER_PAGE_USD * pages)
        per_page = (
            reviewer_per_page_usd
            if reviewer_per_page_usd is not None
            else FALLBACK_REVIEWER_PER_PAGE_USD
        )
        return price + per_page * max(0, pages), "the written defaults (the app did not import)"


@dataclass(frozen=True)
class Estimate:
    """What one experiment is expected to cost: a probe run `repeats` times, per arm."""

    pages: int
    repeats: int
    arms: int
    cost_usd: float
    low_usd: float
    high_usd: float
    seconds: float
    ceiling_usd: float
    ceiling_from: str
    fitted_on: int
    #: True when the page count is outside the range of the picked runs the line was fitted to, so
    #: the figure is an extrapolation and the band is wide on purpose.
    extrapolated: bool = False

    @property
    def runs(self) -> int:
        return self.repeats * self.arms

    @property
    def total_cost_usd(self) -> float:
        return self.cost_usd * self.runs

    @property
    def total_low_usd(self) -> float:
        return self.low_usd * self.runs

    @property
    def total_high_usd(self) -> float:
        return self.high_usd * self.runs

    @property
    def total_ceiling_usd(self) -> float:
        return self.ceiling_usd * self.runs

    @property
    def total_seconds(self) -> float:
        """Runs go one at a time: never two paid jobs at once on one account."""
        return self.seconds * self.runs


def estimate(
    pages: int,
    *,
    repeats: int = 1,
    arms: int = 1,
    points: list[Point] | None = None,
    reviewer_per_page_usd: float | None = None,
) -> Estimate:
    """Price one experiment. Every number is per run except the `total_*` properties."""
    rows = points if points is not None else load_points()
    line = fit(rows)
    whole = fit(rows, picked_only=False)
    ceiling, where = ceiling_usd(pages, reviewer_per_page_usd=reviewer_per_page_usd)

    picked = [point.pages for point in rows if point.picked]
    beyond = bool(picked) and pages > max(picked)

    middle = min(line.cost(pages), ceiling)
    low = min(middle, line.low_rate * pages)
    seconds = line.seconds(pages)
    if beyond:
        # Past the picked runs' page counts the picked line runs away from the evidence: a probe on
        # thirty pages cannot sensibly cost or take more than the whole-set runs of fifty did. The
        # whole-set line is the sanity bound, and the band is widened down to it rather than the
        # middle being quietly moved.
        low = min(low, whole.cost(pages))
        seconds = min(seconds, max(whole.seconds(pages), 0.0))
    return Estimate(
        pages=pages,
        repeats=max(1, repeats),
        arms=max(1, arms),
        cost_usd=middle,
        low_usd=max(0.0, low),
        high_usd=min(ceiling, max(middle, line.high_rate * pages)),
        seconds=seconds,
        ceiling_usd=ceiling,
        ceiling_from=where,
        fitted_on=line.points,
        extrapolated=beyond,
    )


def actual_from_events(events: list[dict[str, Any]], *, seconds: float) -> Point:
    """What a probe really cost, read from the run's own usage events.

    The server emits a `usage` event after every call carrying the spend so far, and a
    `message_end` carrying the stored answer's `cost_usd`. The last of either is the run's own
    figure — this never re-prices tokens itself, so a rate-card change cannot make an old probe
    look cheaper than the bill said.
    """
    cost = 0.0
    pages = 0
    for event in events:
        name = str(event.get("event") or "")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if name in {"usage", "message_end"}:
            for key in ("cost_usd", "spent_usd", "total_usd"):
                value = data.get(key)
                if isinstance(value, int | float):
                    cost = max(cost, float(value))
        if name == "scope":
            scope_pages = data.get("pages")
            if isinstance(scope_pages, list):
                pages = max(pages, len(scope_pages))
    return Point(run="probe", pages=pages, cost_usd=cost, seconds=seconds, picked=True)
