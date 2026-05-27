# Algorithm reconciliation: PRD §7.8 vs `personal-content-resurface/algorithms.md`

## The mismatch

PRD §7.8 (planning/connecting-dots-shimmying-forest.md) defines the hybrid as:

```
score = w1·recency_decay + w2·forgetting_curve + w3·context_relevance + w4·entity_overlap
```

The skill reference (`~/.claude/skills/personal-content-resurface/references/algorithms.md` §"Custom context-aware hybrid") defines it as:

```
score = w_t·time_decay + w_r·activity_relevance + w_p·static_profile_match
        - λ·diversity_penalty(item, already_selected)
```

Four components claimed in both, but only **one** overlaps cleanly
(`recency_decay` ≈ `time_decay`). `forgetting_curve` and `entity_overlap` are PRD
inventions absent from the reference; `static_profile_match` and the
`diversity_penalty` MMR term are reference inventions absent from the PRD.

## Recommendation: **(b) update the PRD to match the reference.**

Three reasons:

1. **The reference is the implementation contract.** It has pseudocode, cold-start
   defaults, a surface-fatigue guard, and an evaluation rubric. The PRD has a
   formula and weights. Aligning the smaller artifact to the larger one is less
   work and lower risk.
2. **`forgetting_curve` belongs to FSRS/SM-2, not the hybrid.** PRD §7.8 already
   keeps SM-2 as the A/B fallback (`--algo sm2`). Putting a forgetting-curve
   term *inside* the hybrid double-counts that signal and muddies the A/B.
3. **`entity_overlap` is a special case of `activity_relevance`.** If
   `activity_embedding` is computed from this week's queries + content, an item
   sharing entities with active themes will already score high on cosine
   similarity. A dedicated entity term adds noise without adding signal until
   the entity graph (#11) is mature — and by then the regression in PRD §G1
   Phase B can learn the weighting empirically.

## Proposed PRD §7.8 replacement

```
score(item) = w_t·time_decay(item)
            + w_r·activity_relevance(item, recent_activity)
            + w_p·static_profile_match(item, profile)
            - λ·diversity_penalty(item, already_selected_today)
```

Phase A defaults: `w_t=0.3, w_r=0.4, w_p=0.3, λ=0.7`. Cold-start and A/B-vs-SM-2
logic from PRD §7.7 carry over unchanged. The `diversity_penalty` is an MMR-style
term applied during top-K selection, not per-item.

## Migration

- Update `planning/connecting-dots-shimmying-forest.md` §5.3, §7.8, and §G1 to
  use the four-component form above.
- Leave the skill reference (`algorithms.md`) untouched.
- Component #14 (digest builder) implements directly against the reference.
