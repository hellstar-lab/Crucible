# Document analyzer (Phase 2C)

`backend.shield.analyze.analyze_document(path)` turns one PDF or DOCX into a `DocumentReport`
(`backend/contracts.py`). It is deterministic and offline: no model calls, no network. The document is
hostile input: it is parsed, never executed, and no URL, attachment or launch target in it is followed.

```
python scripts/analyze_document.py resume.pdf [--json] [--ocr] [--today YYYY-MM-DD]
python scripts/doc_eval.py                  # frozen fixtures + real-world documents
```

## Status

`clean | suspicious | hidden_text_found`, a roll-up of facts, never a verdict.

| Input | Status |
|---|---|
| any hidden fragment | `hidden_text_found` |
| a `medium` or `high` finding (link mismatch, active content, embedded file, off-page text) | `suspicious` |
| a detector error, or a truncated run (200 pages, 50 MB, 60 s) | `suspicious` + `needs_review` (never `clean`) |
| `low` / `info` findings (font_anomaly, revision_history) | unchanged; reported only |
| resume contradictions | unchanged; they are facts about the content, not about tampering |

An image-only document (no text layer) is `clean` with `ocr_required_pages` and a warning: there is no hidden
text to find in pixels, but nothing was read either. Re-run with `--ocr` (needs the local `tesseract`).

## Detectors

| Detector | Source | Severity |
|---|---|---|
| hidden text (render-and-diff, attributes, Unicode, metadata) | Phase 2A | fragment |
| `offpage_text`, `cropbox_shrunk` | geometry, effective box = CropBox ∩ MediaBox | medium |
| `link_target_mismatch` | pypdf annotations / DOCX hyperlinks | high |
| `embedded_files` | name tree + FileAttachment, bounded zlib sniff | medium |
| `active_content` | OpenAction / AA / JavaScript / Launch / SubmitForm; DOCX `vbaProject.bin`, remote template | high |
| `revision_history` | `%%EOF` + `startxref` + `/Prev` | low |
| `font_anomaly` | mid-line family change in body text | low |
| provenance, font inventory | metadata block (no finding) | n/a |

DOCX hidden text: `vanish`, `hidden_style`, `white_text` (vs. page background, not when shaded/highlighted),
`tiny_font` (≤ 4 pt, resolved through the style chain).

## Known limitations

- **Render-diff false positives (Phase 2A, pre-existing).** On real LaTeX documents the render-and-diff
  detector flags (a) single narrow glyphs in AltaCV (`i`, `b`, `e`, `f` in small labels) and (b) text inside
  the attention-visualisation figures of the arXiv paper. Identical at the pre-2C commit. Not tuned here.
- Figure-derived content in papers can become a false "table" or a garbled heading (arXiv pages 13-15).
- A few words in some LaTeX fonts still lose their space ("thatyou"): the gap is below any safe threshold.
- OCR is unverified on this machine (no tesseract); it is covered by a mocked test only.
- No real Word / Pages / Google Docs / Overleaf exports were available; Skia/Quartz/LaTeX/python-docx stand-ins were used.
- `cropbox_shrunk` was verified on a synthetic file and on a print-marks control, not on a real print-ready PDF.
- Resume dates: month-name and `MM/YYYY` forms, apostrophe years, bare years and seasons. Anything else stays a
  raw string with `start/end = None`; nothing is invented. Phone numbers are extracted only with a country code.

## Fixture discipline

`tests/fixtures/docs` is frozen: `FROZEN.sha256` hashes `MANIFEST.json`, which records every file's hash. Two
fixtures were corrected before any detector logic changed; both are recorded under `MANIFEST["amendments"]`.
