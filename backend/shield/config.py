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


# --- Phase 2C -------------------------------------------------------------
# Work caps per document. Over a cap the result is PARTIAL with truncated=True, never silent.
MAX_FILE_MB = 50
MAX_SECONDS = 60.0

# Geometry (offpage_text / cropbox_shrunk)
OFFPAGE_TOLERANCE_PT = 1.0  # a char counts as outside only when beyond the box edge by more than this
MAX_RECONSTRUCTED_CHARS = 2000

# Document kind, per page
KIND_TEXT_MIN_CHARS = 100  # text_pdf: at least this many chars on the page
KIND_IMAGE_MAX_CHARS = 20  # image_only: fewer chars than this AND an image present
SLIDE_MIN_BODY_SIZE = 14.0  # slide_deck: landscape page whose body text is at least this large...
SLIDE_MAX_CHARS = 600  # ...with low text density

# Headings
HEADING_SIZE_RATIO = 1.15  # median char size above body_size * this
HEADING_BOLD_MAX_CHARS = 80  # a bold line shorter than this is a heading
HEADING_BOLD_MIN_FRACTION = 0.9  # fraction of the line's characters that must be bold
RUNNING_LINE_MIN_PAGES = 3  # a line repeating in the top/bottom band of this many pages is a running header/footer
RUNNING_LINE_MIN_FRACTION = 0.4
RUNNING_BAND_FRACTION = 0.08

# Tables
TABLE_MIN_ROWS = 2
TABLE_MIN_COLS = 2
TABLE_MIN_NONEMPTY = 0.6  # fraction of non-empty cells

# Text quality
UNMAPPED_WARN_RATIO = 0.3

# Skills lexicon matching (rapidfuzz)
SKILL_FUZZY_CUTOFF = 90  # fuzz.ratio score; fuzzy matching applies only to terms at least this long
SKILL_FUZZY_MIN_LEN = 6

# DOCX
DOCX_MIN_FONT_PT = 4.0  # run size at or below this is "tiny_font" (inclusive; Word's smallest UI size is 1pt, body text is 8-12pt)
DOCX_MAX_UNCOMPRESSED_MB = 200  # sum of member sizes in the zip; over it nothing is parsed (zip bomb)
DOCX_MAX_MEMBERS = 5000
