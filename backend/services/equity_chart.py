"""
SmAttaker — Equity Curve Chart Generator
============================================
Renders a dark/gold-themed equity curve PNG — same visual identity as
the web dashboard (backend/templates/dashboard.html's Tailwind color
config: void #05070D, card #10162A, gold #D4AF37, win #22C55E,
loss #EF4444). Used by trade_notify.py to send a fresh chart on every
trade completion, covering the FULL trade history (old + new), not
just the trade that just closed — exactly what makes it an "equity
curve" rather than a single-trade P&L snippet.

matplotlib runs headless (Agg backend) — no display, no network, safe
in any server environment.
"""
import io
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

VOID = "#05070D"
CARD = "#10162A"
LINE = "#1E2740"
GOLD = "#D4AF37"
GOLD_LIGHT = "#F0D683"
WIN = "#22C55E"
LOSS = "#EF4444"
MUTED = "#8B93A8"


def render_equity_curve(
    trades: list,
    title: str = "Equity Curve",
    subtitle: Optional[str] = None,
    starting_balance: float = 0.0,
) -> bytes:
    """
    `trades` — completed Trade ORM objects (must have exit_time, pnl_usd,
    is_winner), any order — sorted here by exit_time.

    Returns PNG bytes ready to send via Telegram's send_photo.
    """
    closed = sorted(
        [t for t in trades if t.exit_time and t.pnl_usd is not None],
        key=lambda t: t.exit_time,
    )

    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=160)
    fig.patch.set_facecolor(VOID)
    ax.set_facecolor(CARD)

    if not closed:
        ax.text(
            0.5, 0.5, "No completed trades yet",
            ha="center", va="center", color=MUTED, fontsize=16, transform=ax.transAxes,
        )
        _style_axes(ax, title, subtitle)
        return _finalize(fig)

    xs = [t.exit_time for t in closed]
    cumulative = starting_balance
    ys = []
    for t in closed:
        cumulative += float(t.pnl_usd)
        ys.append(cumulative)

    # Fill color depends on overall trend: gold gradient line always,
    # fill tinted green/red based on final vs starting balance.
    final_positive = ys[-1] >= starting_balance
    fill_color = WIN if final_positive else LOSS

    ax.plot(xs, ys, color=GOLD, linewidth=2.4, zorder=3, solid_capstyle="round")
    ax.fill_between(xs, ys, starting_balance, color=fill_color, alpha=0.12, zorder=1)
    ax.axhline(starting_balance, color=MUTED, linewidth=0.9, linestyle="--", alpha=0.5, zorder=2)

    # Mark wins/losses as small dots along the curve
    win_xs = [t.exit_time for t in closed if t.is_winner]
    win_ys = [ys[i] for i, t in enumerate(closed) if t.is_winner]
    loss_xs = [t.exit_time for t in closed if t.is_winner is False]
    loss_ys = [ys[i] for i, t in enumerate(closed) if t.is_winner is False]
    ax.scatter(win_xs, win_ys, color=WIN, s=18, zorder=4, edgecolors=VOID, linewidths=0.6)
    ax.scatter(loss_xs, loss_ys, color=LOSS, s=18, zorder=4, edgecolors=VOID, linewidths=0.6)

    # Peak marker
    peak_idx = ys.index(max(ys))
    ax.annotate(
        f"Peak: ${ys[peak_idx]:,.2f}",
        xy=(xs[peak_idx], ys[peak_idx]), xytext=(0, 14), textcoords="offset points",
        ha="center", color=GOLD_LIGHT, fontsize=9, fontweight="bold",
    )

    final_delta = ys[-1] - starting_balance
    delta_pct = (final_delta / starting_balance * 100) if starting_balance else 0.0
    delta_color = WIN if final_delta >= 0 else LOSS
    ax.text(
        0.99, 0.03,
        f"{'▲' if final_delta >= 0 else '▼'} {final_delta:+,.2f} USD ({delta_pct:+.1f}%)",
        transform=ax.transAxes, ha="right", va="bottom",
        color=delta_color, fontsize=13, fontweight="bold",
    )

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.autofmt_xdate(rotation=25)

    _style_axes(ax, title, subtitle)
    return _finalize(fig)


def _style_axes(ax, title: str, subtitle: Optional[str]):
    ax.set_title(title, color="white", fontsize=17, fontweight="bold", pad=(28 if subtitle else 14), loc="left")
    if subtitle:
        ax.text(0.0, 1.06, subtitle, transform=ax.transAxes, color=MUTED, fontsize=10.5)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(True, color=LINE, linewidth=0.7, alpha=0.6)
    for spine in ax.spines.values():
        spine.set_color(LINE)
    ax.set_ylabel("Balance (USD)", color=MUTED, fontsize=10)


def _finalize(fig) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
