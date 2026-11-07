Put the company's real, approved documents here (one `.md` file each, with the front-matter
passport described in `hari/knowledge/loader.py`), then set `KNOWLEDGE_DIR=knowledge/docs`.

Until then HARI runs on `knowledge/samples/` — sample texts built from the spec, every title marked «НАМУНА».
This README has no front matter, so the loader refuses it and never indexes it.
