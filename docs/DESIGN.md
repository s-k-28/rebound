# Rebound interface design

Rebound is an instrument for one job: show whether a USDC transfer burned on Base Sepolia was minted on Arc Testnet, prove it, and offer the one safe next action. The interface should read like a ledger and a block explorer, not like a dashboard template. This document replaces the template-led direction in [DESIGN-SOURCES.md](DESIGN-SOURCES.md) and the palette in [PRD section 8](PRD.md#8-interaction-and-visual-design).

## What was wrong

The previous screen used the stock parts of a generated SaaS dashboard:

| Problem | Where it showed |
|---|---|
| The same three numbers appeared three times | Sidebar counts, a KPI tile row, and the tab counts |
| KPI tiles | Tinted icon square, label, and a large "—" before any scan |
| Marketing inside the tool | "What Rebound checks" hero card, sidebar "See the full workflow" promo, fake "Recovery workspace" org switcher, a "Workspace / Transfers" breadcrumb on a one-screen app |
| A blue "$" avatar on every row | Off-palette and identical on every row, so it carried no information |
| Pills and rounded cards everywhere | One radius, tinted status pills with icons, icon-in-circle bullets |
| A sparse inspector | Label/value pairs stacked with large gaps. The proof (blocks, hashes, nonce state) was not the main content |

## Principles

Each principle comes from a primary or named source. The decisions below follow from them.

1. **Neutral canvas, color only for state.** Linear's 2024 redesign cut how much brand color reached its UI so the result would look "more neutral and timeless" ([Linear](https://linear.app/now/how-we-redesigned-the-linear-ui)). Stripe Apps restricts the colors each element can use ([Stripe Apps design](https://docs.stripe.com/stripe-apps/design)).
2. **A small, fixed status vocabulary, always with a word.** Stripe's Badge has six types, and badges are "read-only" ([Stripe Badge](https://docs.stripe.com/stripe-apps/components/badge.md?app-sdk-version=9)). Vercel: "Don't rely on color alone" ([Vercel guidelines](https://vercel.com/design/guidelines)). Stripe re-leveled its palette so status colors keep text contrast ([Stripe, accessible color systems](https://stripe.com/blog/accessible-color-systems)).
3. **One primary action per view.** Stripe: "Avoid having more than one primary button available to the user at a given time." Labels are verb plus noun ([Stripe Button](https://docs.stripe.com/stripe-apps/components/button.md?app-sdk-version=9)).
4. **Tabular figures, right-aligned amounts.** `tabular-nums` makes every figure the same width "like in tables" ([MDN font-variant-numeric](https://developer.mozilla.org/en-US/docs/Web/CSS/font-variant-numeric)). Vercel names tabular figures or a mono face for numbers ([Vercel guidelines](https://vercel.com/design/guidelines)).
5. **Hashes in mono, middle-truncated, full value copyable.** Middle truncation suits strings "whose head and tail both carry information" ([Vercel Geist, Middle Truncate](https://vercel.com/geist/middle-truncate)).
6. **Hairlines and spacing, not boxes.** Linear's 2026 refresh moved to fewer, lower-contrast separators ([Linear](https://linear.app/blog/behind-the-latest-design-refresh)). Refactoring UI suggests spacing or background contrast in place of extra borders ([Refactoring UI](https://refactoringui.com/)).
7. **Spend space on data.** Tufte's data-ink ratio, as summarized by NN/g: "Remove any unnecessary visual elements" ([NN/g](https://www.nngroup.com/articles/clutter-charts/)). Matt Ström-Awn defines density as "the value a user gets from the interface divided by the time and space the interface occupies" ([UI density](https://mattstromawn.com/writing/ui-density/)).
8. **Outcome first, raw fields lower.** A critique of Etherscan faults the transaction page for pushing the key action down and promoting status codes ([Pratt IxD](https://ixd.prattsi.org/2024/09/design-critique-etherscan-website/)).
9. **Fewer, smaller icons, no colored icon backgrounds.** Linear 2026 removed colored icon backgrounds and scaled down the icons that remained ([Linear](https://linear.app/blog/behind-the-latest-design-refresh)).
10. **Copy as utility text. Design empty, loading and error states too.** "Use as few words as possible" ([Vercel guidelines](https://vercel.com/design/guidelines)). Generated UIs nail the happy path and skip the other states ([Canatselis](https://world.hey.com/kostac/spot-the-slop-a-ui-designer-s-guide-to-fixing-ai-defaults-4c448c9c)).

These sources name the tells this redesign removes: KPI stat rows, equal-weight card grids, colored left-edge accent bars, status dots on everything, tinted icon chips, and uniform radius ([Krebs](https://adriankrebs.ch/blog/design-slop/), [Humble](https://www.thefountaininstitute.com/blog/signs-vibe-coded-ui), [Mondal](https://www.saasui.design/blog/saas-ui-looks-ai-generated), [Canatselis](https://world.hey.com/kostac/spot-the-slop-a-ui-designer-s-guide-to-fixing-ai-defaults-4c448c9c)).

## Decisions

### Color

The rule is that **ink means settled, the accent means you can act here, amber means waiting, and red means the record needs review.** No other color carries meaning, and nothing is decorative.

| Token | Hex | Use |
|---|---|---|
| `PAPER` | `#F3F4F2` | Page ground: cool neutral, not cream |
| `SURFACE` | `#FFFFFF` | Ledger, inspector, inputs |
| `INK` | `#141A17` | Primary text, settled states, filled proof markers |
| `INK_2` | `#48514C` | Secondary text, hashes in rows |
| `INK_3` | `#68706B` | Labels and metadata (4.9:1 on white) |
| `RULE` | `#E1E4E0` | Hairlines |
| `RULE_STRONG` | `#C8CDC8` | Input and secondary-button borders |
| `ACCENT` | `#1D5A44` | The only accent: primary button, links, "Ready to finish", "Not minted" on a ready transfer |
| `AMBER` | `#9A5B0B` | Waiting, restricted, expired, example mode (5.4:1) |
| `RED` | `#A8332B` | Source receipt changed, errors (6.6:1) |

### Type

- **Manrope** (already bundled) carries words. Scale: 11 / 12 / 13 / 15 / 16 / 18. Weights: 500 for body emphasis, 600 for labels in context, 700 for section titles and the actionable status, 800 only for the wordmark. No all-caps labels and no eyebrows.
- **IBM Plex Mono** (Regular and Medium, OFL, bundled in `assets/fonts` with `OFL-IBMPlexMono.txt`) carries every machine value: amounts, hashes, addresses, block numbers, nonces, timestamps and counts. Flet exposes no OpenType feature control, so a mono face is how this app gets tabular figures. Amounts line up on the decimal point in every table.
- Related amounts share one precision. The inspector shows `1.00000 / 0.00013 / 0.99987` instead of `1.00 / 0.00013 / 0.99987`, so the decimal points align and no digit is hidden.

### Shape and surfaces

- Radii vary with hierarchy: 3 for the Testnet tag, 4 for small links, 6 for controls and notices, 8 for the ledger frame. Status dots and proof markers are round.
- One-pixel hairlines separate things. There are no shadows, gradients, illustrations, emoji or tinted icon chips.
- Icons only where they change meaning: back, close, open external receipt, copy, link.

### Layout

```
Desktop, 1080 px and wider
┌────────────────────────────────────────────────────────────────────────────────────┐
│ Rebound │ Base Sepolia → Arc Testnet [Testnet]                Coverage  How it works │
├──────────────────────────────────────────────────────┬─────────────────────────────┤
│ [example-mode notice, only in examples]              │ 128.50 USDC              ×  │
│ [wallet address 0x…              ] [2,000 blocks▾] [Find transfers] │ ● Ready to finish │
│ Search by transaction  History options  …            │ explanation                 │
│ ┌──────────────────────────────────────────────────┐ │ Proof        Checked … UTC  │
│ │ All transfers 4  Needs attention 3  Completed 1  │ │ ① Burn on Base Sepolia  ✓   │
│ │ Amount, USDC  Status   Source tx   Block  Recip. │ │ │ block, log, tx, confs    │
│ │       128.50  ● Ready  0x0101…0101 34,999,850 …  │ │ ② Circle attestation        │
│ │        42.00  ● Wait…  0x0202…0202 34,999,700 …  │ │ │ nonce, expiry            │
│ │ coverage line                                    │ │ ③ Mint on Arc Testnet       │
│ └──────────────────────────────────────────────────┘ │ Fixed recipient             │
│                                                      │ Amount table                │
│                                                      │ Finish the transfer / Actions│
│                                                      │ Record + copy actions       │
└──────────────────────────────────────────────────────┴─────────────────────────────┘
```

Narrower than 1080 px, the list fills the width, and selecting a transfer replaces the list with the inspector as a full-width sheet. The sheet has an "All transfers" back bar.

### Ledger row

| Column | Content | Treatment |
|---|---|---|
| Amount, USDC | `128.50` | Mono Medium 13, right-aligned, no unit per row (it is in the header) |
| Status | dot and word | 7 px dot in the state color. Ink text, bold only when actionable |
| Source transaction | `0x7e43…3f23` | Mono 12.5, middle-truncated |
| Source block | `61,175,721` | Mono, right-aligned |
| Recipient | `0x3958…722c` | Mono. Hidden when the ledger is narrower than 820 px |

There are no avatars, no chevrons and no per-row tooltips. A selected row gets a neutral wash, without a colored edge bar. On phones a row is two lines: amount and status, then hash and block.

### Inspector

1. **Outcome:** the amount in mono at 30 px, the status, and the service's one-sentence explanation.
2. **Proof**, the main content: three numbered steps joined by a neutral rail. Each step shows a state word (Verified, Matched, Minted, Waiting, Not minted, and so on) with its evidence in mono: source block and log, tx hash, confirmations, and the receipt link; then the Circle nonce and expiry block; then the Arc nonce state, block and verified receipt link. Filled ink markers mean proven. Outlined markers take the state color. The step states come from `proof_steps()` and are derived only from verified fields. Times come from the canonical Base burn block and the verified Arc mint block ("Burned 06:43:33 UTC, 1 h 12 min ago", "Minted 06:45:12 UTC, 99 s after the burn"). Only the relative part reads the server clock, once per render. While a live transfer waits on Base confirmations or Circle, a mono line under the title counts down to the next recheck ("Checking again in 8 s") with a Stop link.
3. **Fixed recipient**: the full address, and the rule that it cannot change.
4. **Amount** table: burned, Circle fee, and recipient receives (total row under an ink rule).
5. **Finish the transfer** (ready) or **Actions**: the single primary button, then secondary refresh and replay buttons. Example mode shows "This example cannot prepare or sign a transaction." in place of the form.
6. **Record**: full source hash, destination caller, message nonce and discovery source, plus copy evidence and copy case link.

### Status vocabulary

| Status | Dot | Meaning |
|---|---|---|
| Ready to finish | accent | A recovery can be reviewed now |
| Completed | ink | Arc consumed the nonce. Nothing to do |
| Waiting for Circle, Source confirming, Caller required, Attestation expired, Forwarded transfer, Mint submitted | amber | Not actionable yet, or restricted |
| Source needs review | red | The canonical receipt no longer matches |
| Could not verify, Outside current support | grey | Unknown. Take no action |

### Copy

- Sentence case. Plain verbs. No slogans, tricolons or "journey". Labels never go in all caps.
- Buttons say what they do: "Find transfers", "Check transaction", "Review recovery", "Refresh this transfer".
- Do not use middle-dot joined strings in the interface.

### States

Every state below renders inside the ledger frame, where the results will appear, so the frame never jumps.

- **Empty:** one line ("Enter a wallet address to list its USDC transfers from Base Sepolia to Arc.") and two actions: "Check a live sample" and "Explore examples". Tabs and the filter stay hidden until a lookup returns records, so empty counts never show.
- **Validation:** checked before any network call. A red line sits directly under the field ("Enter the full wallet address: 0x followed by 40 hex characters."), the field border turns red, the caret goes back into the field, and the results already on screen stay.
- **Loading:** a 2 px progress rule on the frame's top edge and the exact request: "Scanning Base Sepolia blocks 61,175,522–61,175,721 for 0x3958…722c" (or "the latest 2,000 Base Sepolia blocks" when no end block is set), then "Reading source events, Circle attestations and destination state…". The previous lookup's rows, counts and errors are never shown under a new one.
- **Error:** the service's message in red, inside the frame, with "Try again". It never apologizes.
- **Example mode:** a persistent amber notice ("Example mode. These transfers are synthetic and nothing here can be signed.") with "Return to live". Preparing and signing stay blocked in `InboxState.prepare`.
- **No results:** a sentence that names what was searched ("No supported transfers in the latest 2,000 Base Sepolia blocks.") and the next step: "Search 10,000 blocks" and "Search by transaction". The coverage line stays visible.
- **Filter with no matches:** names the query and offers "Clear filter".
- **Confirmations:** one snackbar style (ink, compact, floating) and one phrasing: "Evidence copied", "Case link copied", "Demo wallet address copied", "Transaction JSON copied".
- **Focus:** buttons and links draw a ring on keyboard focus. Rows and tabs take a neutral wash on hover and a stronger one on focus, and open with Enter.

### Mobile (390 px portrait)

The top bar wraps to two lines and the form stacks. Tabs scroll horizontally and never wrap. Rows are two lines. The inspector opens as a full-width white sheet under a pinned "All transfers" back bar. Links, tabs, buttons and the filter are at least 44 px tall, and the page keeps 32 px clear below the last row. No layout requires turning the phone sideways.

### Browser tab

The page title is "Rebound". The favicon, home-screen icons and loading mark are a plain white R on an ink square (`assets/favicon.png`, `assets/icons/`), and `assets/manifest.json` sets the browser theme to ink.

## Removed

KPI tiles, the dark sidebar and its duplicate counts, the org switcher, the breadcrumb, the "What Rebound checks" hero card, the promo card, the "$" avatars, status pills with icons, decorative prefix icons in inputs, per-row tooltips and the footer route line (the top bar already states the route).
