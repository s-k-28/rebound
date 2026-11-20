# Rebound design direction

The first frontend was rejected as too generic. The revision uses specialist finance and product-design references, with original Python/Flet controls rather than imported template source or purchased assets.

## Reviewed references

| Creator | Reference | Relevant design decisions |
|---|---|---|
| Peter Cha | [Tessel Finance](https://www.framer.com/marketplace/templates/tessel-finance/) · [live preview](https://dynamic-course-813547.framer.app/) | Persistent navigation, aligned financial rows, restrained status colors, compact workspace hierarchy. Its marketing hero is not the right structure for a recovery inbox. The embedded product workspace was visually inspected in Chrome. |
| Keitoto Studio | [Globyn](https://www.mebius.design/resources/globyn) · [gallery](https://www.mebius.design/resources/globyn/gallery) | Finance workflow continuity: transfer history, recipient review, receipt and confirmation. Use as a reference for useful screen states instead of a decorative landing page. |
| Victor Studios | [Conduit Pay](https://www.framer.com/marketplace/templates/conduit-pay/) · [live preview](https://conduit-fintech.framer.website/) | A payment's amount, fee, recipient and route are understandable together. Translate that information hierarchy into the recovery inspector; template rates, examples and marketing claims are not Rebound data. Visually inspected in Chrome. |
| PrimeUI | [Vellum](https://www.framer.com/marketplace/templates/vellums/) · [live preview](https://vellumai.framer.website/) | Strong type and a clear progression from scattered evidence to an organized financial workflow. Useful for the burn → attestation → mint explanation. |
| Obra Studio | [Product components](https://shadcn.obra.studio/components) | Consistent spacing, typographic hierarchy and complete component states. No React or shadcn code is imported into this Python app. |
| NS. Studio | [Sightline](https://www.framer.com/marketplace/templates/sightline/) | Keep search, filtering and row selection together. Rebound gives the evidence pane its own scroll area so users can compare proof with the inbox. |

## Original implementation

- Forest navigation, cool paper workspace, mint accents, concise status colors.
- Bundled Manrope typography, licensed under the included SIL Open Font License.
- An application shell with compact address/history scan controls; no large landing-page hero above the work.
- Transfer search/filter controls and counts derived from current records. No fabricated recovered balance, revenue, chart history or customers.
- A selected-transfer inspector with exact recipient, original and net amount, caller rule, source evidence and current completion state.
- Live data and synthetic examples remain visibly distinct, with transaction preparation disabled for examples.
- Desktop keeps the searchable ledger visible while the evidence pane scrolls independently.
- Mobile opens one transfer at a time with a visible return to its inbox and filters.
- The Rossark motion approach informs a single 180 ms ease-out reveal on selection. The new transfer appears in place. The app reads platform accessibility preferences and uses a still reveal when animations are disabled or preferences are unavailable. [Flet animation controls](https://flet.dev/docs/cookbook/animations/) · [WCAG animation from interactions](https://www.w3.org/WAI/WCAG22/Understanding/animation-from-interactions)

The references are design research, not acquired template licenses. No template purchase or proprietary source copying occurred.
