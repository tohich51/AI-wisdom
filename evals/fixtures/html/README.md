# HTML fixtures for the extractor (`html-extract/1`)

Two pages and two pinned extractions. The goldens in `expected/` are what the
extractor must produce; a change to fragment boundaries, whitespace handling or
entity decoding shows up as a diff against a file a human read, instead of as a
quiet drift in every knowledge item the product will ever hold.

| fixture | what it is | what it pins |
|---|---|---|
| `article.html` | an ordinary reference page: headings, paragraphs, a list, a table, relative and absolute links, an image, an entity, a non-breaking space, a comment | the ordinary case, including the whitespace and entity rules |
| `hostile.html` | a page whose every other paragraph is an attempt to obtain a role, run a command or exfiltrate a token, plus a `<script>`, a `<meta refresh>`, an `onclick`, a `javascript:` href and references to a metadata endpoint, a private address and a loopback service | the data boundary: what is extracted, what is not, and what is reported |

## Decisions the goldens freeze

These are choices, not accidents, and each one is visible in the goldens:

* **A block boundary is a fragment boundary.** `p`, `li`, `h1`-`h6`, `td`, `th`
  and the common containers end a fragment. A table's cells are separate
  fragments, each with its own ordinal — finer than a row, and honest about what
  a page actually offers.
* **`chapter` is the nearest preceding heading.** It is the only structure a web
  page has that a reader can navigate by. It is truncated, and it is never
  guessed at.
* **Scripts, styles, comments, `noscript`, `template`, `iframe`, `object`,
  `embed`, `svg` and `math` bodies are not text.** A payload hidden in any of
  them does not become a knowledge item. The raw bytes still contain it, and the
  original is stored unchanged.
* **Attributes are not text.** `onclick`, `alt`, `javascript:` hrefs and
  `data:` URLs contribute nothing to a fragment.
* **The title is metadata.** It is recorded on the result, scanned for
  directive-shaped text, and never emitted as a fragment.
* **Whitespace is collapsed and entities are decoded.** `&nbsp;` reads as one
  space, `&amp;` as `&`, and a run of spaces as one.
* **References are recorded and never fetched.** `href`/`src`/`srcset`/`poster`
  values are resolved against the page URL with `urljoin` — string arithmetic —
  and listed. Nothing in the card opens them, so a page cannot make the gateway
  issue a second request to whatever it names.
* **Everything is bounded.** Fragment count, fragment length, total text and the
  number of recorded references all have ceilings, and a document that hits one
  says so in `truncated` / `truncation_reason` rather than silently stopping.

## Reading a golden

`expected/*.json` holds the URL, the fixed retrieval instant, the content hash,
the extractor version, the title, the language, the fragment list, the detected
directive kinds and offsets, and the referenced URLs. `test_snapshot_and_extract.py`
compares a live extraction against it field by field.

The files are read by `tests/integration/fetch/`, which is also where the
end-to-end path is: fetch under the policy (over a mock transport, with the
address checks live), extract, record. Nothing here reaches the network.
