# AI-wisdom interface

## Overview

`src/kb/static/index.html` defines the product-admin shell, `src/kb/static/style.css` is the visual source of truth, and the loaded vanilla-JS modules provide behavior. Token prose here is descriptive rather than generated configuration.
`src/kb/static/forms.js` is the canonical owner of shared client-side form validation and submission; existing feature modules retain ownership of native control construction, field semantics, and API calls.
Graph-specific reading, provenance, palette, and focus behavior remain documented in [the graph UI design](docs/UI-DESIGN.md); that authority is referenced rather than duplicated here. Security- and business-sensitive behavior also remains owned by the repository's existing security and business entries.

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
- Graph labels use `--graph-font-family`, `--graph-label-size`, and `--graph-overview-label-size`; the initial Arial 11 graph-label treatment is no longer authoritative.
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
- Forms pair native labels, inputs, selects, checkboxes, textareas, and buttons in a vertical grid. Existing feature modules retain native control creation and business semantics.
- `src/kb/static/forms.js` is the single canonical owner of shared client-side validation and submission orchestration; native control owners and feature callbacks are otherwise retained.
- Native `<select>` controls intentionally retain their OS-owned popup.
- Associated checkbox and radio labels provide the project's `44px` target while the native controls retain their readable visual size. Ordinary form labels are not forced to that height.
- Lists and pagers use existing vanilla-JS owners; feature workspaces mount into their existing route containers.
- Dialogs remain owned by the current vanilla-JS implementation and native browser behavior. `dialogForm` derives a contextual visible submit label from its exact title, with `Сохранить` retained as the fallback.
- `#logout` has a real `addEventListener` binding in `app.js`; no inline callback is required.

## Graph integration

- Graph presentation uses the `--graph-*` CSS tokens together with the visible legend and the full-DOM relation alternative; the detailed contract remains in `docs/UI-DESIGN.md`.
- Runtime graph geometry is delegated to Cytoscape layout and style options using measured labels. The application does not add a custom label-collision engine.
- Exact source matching retains every configured identifier, quotation, and locator constraint; graph presentation changes do not weaken source validation.

## Interaction targets

- `44px` is the project target for primary controls and associated checkbox/radio labels, not a claim that WCAG requires `44px`. WCAG 2.2 AA Target Size (Minimum) is `24px` by `24px` CSS pixels with its documented exceptions.
- Checkbox and radio inputs retain their readable native dimensions; the associated label supplies the larger pointer target.
- Native `resize: vertical` remains enabled for textareas as a deliberate long-text business exception and provides user-controlled expansion. It is not disabled without a working auto-grow or expansion replacement.
- No formal WCAG or GOST certification is asserted.

## Do's and Don'ts

- **Do:** use the CSS tokens, native controls, visible focus, responsive wrapping, semantic status or error relationships, and `44px` as the project target for applicable interactive targets.
- **Do:** preserve existing route and feature owners, and record browser or command results only when they actually run.
- **Don't:** introduce a component framework, custom select popup, fake toast, or decorative focus hack.
- **Don't:** add inline event attributes merely to satisfy scanners when a real listener already exists.
- **Don't:** reset a create form after a failed request or duplicate normative graph-specific rules from `docs/UI-DESIGN.md`.
- **Don't:** disable native vertical textarea resizing without a working expansion mechanism.
- **Don't:** present the `44px` project target or source readback as formal WCAG/GOST certification.
