# AI-901 Gauntlet Recovery Prompt

Work autonomously in:

`/Users/adnanribic/techlingo-agent-framework`

Continue the AI-901 qualitative-publication task. Preserve the intentionally dirty worktree. Never pull, reset, revert, clean, discard, or overwrite unrelated changes. Never manually edit generated question banks to manufacture a pass. Never weaken deterministic, source-fidelity, provenance, publication, or qualitative thresholds, and never fabricate Gauntlet evidence.

## Verified baseline

- Branch `main`, baseline HEAD `705c1664b354d3def5d5085b27b328bf9a8c53b3`.
- All 12 AI-901 sources are `status=ok` and `validation_ok=true` in `courses/ai-901/build_state.json`.
- Source rebuild configuration SHA-256: `fcd6b996703cdfa67a3cb2c29668b7ceafa8f94651b8adfc9ac3e79cd64d9c4b`.
- Deterministic in-memory artifact SHA-256: `69c9d18537cc04d4d24f6ffad549e8d9c17ee1b85b2ba8dc167de6c8be33ae73`.
- Deterministic audit passes for 229 units, 2,160 active source item keys, and 2,419 placements, with zero sequence errors, relaxations, or traceability failures.
- Course quality passes with 47 non-blocking warnings.
- Full test suite passes: 287 tests.
- The first complete qualitative run persisted 229 coherent histories and 230 rounds. Only two units were eligible; 227 required human review. Do not delete, rewrite, or reinterpret those histories.

## Diagnosed orchestration defect

The critic produced 215 valid evaluations. Of those, 209 contained a below-threshold dimension and 201 set `human_review_recommended=true`, while only one was below the configured `0.65` confidence threshold. The controller treated the boolean as an immediate terminal stop before applying an otherwise actionable `largest_gap`. The editor ran only twice and the comparator never ran.

The critic prompt also instructed the model to lower confidence whenever no approved reference exemplar existed. Luna repeatedly treated the absence of an exemplar as justification for human review even though authoritative source material was supplied. Approved references are pedagogical exemplars, not substitutes for authoritative source evidence.

## Required correction

1. Preserve backward readability of existing immutable history.
2. Bind a new evaluator-protocol version into the canonical Gauntlet policy/context hash so old and new judgments cannot be confused.
3. Add a structured blocking human-review reason to new critic results. Accept only narrowly defined reasons such as:
   - insufficient authoritative source evidence;
   - conflicting authoritative source evidence;
   - answer ambiguity that cannot be safely resolved from the supplied source;
   - no bounded repair can preserve deterministic identity/correctness constraints;
   - external subject-matter expertise is genuinely required.
4. Require new live critic outputs to keep the review boolean and structured reason coherent.
5. Explicitly instruct the critic:
   - the supplied source material is authoritative;
   - absence of an approved reference exemplar may modestly reduce numeric confidence but must never by itself request human review;
   - ordinary below-threshold findings, including critical but source-resolvable defects, are repairable when a narrow source-bounded directive exists;
   - for a repairable artifact, return `human_review_recommended=false`, no blocking reason, and exactly one actionable `largest_gap`;
   - request human review only when safe automatic repair cannot be derived from the supplied evidence.
6. Keep human review terminal when a coherent blocking reason exists or confidence is below the configured hard human-review threshold.
7. Do not relax the `0.80` quality thresholds, protected dimensions, deterministic hard gate, fresh source-fidelity critic, independent editor/comparator, or position-swapped comparison.

## Regression requirements

Add tests proving that:

- a repairable below-threshold result reaches the editor even when no approved exemplar exists;
- absence of an approved exemplar does not itself request human review;
- an incoherent review boolean/reason is rejected and retried as semantic output failure;
- a concrete blocking reason stops before editing;
- confidence below the hard human-review threshold stops before editing;
- edited challengers still must pass deterministic and source-fidelity gates;
- a valid challenger reaches independent position-swapped comparison;
- old history records without the new optional field remain readable.

Run focused tests, the complete test suite, the deterministic AI-901 audit, course quality, and `git diff --check`.

## Calibrated pilot

After code and tests pass, run a fresh independent-Luna pilot on a small representative set rather than immediately rerunning all 229 units. Include existing passes, session-ordering defects, prompt-repetition defects, answer ambiguity, factual-fidelity defects, a very short L3 unit, and a checkpoint.

Audit whether:

- repairable findings reach the editor;
- edits stay within declared keys, paths, fields, and scope;
- deterministic and fresh source-fidelity gates reject unsafe challengers;
- valid challengers reach both position-swapped comparisons;
- promotions are supported by stable comparison evidence;
- human review is reserved for genuine blocking uncertainty.

Proceed to a fresh all-unit run only if the pilot demonstrates those behaviors. Preserve the original histories and bind new evidence to the current artifact, evaluation context, policy hash, source hashes, and exact models.

The first v2 pilot attempt on `counting-important-terms-l3` was stopped after it exposed an evidence-addressing defect: Luna repeatedly placed JSON navigation expressions and source filenames in structured artifact `paths`. That immutable failed record must remain preserved. Protocol v3 must explicitly require every structured item key/path citation to be copied literally from `actual_final_artifact.items[*].item_key` or `.path`; source citations belong in evidence prose, and artifact-wide evidence may leave location arrays empty.

Protocol v3 then produced a clean one-call pass for `counting-important-terms-l3`, but the first repairable pilot (`ai-agents-working-together-l2`) exposed a scope inconsistency: the critic selected session scope while instructing the editor to rewrite `question_text`. Session scope is reorder-only, so three editor attempts were correctly rejected. Preserve that immutable record. Protocol v4 must make scope semantics explicit, reject live session directives with non-empty `allowed_payload_fields`, and require exact editor change-reporting metadata.

Protocol v4 subsequently exercised the complete critic/editor/hard-gate/source-fidelity/two-position-comparator loop and safely promoted one bounded proposal for `ai-agents-working-together-l2`, although the unit later plateaued. Its short-unit pilot, `how-computers-represent-images-l3`, then exposed unsafe mechanic conversion: Luna invented `single_select` and later malformed a multiple-choice payload; both challengers failed hard gates. Preserve both records. Protocol v5 must protect `question_type` from automatic Gauntlet edits and classify defects that require adding/removing/replacing items or changing mechanics as `unsafe_or_unbounded_repair` for the authored rebuild workflow.

Protocol v5 then produced the expected one-call authored-rebuild review for the short unit, a clean one-call pass for `build-document-extraction-in-code-l3`, and safe champion retention for `build-an-image-analysis-app-l2`. The latter exposed that JSON/schema repair attempts inside one CLI adapter call were persisted as only one backend call. Preserve those records. Protocol v6 must count every physical completion attempt made by `LLMClient.run_json_model` so usage and retry evidence are not understated.

## Current verified v6 state and next action

- Protocol v6 passed a live independent-Luna smoke test on `meaning-with-embeddings-l3`: success, eligible, confidence `0.91`, and one recorded physical backend call.
- Full suite: 295 passed, 108 deprecation warnings.
- Focused Gauntlet/publication/backend suite: 94 passed.
- Deterministic audit remains PASS for artifact `69c9d18537cc04d4d24f6ffad549e8d9c17ee1b85b2ba8dc167de6c8be33ae73`, 229 units, 2,419 questions, zero sequence errors, zero relaxations, and 47 non-blocking warnings.
- Course quality remains PASS with zero schema or sequence errors.
- There are 238 immutable history records across legacy and protocols v2-v6; all load with zero coherence errors.
- No files are staged, and HEAD remains `705c1664b354d3def5d5085b27b328bf9a8c53b3` on `main`.

Do not launch the all-229 v6 rerun yet. The pilot proved correct fail-closed orchestration, but also proved that:

1. promoted edited champions remain proposals and are not applied to canonical authored banks;
2. there is currently no course CLI command for reviewing and applying a proposal through the authored rebuild path;
3. repairable units can take 30-65 minutes at high reasoning, especially when semantic/source-review retries occur;
4. rerunning unchanged canonical units would repeatedly rediscover defects without incorporating supported repairs.

The next implementation task is a reviewable, hash-bound proposal workflow—not silent auto-application. It should:

- list only Gauntlet rounds with a promoted edited champion and show exact item-key, field, before/after, source-fidelity, hard-gate, and comparator evidence;
- export a human-review artifact without changing banks;
- require explicit approval of the exact proposal/history/artifact hashes;
- map approved item changes back to the authoritative authored source/bank path, rejecting ambiguous mappings;
- preserve protected mechanics, stable identities, concepts, answers, and source provenance;
- rebuild the affected source through the normal Terra A0-A5 workflow rather than directly patching a generated compiled unit;
- rerun deterministic audit and a fresh v6 Gauntlet evaluation against the new artifact hash;
- keep authored-rebuild cases such as `unsafe_or_unbounded_repair` in a separate human queue.

Only after that workflow is implemented, tested, and used to incorporate approved repairs should the representative pilot continue and the all-unit v6 rerun be reconsidered.

Compile, stage, commit, or push only after every source, deterministic audit, qualitative gate, publication-safety check, full test, and diff check genuinely passes. Otherwise report the exact blocker and next supported action.
