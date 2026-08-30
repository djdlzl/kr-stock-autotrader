<!-- Curated from 21st design context plus Giraffe product constraints. -->
# Giraffe Design Context

## Design read

A trust-first Korean paper-trading product for non-expert users, using a calm SEED-inspired product language rather than a marketing-page aesthetic.

- Taste dials: variance 3, motion 2, density 5.
- One warm orange accent and warm-neutral paper surfaces.
- Motion is limited to feedback and state transitions.
- Paper-only safety and authentication boundaries outrank decoration.

## Stack and reuse rule

- FastAPI with server-rendered HTML, vanilla CSS, and vanilla JavaScript.
- Reuse the project shells and semantic tokens before adding primitives.
- 21st.dev sign-in, dashboard, form, button-group, wizard, card, and empty-state collections are composition references only.
- Do not import React/shadcn components into this app solely to imitate a reference.

## Required product patterns

- Public root: authentication shell only.
- Authenticated app: status summary, plan list, progressive create/tick panels.
- Labels above inputs, explicit helper/error text, disabled/loading states.
- Human-readable Korean status and condition copy.
- Empty, loading, error, session-expired, and destructive-confirmation states.
- 44px minimum touch targets, visible focus, reduced-motion support, mobile single column.

## Visual tokens

- Primary: `#ff6f0f`; primary low: `#fff0e5`.
- Paper: `#fffaf6`; sheet: `#ffffff`.
- Ink: `#1f1d1b`; low ink: `#6d6863`; divider: `#e9e1da`.
- Semantic status: success `#126b49`, warning `#a95b00`, danger `#c73524`.
- Base spacing: 8px. Inputs 12px radius, cards 16px, authentication shell 20px.

## Avoid

- Official Daangn logos, characters, or implied affiliation.
- Private controls or data in unauthenticated HTML.
- Raw debug JSON.
- Decorative gradients, glass effects, excessive motion, card inflation, or oversized headlines.
- New frontend build infrastructure without a direct requirement.

## Evidence limits

`21st search` requires an authenticated 21st account in this environment. Public 21st category pages and the official 21st build/review skills were used. Local `21st review` reports zero reviewed files for Python-embedded HTML, so manual source and Safari runtime review remains authoritative.
