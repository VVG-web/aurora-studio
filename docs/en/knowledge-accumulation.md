# Accumulating knowledge in a card

A summary of the requirement behind the rule "the unit of the base is an entity". The detailed requirements document
(Russian, ships to projects with the engine and is pinned by tests): [../накопление-знания.md](../накопление-знания.md).
The rule itself — [knowledge-rules.md](knowledge-rules.md), section 4.

Status: **done in 1.100.32**, except rebuilding live bases. Opened on 2026-09-03 after analysing a live project where
`kb:twins` found 493 groups of duplicates — 1266 cards, a third of the base.

The duplicates turned out to be not an implementation defect but a **direct consequence of the parsing architecture**.

## The requirement

**The unit of the base is an entity, not a source.**

1. **Accumulation.** Five artifacts speak about an analytical balance — the base has one card "Analytical balance" that
   accumulates knowledge from all five. Not five cards and not "Analytical balance (from the spec)".
2. **Extraction.** An independent entity was explained inside a card — it is moved into its own card and a link takes its
   place. "Receives information from the FCOD subsystem, which is part of the MinFin payment-processing project" →
   "receives information from the subsystem `[[FCOD]]`" plus a card "FCOD" with the definition moved into it.

The base must grow **in depth**: fewer new names than new facts.

## What did not exist before 1.100.32, and how it was solved

| Gap | How it was solved |
|---|---|
| Parsing did not see the base: it received only a source's sections and could not know about the four earlier documents on the same entity | candidates from the semantic index go into the parsing prompt; a record `{"into": "<card>"}`; a symmetric check by the critic |
| "Accumulate" did not exist as an operation: a name was taken by another source → a refusal addressed to a human, which the model read as "invent another name" (twelve cards about one process) | `build_plan --append`; the block per source; `distilled` is removed; idempotent; whitelisted for the agent; the refusal text rewritten into a proposal to add |
| No extraction from text: only the reverse direction (a link without a card → a stub) | `agent:extract`: a piece is found in the thesis by substring and moved character by character; not found — the whole move is cancelled |
| A card was tied to one source (`source:` is one field) | `sources:` as a list and a single reader `card_sources()`; schema step 5 → 6; trust takes the **weakest** source |
| Duplicates were decided by a human | `agent:twins`: "one entity or different" — by the text; `--merge` is open to the agent, `--merge-all` is not |

A thesis from several sources is assembled with a caveat: a divergence in a number, term or condition is **named**, with
where each value came from, not averaged.

## What is left

1. **Rebuild the live bases.** The 493 duplicate groups were accumulated by the old parsing; merging them one by one is
   more expensive than rebuilding by the new rules. The order is ready: terms first, candidates in parsing, accumulation,
   extraction, merging by the model.
2. **`kb:split` cuts by size.** It is a separate operation — slicing a bloated document — and it is not the same as
   extracting an entity. Check on a live base that they do not fight: a card from which definitions were moved out may
   stop being "bloated".
3. **Check the measure on a rebuilt base** (below).

## How to check that it is done

- `kb:twins` on a freshly built base gives units of groups, not hundreds;
- one parse of five documents about one entity gives one card with five sources;
- `ops:search-quality` does not fall: an accumulated card must not lose to a search for its own thesis;
- the share of cards with a non-empty `based_on` grows — knowledge started being used instead of lying around.
