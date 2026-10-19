# Parser provenance

Newly parsed fragments record the installed parser package name/version and actual
EPUB spine or DOCX heading alongside the existing structural address. Migration0030
adds four nullable text columns. Existing records stay NULL; updating the application
does not reparse a book, alter its text/citations or call a model.

| Format | Reused package | Locked version | Upstream license |
| --- | --- | --- | --- |
| PDF | pypdf | 6.20.0 | BSD-3-Clause |
| EPUB | EbookLib | 0.20 | AGPL-3.0-or-later |
| DOCX | python-docx | 1.2.0 | MIT |
| HTML | beautifulsoup4 | 4.14.3 | MIT |
| Plain UTF-8 | Python | Actual runtime version | Python distribution license |

Versions come from the installed distribution metadata and the existing lockfiles.
License names above were checked against the same distributions' METADATA.
Dependency licenses are recorded separately from the project's own license.
No dependency, license file or lockfile was replaced for this change.

DOCX heading labels use actual heading styles, including inherited custom styles;
the label is a bounded prefix while the source paragraph remains complete.
Table cells retain their original cell address and enclosing document heading.
EPUB spine identifiers distinguish repeated chapter titles; page numbers stay unknown.
Scans still report needs_ocr when no extractable text exists; this does not add OCR.
