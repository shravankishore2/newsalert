"""WCAG contrast audit for QuantRadar's colour tokens (light + dark), read from web/src/index.css.

Text pairs must reach 4.5:1 (AA, normal text). Card borders must reach 3:1 against the page
(WCAG 1.4.11, non-text contrast). Card backgrounds vs page are reported for information.
Run: python3 web/scripts/contrast.py   (exit code 1 if anything fails)
"""
import re
import sys
from pathlib import Path

css = Path(__file__).resolve().parents[1].joinpath("src/index.css").read_text()


def block(pattern: str) -> dict:
    m = re.search(pattern, css, re.S)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", m.group(1))) if m else {}


light = block(r":root \{(.*?)\n\}")
light.update({k: v for k, v in (block(r":root \{(.*?)\n\}")).items()})
dark = dict(light)
dark.update(block(r':root\[data-theme="dark"\] \{(.*?)\n\}'))


def rgb(h):
    return [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]


def lum(h):
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in rgb(h)]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(fg, bg, alpha):
    """fg over bg at alpha (color-mix(in srgb, fg a%, transparent) painted on bg)."""
    f, b = rgb(fg), rgb(bg)
    return "#" + "".join(f"{round((alpha * x + (1 - alpha) * y) * 255):02x}" for x, y in zip(f, b))


TEXT = [
    # (text token, background token, where)
    *[(t, b, "body/secondary/muted text") for t in ("text-primary", "text-secondary", "text-muted")
      for b in ("bg", "surface-1", "surface-2", "pos-bg", "neg-bg", "neu-bg")],
    ("pos-ink", "bg", "Positive column heading"), ("neg-ink", "bg", "Negative column heading"),
    ("pos-ink", "pos-bg", "Positive tab (phone)"), ("neg-ink", "neg-bg", "Negative tab / problem pill"),
    ("link", "surface-1", "links"), ("link", "pos-bg", "links on cards"), ("link", "neg-bg", "links on cards"),
    ("link", "neu-bg", "links on cards"), ("link", "surface-2", "links in popover"),
    ("accent-ink", "accent-strong", "selected view button / primary button"), ("warn-text", "warn-bg", "warning pill"),
    ("replay-ink", "replay-bg", "replay banner"), ("good-ink", "surface-1", "Up label on price cards"),
    ("critical-ink", "surface-1", "Down label on price cards"), ("good-ink", "bg", "Up label (dashed cards)"),
    ("critical-ink", "bg", "Down label (dashed cards)"),
]
FIXED = [("#ffffff", "impact-up", "impact badge Up"), ("#ffffff", "impact-down", "impact badge Down"),
         ("#ffffff", "impact-none", "impact badge None")]
BORDERS = [("pos-border", "bg"), ("neg-border", "bg"), ("neu-border", "bg"), ("border-strong", "bg")]

fails = 0
for mode, t in (("light", light), ("dark", dark)):
    print(f"== {mode}")
    rows = []
    for fg, bg, where in TEXT:
        rows.append((where, f"{fg} on {bg}", ratio(t[fg], t[bg]), 4.5))
    for fg, bg, where in FIXED:
        rows.append((where, f"white on {bg}", ratio(fg, t[bg]), 4.5))
    for tone in ("pos-bg", "neg-bg", "neu-bg"):
        badge = mix(t["accent"], t[tone], 0.12)
        rows.append(("event badge on card", f"text-primary on accent@12% over {tone}", ratio(t["text-primary"], badge), 4.5))
        chip = mix(t["surface-2"], t[tone], 0.80)
        rows.append(("chip on card", f"text-secondary on chip over {tone}", ratio(t["text-secondary"], chip), 4.5))
    for b, page in BORDERS:
        rows.append(("card border vs page", f"{b} vs {page}", ratio(t[b], t[page]), 3.0))
    for where, pair, r, need in rows:
        ok = r >= need
        fails += not ok
        print(f"  {'PASS' if ok else 'FAIL'} {r:5.2f}:1 (need {need}) {pair:44s} {where}")
    for tone in ("pos-bg", "neg-bg", "neu-bg"):
        print(f"  info  {ratio(t[tone], t['bg']):5.2f}:1 {tone} vs page background")
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
