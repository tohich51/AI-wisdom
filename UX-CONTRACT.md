# UX contract

Runtime under `src/kb/static` and its CSS are authoritative. `src/kb/static/forms.js` is the canonical owner of shared client-side validation and submission, while existing feature modules retain native control creation, field semantics, and callbacks. This map covers the five required capabilities and does not replace the [graph-specific contract](docs/UI-DESIGN.md).

The native `<select>` popup is intentionally OS-owned. No custom listbox, toast framework, or component framework is introduced. Security and business behavior remain governed by the repository's existing authoritative entries. Source readback below is not browser certification.

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
| --- | --- | --- | --- | --- |
| Select/Listbox | OS-owned native browser `<select>` popup | `src/kb/static/index.html` for page controls; existing vanilla-JS creators for dynamic controls | Native select only; no custom popup or listbox replacement | Source readback confirms native ownership; popup and keyboard checks for the current candidate are pending |
| Form | `src/kb/static/forms.js` shared validation/submission with retained native feature owners | `src/kb/static/forms.js`, `src/kb/static/index.html`, and the current form creators in `src/kb/static/app.js` | Shared canonical validation and submission orchestration; native page and dialog controls; no framework wrapper | Source readback confirms ownership, linked errors, and retained failed input; candidate focus and announcement checks are pending |
| Scrollbar | Browser scrollbars styled by application CSS | `src/kb/static/style.css` scrollbar and overflow rules | Native Firefox, WebKit, and system-forced-color presentation | Source readback confirms thin stable rules; historical viewport and forced-color evidence exists, while current-candidate checks are pending |
| Toast | Existing vanilla-JS `message()` writing `#status` | `src/kb/static/index.html` and `src/kb/static/app.js` | Persistent polite status region only; no floating toast | Source readback confirms the live region and owner; current runtime announcement check is pending |
| CRUD | Existing vanilla-JS views, feature callbacks, API wrappers, and router with shared form submission | `src/kb/static/app.js`, `src/kb/static/data.js`, `src/kb/static/router.js`, and `src/kb/static/forms.js` | Only evidenced create, read, update, and publication actions; no unobserved delete affordance | Source ownership readback only; candidate end-to-end CRUD and any delete capability remain pending and uncertified |

## Verification evidence

- Historical browser evidence at `59346ebf8cea6a1c8c327bda55ee5d7a7a7de4cc` passed citation behavior, focus return, `320px` and `390px` layout checks, and forced-colors presentation.
- Those results describe the historical baseline only. Browser verification and Sol verification for the current candidate are still pending and must not be represented as passed.
- Source readback can establish ownership and static constraints, but it is not browser conformance. No formal WCAG or GOST certification is claimed.

## Ownership and matching boundaries

- `src/kb/static/forms.js` owns shared validation and submission; existing feature owners retain native controls, business semantics, and callbacks.
- Exact-match source selection retains every configured identifier, quotation, and locator constraint. Label and graph presentation changes do not relax those constraints.
- Graph presentation uses the shared graph CSS tokens, a visible legend, and the full-DOM relation alternative. Runtime geometry remains delegated to Cytoscape's measured-label options rather than a custom collision engine.

## Privacy and pagination boundaries

- Private-book drafts are not persisted in browser storage by this candidate.
- Draft recovery is opt-in per exact form entity and held only in tab RAM: up to 20 records / 128 KiB total and 32 Ki characters per field. No password, token or file contents are saved. It requires a fresh identity, accessible library list and exact server policy after sign-in. Recovery is explicit and never submits a write; closing/reloading the tab loses the text. Administrative permission forms are excluded. Browser component isolation checks passed; product-level reauthentication acceptance remains pending.
- Lists retain only a numeric page index in a bounded 100-key tab-RAM map scoped by account, principal, library and named list. Client and fixed server pages restore without replaying all earlier pages; a missing page falls back once to page one. Access invalidation resets positions. Synthetic browser checks passed; product readback remains pending.
- These are explicit pending limitations; this document does not claim that all 72 review findings are closed.

Verification status is deliberately per capability: no row is silently certified by this document.
