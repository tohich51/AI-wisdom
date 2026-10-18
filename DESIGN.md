# AI-wisdom interface

## Overview

`src/kb/static/index.html` defines the product-admin shell, `src/kb/static/style.css` is the visual source of truth, and the loaded vanilla-JS modules provide behavior. Token prose here is descriptive rather than generated configuration.
Graph-specific reading, provenance, palette, and focus behavior remain documented in `docs/UI-DESIGN.md`; that document is referenced rather than duplicated here.

## Colors

- Canvas and surfaces use `--canvas`, `--surface`, and `--surface-subtle`.
- Primary and secondary text use `--ink` and `--muted`.
- Borders use `--line` and `--line-strong`.
- Actions use `--accent`, `--accent-hover`, and `--accent-soft`.
- Errors use `--danger`; keyboard focus uses `--focus`.
- `forced-colors: active` replaces theme colors with system color roles; shadows are removed where supported.

## Typography

- `--font` supplies the platform system UI stack; body copy uses a `1.5` line height.
- Section titles use the responsive `clamp(1.7rem, 3vw, 2.3rem)` size and tight line height.
- Card and section headings use the local `h2` scale; labels and metadata have smaller explicit sizes.
- Labels and controls use the inherited UI font and `650`–`750` weight where specified.
- Long labels, values, URLs, and record text wrap instead of forcing horizontal overflow.

## Layout

- The shell uses a full-height grid: a `250px` sidebar and a fluid main column, with a full-width header.
- The sticky sidebar owns its vertical overflow; the main column is capped at `1240px`.
- Library workspaces use asymmetric two-column grids and collapse to one column at `860px`.
- Research uses three columns, then two, then one at `1100px` and `700px`; maps use a constrained surface.
- Additional narrow-screen adjustments begin at `380px`; content containers preserve `min-width: 0`.

## Elevation & Depth

- Cards use the shared subtle `0 10px 30px` shadow over the canvas.
- The header has only a fine lower-edge shadow.
- Hover states change border and background; pressed selected controls use the accent-soft surface.
- Focus-visible controls receive a three-pixel focus ring with offset; dialog presentation remains native.
- Reduced-motion preferences remove meaningful transition and animation duration.

## Shapes

- Cards and status regions use `--radius: 0.75rem`.
- Buttons and fields use `--control-radius: 0.55rem`.
- Tags and scrollbar thumbs use a fully rounded pill shape.
- Native scrollbars are `12px` with a bordered track and three-pixel thumb inset.

## Components

- A skip link, header, account controls, status region, and labeled sidebar navigation form the shell.
- The persistent `#status` region uses `role=status` and `aria-live=polite`; it is not a floating toast.
- Forms pair native labels, inputs, selects, checkboxes, textareas, and buttons in a vertical grid.
- Native `<select>` controls intentionally retain their browser popup.
- Lists and pagers use existing vanilla-JS owners; feature workspaces mount into their existing route containers.
- Dialogs remain owned by the current vanilla-JS implementation and native browser behavior.
- `#logout` has a real `addEventListener` binding in `app.js`; no inline callback is required.

## Do's and Don'ts

- **Do:** use the CSS tokens, native controls, visible focus, `44px` targets, responsive wrapping, and semantic status or error relationships.
- **Do:** preserve existing route and feature owners, and record browser or command results only when they actually run.
- **Don't:** introduce a component framework, custom select popup, fake toast, or decorative focus hack.
- **Don't:** add inline event attributes merely to satisfy scanners when a real listener already exists.
- **Don't:** reset a create form after a failed request or duplicate graph-specific rules from `docs/UI-DESIGN.md`.
