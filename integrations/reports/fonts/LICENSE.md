# Fonts

`NotoSans-Regular.ttf`, `NotoSans-Bold.ttf` — Noto Sans, Copyright 2015 Google LLC,
licensed under the SIL Open Font License, Version 1.1 (https://openfontlicense.org).
Bundled so the report images and PDFs render identically on Render and on a
developer machine; they cover Uzbek Cyrillic (ў қ ғ ҳ) and have tabular figures.

`integrations/api/static/NotoSans-{Regular,Bold}.woff2` (2026-10-23) are subsets of these two (Latin,
Cyrillic, punctuation, arrows — `fontTools.subset`, WOFF2) for the PULSE page, same license: 1 MB → 87 KB.
