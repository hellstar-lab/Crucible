# Third-party material

Code and data included in this repository that was written by others.
Dependencies installed from PyPI (see `requirements.txt`) are not
repeated here.

## Technology lexicon (document analyzer)

`backend/shield/data/tech_lexicon.txt` is a hand-written list of technology names and aliases (one entry per
line, `|` separates aliases, a leading `!` marks a term that is also an ordinary word, such as "Go" or "Rust",
and is only counted in a skills-like context). It is original to this repository, not copied from a third-party
list, and is used by `backend/shield/resume.py` for exact and rapidfuzz matching.

## GitHub Linguist exclusion data

| | |
|---|---|
| Files | `backend/evidence/linguist/vendor.yml`, `backend/evidence/linguist/documentation.yml` |
| Upstream | https://github.com/github-linguist/linguist (`lib/linguist/vendor.yml`, `lib/linguist/documentation.yml`) |
| Pinned commit | `5fbdfcb8133be2bed88bf3ce62b2335f50474525` (default branch `main`, committed 2026-09-29) |
| Exact sources | https://raw.githubusercontent.com/github-linguist/linguist/5fbdfcb8133be2bed88bf3ce62b2335f50474525/lib/linguist/vendor.yml<br>https://raw.githubusercontent.com/github-linguist/linguist/5fbdfcb8133be2bed88bf3ce62b2335f50474525/lib/linguist/documentation.yml |
| Licence | MIT, Copyright (c) 2017 GitHub, Inc. |
| Retrieved | 2026-10-10 |
| Modifications | None. The files are vendored byte-for-byte; all 186 patterns (168 + 18) compile unmodified with Python's `re`. |

The two content heuristics in `backend/evidence/exclusions.py` (`is_minified`,
`has_source_map`) are re-implementations of the corresponding checks in
Linguist's `lib/linguist/generated.rb` at the same commit, not copies of
its code.

### MIT licence text (GitHub Linguist)

Copyright (c) 2017 GitHub, Inc.

Permission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use,
copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.
