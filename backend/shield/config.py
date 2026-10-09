"""Every Shield threshold, in one place. Nothing is hardcoded elsewhere."""

# Rendering
RENDER_SCALE = 2.0  # render pages at 2x (144 dpi) for the visibility diff

# Visibility (render-and-diff)
DIFF_VISIBLE_LEVEL = 12  # max grayscale diff below this => glyph not drawn
CONTRAST_MIN = 24  # drawn, but max diff below this => too faint to read
DIFF_BOX_INSET_PX = 1  # shrink char boxes so neighbouring glyph ink is not counted

# Attribute detectors
BG_MATCH_DIST = 90  # sum |dR|+|dG|+|dB| between fill and background
# Rendered size in points at or below which text is flagged (INCLUSIVE).
# Inclusive because an exclusive "< 2.0" rule was evaded by text set at
# exactly 2pt. Not higher because 3.0 and 4.0 were validated against real
# PDFs and flagged legitimate text: page previews and diagram labels at
# 2.05-2.73pt in a vendor manual (10,816 chars at 3.0, 11,044 at 4.0).
# Known gap: plain dark text at 2.05-4pt passes this rule; a false positive
# on an honest resume is judged worse than missing that.
MIN_FONT_SIZE = 2.0
# Sizes come from float32 pdfium matrices: 2pt via "20 Tf" x 0.1 measures as
# 2.0000000298. The tolerance keeps the inclusive boundary robust to that.
FONT_SIZE_EPSILON = 0.01
MIN_OPACITY = 0.1  # 0..1; alpha below this counts as transparent

# Metadata
MAX_METADATA_LEN = 200  # characters per metadata field / XMP text segment
B64_TEXT_PRINTABLE_MIN = 0.9  # decoded base64 at least this printable => treat as text, not a blob

# Unicode
MIN_ZW_RUN = 8  # zero-width / filler run length that counts as hidden payload
MIN_VS_RUN = 8  # variation-selector run length that counts as hidden payload

# Fragment grouping: a hidden run continues across whitespace only while the
# next hidden char sits within this many font-sizes vertically of the last.
FRAGMENT_LINE_GAP = 2.0
FRAGMENT_MIN_LINE_HEIGHT = 2.0  # points; floor for that gap so microscopic text still groups

# Resource limits for untrusted uploads
MAX_PAGES = 200

