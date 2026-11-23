# AI usage: where the tokens go, and how to spend fewer

Audit of 2026-11-23, from the code (no production numbers yet: see "Measure first"). Prices are in `clipcrow/costs.py` (USD per million tokens, input / output).

## Where Claude is called

Every call goes through `AnthropicLLM.generate` (`planner/llm.py`), one model for everything: `$CLIPCROW_MODEL`, default **claude-opus-5-5** ($4 / $20). The service never passes a cheaper scoring model (`score_llm` is only wired in the CLI).

| Call (`purpose`) | Where | Input | Output | How often |
|---|---|---|---|---|
| `text-refine` | `textfix.refine`, Marathi/Hindi only, before the cuts | the WHOLE transcript, 25 sentences per call | the whole transcript again (corrected text) | every job in mr/hi; output is the dear side ($20) |
| `retake-judge` | `cleancut.judge_retakes` (cleanup) | the whole recording, one call up to 500 lines | small (cut ids, intent, passages) | each cleaned job |
| `retake-verify` | `cleancut.verify_cuts` | the whole recording again, with cuts marked | small | each cleaned job |
| `score` | `plan.plan_edl` pass 1 | every window, 6 per call; windows overlap (stride 0.6), so each sentence is sent about **1.7 times** | small | each non-"whole" job |
| `detail:<id>` | pass 2 | one window each, `ceil(1.5 x clips) + 1` windows | clips and copy (title, hook, stat) | each job |
| `repair:<id>` | pass 2 | the same prompt again plus a suffix | same | when a title or hook came out in the wrong script |
| `beats:<clip>` | explainer, per clip | the clip's sentences, plus **images** of the person's pictures | beats | explainer jobs only |
| `style:captions` | reference-video look | frames (images) | small | per style from a reference |

A run that fails and is run again re-pays for every call before the failure. Since 48826c3 a restart or "Continue from" step skips finished steps, which already removes most of that waste.

## What is wasteful or risky

1. **One expensive model for easy work.** Scoring, text correction and the final read-through are classification or light editing. They run on the same model as the creative copy.
2. **Scoring sends the transcript about 1.7 times.** The overlap exists so a moment straddling a boundary stays whole in one window.
3. **`text-refine` re-emits every sentence**, including the ones that were already right. Output tokens cost 5x input.
4. **Nothing is cached.** Same transcript, same prompt, same model gives a fresh paid answer on every retry, re-plan or "Run again". The ReplayLLM exists for tests only.
5. **`repair:` resends the whole detail prompt.** Wrong-script titles are a retry of a failure that a cheaper check could often avoid.
6. **No budget.** `max_tokens` is 8000 per call but there is no cap per job or per day; a long recording or many pictures has no ceiling.
7. **Abandoned calls are still billed.** `Interruptible` drops the answer when a job is stopped, but the call finishes in the background.
8. **Pictures** (explainer) are sent as base64 JPEGs with each beats call; image tokens are a large share of that call.

## Plan, in order of saving for effort

| # | Change | Expected effect | Risk |
|---|---|---|---|
| 1 | **Measure first**: a per-purpose table (tokens and USD by `purpose`, per job and per day) in Operator. The data is already recorded per call (`edl.ai.calls`); this is aggregation and a card | tells which of the rest matters | none |
| 2 | **A cheaper model per purpose** (`CLIPCROW_MODEL_SCORE`, `_REFINE`, `_VERIFY`, `_JUDGE`; default = today). Try `claude-haiku-5-5` ($0.10 / $0.50, 40x cheaper input than today) for `score`, `text-refine`, `retake-verify`; `claude-sonnet-5-5` ($2 / $10) for `retake-judge`. Keep the strongest model for `detail`, `beats` | likely the largest single saving; scoring and refine are most of the volume | quality: compare on 3 to 5 real recordings (same transcript, both models), check that the picked windows and corrected sentences agree before switching the default |
| 3 | **Cache LLM answers on disk per job** keyed by hash(model, purpose, system, user): a re-run, retry or resume with the same inputs costs nothing. Images hash by bytes | removes all retry waste, including "Run again" | none for correctness; the key must include the model and the prompt text |
| 4 | **Refine only what needs it**: send just the sentences where the recogniser was unsure or the text has typical Marathi/Hindi slips (word confidence below a threshold, Latin letters inside Devanagari, a small list of known confusions). Return only changed sentences, as it does now | cuts the input and above all the output of `text-refine` by a large share | a missed correction; keep a switch for "refine everything" |
| 5 | **Score less**: stride 0.6 to 0.75 and 8 windows per call (about 1.3 x instead of 1.7 x); or a free pre-filter (drop windows that are mostly filler by word count, speech rate, energy) before any call | about 20 to 30 percent off scoring | a straddling moment is found less often; check on real recordings |
| 6 | **Prompt caching** for the system prompt and the shared instructions in `detail` and `beats` | small (the prompts are short) but free; worth it only if the cached prefix reaches the minimum size | none |
| 7 | **A per-job and per-day token budget** with a clear failure ("budget reached, continue from this step") and an Operator setting | an upper bound on a bad day | a job stops early; surface it in the job page |
| 8 | **Skip `repair:`** when the title is already acceptable, or repair only the failing fields with a short prompt | small | low |
| 9 | **Pictures**: downscale to the size the model needs (about 1024 px long side) before base64; send each picture once per clip, not per call | smaller `beats` input on explainer jobs | cropping accuracy; keep the original for the crop step |

Suggested order: 1, 3, 2 (with the comparison), 4, 7, then 5, 8, 9, 6.

## Measure first

Before changing a default, run 3 to 5 real recordings (a Marathi talk, a Hindi one, an English one, one cleaned, one explainer) and record tokens and USD per `purpose` for the current setup. Then run the same transcripts with the cheaper models, and compare outputs (picked windows, corrected sentences, cuts), not only the bill. Keep the comparison in `.claude/development-log.md`.
