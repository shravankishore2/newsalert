import subprocess
import sys


def test_ui_colours_meet_wcag_aa():
    """web/scripts/contrast.py checks every text/background pair (4.5:1) and card borders (3:1)
    in light and dark themes, straight from web/src/index.css."""
    r = subprocess.run([sys.executable, "web/scripts/contrast.py"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:]
