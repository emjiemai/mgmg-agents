"""Draws one Vega-Lite chart in its own short-lived process (2026-11-17, documenter.chart_png).

vl-convert runs Vega in an embedded V8: ~60 MB the first time and growing with the data (≈ 220 MB after a few charts),
never given back. The API service has 512 MB, so each chart is drawn here and the memory goes when the process ends.

stdin: {"spec": <full Vega-Lite spec>, "scale": 2}; stdout: the PNG bytes; a failure → exit 2 and one line on stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FONTS = Path(__file__).resolve().parent / "fonts"


def main() -> int:
    try:
        job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        import vl_convert as vlc

        vlc.register_font_directory(str(FONTS))
        png = vlc.vegalite_to_png(job["spec"], scale=float(job.get("scale") or 2), allowed_base_urls=[])
    except Exception as exc:  # noqa: BLE001 — the reason goes back to the parent, never a traceback with data
        sys.stderr.write(" ".join(str(exc).split())[:300] + "\n")
        return 2
    sys.stdout.buffer.write(png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
