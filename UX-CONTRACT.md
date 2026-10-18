# UX contract

Runtime under `src/kb/static` and its CSS are authoritative. This map covers only the five required capabilities and does not replace the graph-specific contract in `docs/UI-DESIGN.md`.

The native `<select>` popup is intentional. No custom listbox, toast framework, or component framework is introduced. Source readback below is not browser certification.

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
| --- | --- | --- | --- | --- |
| Select/Listbox | Native browser `<select>` popup | `src/kb/static/index.html` for page controls; existing vanilla-JS creators for dynamic controls | Native select only; no custom popup or listbox replacement | Source readback confirms native ownership; popup and keyboard browser checks pending |
| Form | Native controls with existing vanilla-JS submit handlers | `src/kb/static/index.html`, the create-library handler in `src/kb/static/app.js`, and the current form owners there | Native page forms and existing dialog forms; no framework wrapper | Source readback covers `novalidate`, linked create-library errors, preserved failed input, and existing empty-search handling; browser focus and announcement checks pending |
| Scrollbar | Browser scrollbars styled by application CSS | `src/kb/static/style.css` scrollbar and overflow rules | Native Firefox, WebKit, and system-forced-color presentation | Source readback confirms thin stable scrollbar rules; viewport and forced-color checks pending |
| Toast | Existing vanilla-JS `message()` writing `#status` | `src/kb/static/index.html` and `src/kb/static/app.js` | Persistent polite status region only; no floating toast | Source readback confirms the live region and owner; runtime announcement check pending |
| CRUD | Existing vanilla-JS views, forms, API wrappers, and router | `src/kb/static/app.js`, `src/kb/static/data.js`, and `src/kb/static/router.js` | Only evidenced create, read, update, and publication actions; no unobserved delete affordance | Source ownership readback only; end-to-end CRUD and any delete capability remain uncertified |

Verification status is deliberately per capability: no row is silently certified by this document.
