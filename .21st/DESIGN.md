# Giraffe — Quiet Authority Design Contract

## Product read
Giraffe is a **Private Investment Office** for Korean paper-trading decisions. It must feel like a calm, selective operating surface: the user sees what is known, what changed, and what remains unverified—not invented wealth or performance.

- Taste dials: **variance 6 / motion 4 / density 7**.
- Stack: FastAPI SSR + semantic HTML + CSS custom properties + small vanilla JS. No React, CDN, or new build system.
- Visual language: cold-luxury **graphite**, **porcelain**, and one Giraffe **cobalt** accent. Korean-readable sans and tabular numeric settings.

## Information hierarchy
1. Purpose and current investment-decision state.
2. Accumulated review scale (reviewable cards, holds, evidence needing refresh).
3. Today’s evidence and judgment changes.
4. Items that are still unverified and cannot support a conclusion.
5. Provenance and raw technical detail only behind an explicit disclosure.

## Tokens
- Graphite `#161b25`; porcelain `#f4f6f8`; sheet `#ffffff`; ink `#1d2735`; muted `#667286`; divider `#d8dee8`.
- Cobalt `#2457d6`, cobalt low `#edf2ff`; semantic success `#126b49`, warning `#9a6200`, danger `#b9382f`.
- 8px spatial rhythm; controls at least 44px; surface radius 14px; controls radius 10px; restrained elevation and separators.
- Focus is a clearly visible cobalt outline. Respect `prefers-reduced-motion`; animation only explains a sheet or state transition.

## Components and behavior
- Authenticated and unauthenticated views share the same graphite/porcelain/cobalt shell and compact office wordmark.
- Dashboard summary is a single operational strip, not decorative KPI cards. Use real counts only and label unknown/stale states plainly.
- Rows are evidence-led: identity, actual state, change, and next review. GOOD/BAD/warning colors are semantic only.
- Detail is a keyboard-safe bottom sheet. Scenario, provenance, and raw detail use disclosures; focus trap, Escape, opener restoration, and mobile sheet behavior remain intact.
- Loading, empty, error, session expiry, and unavailable evidence must be explicit Korean text states.

## Non-negotiable constraints
- Presentation only: do not alter API, database, authentication, card, evidence, scenario, order, or allocation semantics.
- `SIGNUP_ENABLED=false` exposes neither signup UI/client code nor `/api/signup`; `LIVE_TRADING=false` remains unchanged.
- Preserve logical focus order, ARIA live feedback, 200% zoom, 390px single-column layout, and 44px touch targets.
- Do not fabricate PnL, win rate, holdings, performance, or certainty.

## Avoid
- Gold wealth cues, neon, glassmorphism, gradients, ticker motion, repeated list reveals, parallax, or fake market pulses.
- Card-in-card inflation, oversized marketing headlines, and permanently exposed raw/debug data.
- Any implication of broker execution: this remains record-only paper trading.

## Evidence
Canonical decision: `Decision-Giraffe-Premium-Investor-UI-System`. Tool review confirms project-native CSS/vanilla implementation and Better-style accessibility/layout review as the final quality bar.
