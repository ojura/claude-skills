#!/usr/bin/env python3
"""
Toaster Pop-Time Performance Study: Chart Generation
All data is fabricated for benchmarking purposes. Any resemblance
to real toaster firmware is purely coincidental.
"""
import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

OUT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Shared style
# ---------------------------------------------------------------------------
plt.rcParams.update({
    'font.family': 'DejaVu Sans',
    'font.size': 10,
    'axes.titlesize': 12,
    'axes.titleweight': 'bold',
    'axes.spines.top': False,
    'axes.spines.right': False,
    'figure.dpi': 150,
})

COLORS = ['#2e86ab', '#e84855', '#f4a261', '#2a9d8f', '#8338ec', '#fb8500']


def save(fig, name):
    """Save as both PNG (150 dpi) and PDF."""
    png_path = os.path.join(OUT, name + '.png')
    pdf_path = os.path.join(OUT, name + '.pdf')
    fig.savefig(png_path, dpi=150, bbox_inches='tight')
    fig.savefig(pdf_path, bbox_inches='tight')
    print(f'  Wrote {png_path}')
    print(f'  Wrote {pdf_path}')
    plt.close(fig)


# ---------------------------------------------------------------------------
# 1. headline_speedups.png  -- horizontal bar chart of top wins
# ---------------------------------------------------------------------------
def plot_headline_speedups():
    labels = [
        'Coil Pre-energisation',
        'Adaptive Darkness Lookup Table',
        'Spring Constant Re-tuning',
        'Thermal Throttle Bypass',
        'Crumb Tray Aerodynamics',
        'Bread Slot Width Auto-calibration',
        'Mains Cycle Phase Alignment',
        'Latch Mechanism Debounce Removal',
    ]
    speedups = [3.81, 2.94, 2.47, 1.98, 1.72, 1.55, 1.31, 1.14]

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.barh(labels, speedups, color=COLORS[:len(labels)], edgecolor='white', linewidth=0.5)
    ax.axvline(1.0, color='black', linewidth=0.8, linestyle='--', label='Baseline (1.0x)')
    ax.set_xlabel('Speedup (x) relative to stock firmware')
    ax.set_title('Headline Speedups: Top 8 Toaster Optimisations')
    ax.legend(loc='lower right')
    for bar, val in zip(bars, speedups):
        ax.text(val + 0.03, bar.get_y() + bar.get_height() / 2,
                f'{val:.2f}x', va='center', fontsize=9)
    ax.set_xlim(0, 4.5)
    fig.tight_layout()
    save(fig, 'headline_speedups')


# ---------------------------------------------------------------------------
# 2. throughput_scaling.png  -- line chart: slices/min vs concurrency (bread slots)
# ---------------------------------------------------------------------------
def plot_throughput_scaling():
    slots = np.array([1, 2, 4, 6, 8, 10, 12, 16])

    # Theoretical linear
    linear = slots * 4.0

    # Stock firmware: saturates early due to coil contention
    stock = np.array([4.0, 7.6, 12.1, 14.8, 15.3, 15.5, 15.6, 15.7])

    # Optimised: better coil scheduling
    optimised = np.array([4.0, 7.9, 15.2, 21.8, 27.1, 31.4, 34.0, 36.2])

    # Coil pre-energisation + LUT: near-linear to 8 slots
    full_opt = np.array([4.0, 8.0, 16.0, 23.5, 30.8, 36.1, 39.9, 42.3])

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(slots, linear, 'k--', linewidth=1, label='Linear ideal', alpha=0.4)
    ax.plot(slots, stock, 'o-', color=COLORS[1], label='Stock firmware')
    ax.plot(slots, optimised, 's-', color=COLORS[0], label='Tier 1 optimisations')
    ax.plot(slots, full_opt, '^-', color=COLORS[3], label='Tier 1 + Tier 2')
    ax.set_xlabel('Active bread slots')
    ax.set_ylabel('Throughput (slices / min)')
    ax.set_title('Toaster Throughput Scaling vs Slot Count')
    ax.legend()
    ax.set_xticks(slots)
    fig.tight_layout()
    save(fig, 'throughput_scaling')


# ---------------------------------------------------------------------------
# 3. latency_histogram.png  -- grouped bar: pop-time latency distribution buckets
# ---------------------------------------------------------------------------
def plot_latency_histogram():
    buckets = ['<80 ms', '80-100 ms', '100-120 ms', '120-150 ms', '150-200 ms', '>200 ms']
    x = np.arange(len(buckets))
    width = 0.35

    stock_counts    = [2, 18, 34, 29, 12, 5]
    optimised_counts = [5, 41, 38, 12, 3,  1]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(x - width/2, stock_counts,    width, label='Stock firmware', color=COLORS[1], alpha=0.85)
    ax.bar(x + width/2, optimised_counts, width, label='Tier 1 optimised', color=COLORS[0], alpha=0.85)
    ax.set_xticks(x)
    ax.set_xticklabels(buckets)
    ax.set_xlabel('Pop-time latency bucket')
    ax.set_ylabel('Count of trials (out of 100)')
    ax.set_title('Pop-Time Latency Distribution: Stock vs Optimised')
    ax.legend()
    fig.tight_layout()
    save(fig, 'latency_histogram')


# ---------------------------------------------------------------------------
# 4. before_after.png  -- grouped bar: mean pop-time before/after across bread types
# ---------------------------------------------------------------------------
def plot_before_after():
    bread_types = [
        'White (thin)', 'White (thick)', 'Wholegrain', 'Rye', 'Sourdough',
        'Bagel half', 'English Muffin', 'Crumpet',
    ]
    before_ms = np.array([142, 178, 195, 201, 218, 230, 244, 261])
    after_ms  = np.array([ 58,  74,  83,  88,  95, 101, 112, 124])

    x = np.arange(len(bread_types))
    width = 0.38

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(x - width/2, before_ms, width, label='Before (stock)', color=COLORS[1], alpha=0.85)
    ax.bar(x + width/2, after_ms,  width, label='After (Tier 1+2)', color=COLORS[3], alpha=0.85)
    ax.set_xticks(x)
    ax.set_xticklabels(bread_types, rotation=25, ha='right', fontsize=9)
    ax.set_ylabel('Mean pop-time (ms)')
    ax.set_title('Before / After Mean Pop-Time by Bread Type')
    ax.legend()
    fig.tight_layout()
    save(fig, 'before_after')


# ---------------------------------------------------------------------------
# 5. cost_breakdown.png  -- stacked bar: where pop-time is spent, per phase
# ---------------------------------------------------------------------------
def plot_cost_breakdown():
    phases = ['Mains\nSync', 'Coil\nEnergise', 'Thermal\nSoak', 'Darkness\nSample',
              'Spring\nRelease', 'Latch\nClear']
    stock_ms    = [12,  45, 38, 22, 18,  7]
    optimised_ms = [ 3,  11, 36,  5,  7,  3]

    x = np.arange(len(phases))
    fig, ax = plt.subplots(figsize=(8, 5))

    bottom_s = np.zeros(len(phases))
    bottom_o = np.zeros(len(phases))
    # We stack by component category, simplified: show stock as one stacked group, optimised as another
    # Actually do side-by-side stacked to make it interesting
    width = 0.35
    for i, (s, o) in enumerate(zip(stock_ms, optimised_ms)):
        ax.bar(x[i] - width/2, s, width, bottom=0, color=COLORS[i % len(COLORS)], alpha=0.7)
        ax.bar(x[i] + width/2, o, width, bottom=0, color=COLORS[i % len(COLORS)], alpha=1.0,
               edgecolor='black', linewidth=0.5)

    ax.set_xticks(x)
    ax.set_xticklabels(phases)
    ax.set_ylabel('Mean time (ms)')
    ax.set_title('Pop-Time Cost Breakdown by Phase: Stock (left) vs Optimised (right)')

    # Legend patches
    stock_patch = mpatches.Patch(facecolor='grey', alpha=0.5, label='Stock firmware (faded)')
    opt_patch   = mpatches.Patch(facecolor='grey', alpha=1.0, label='Optimised (solid)')
    ax.legend(handles=[stock_patch, opt_patch])
    fig.tight_layout()
    save(fig, 'cost_breakdown')


# ---------------------------------------------------------------------------
# 6. the_cliff.png  -- line chart showing the dramatic performance cliff
#    (what happens when bread moisture exceeds the thermal probe's ADC range)
# ---------------------------------------------------------------------------
def plot_the_cliff():
    moisture_pct = np.linspace(0, 60, 300)

    # Pop-time is stable until moisture ~38%, then the ADC overflows
    # and the firmware falls back to a 500 ms watchdog timeout
    def pop_time(m):
        base = 95 + 0.4 * m
        cliff_mask = m >= 38.5
        result = base.copy()
        result[cliff_mask] = 520 + 30 * np.random.default_rng(42).standard_normal(cliff_mask.sum())
        return result

    rng = np.random.default_rng(7)
    pt_stock = pop_time(moisture_pct) + rng.standard_normal(len(moisture_pct)) * 4
    pt_patched = 95 + 0.38 * moisture_pct + rng.standard_normal(len(moisture_pct)) * 3

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(moisture_pct, pt_stock,   color=COLORS[1], linewidth=1.5, label='Stock firmware')
    ax.plot(moisture_pct, pt_patched, color=COLORS[3], linewidth=1.5, label='ADC range patch (Ch. 5)')
    ax.axvline(38.5, color='black', linestyle=':', linewidth=1.2, label='Cliff at 38.5% moisture')
    ax.annotate('ADC overflow\nwatchdog fires', xy=(38.5, 520), xytext=(45, 480),
                arrowprops=dict(arrowstyle='->', color='black'), fontsize=9)
    ax.set_xlabel('Bread moisture content (%)')
    ax.set_ylabel('Pop-time (ms)')
    ax.set_title('The Cliff: Pop-Time vs Bread Moisture (stock vs patched)')
    ax.legend()
    ax.set_ylim(60, 580)
    fig.tight_layout()
    save(fig, 'the_cliff')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    print('Generating toaster performance charts...')
    plot_headline_speedups()
    plot_throughput_scaling()
    plot_latency_histogram()
    plot_before_after()
    plot_cost_breakdown()
    plot_the_cliff()
    print('Done. All six charts written.')
