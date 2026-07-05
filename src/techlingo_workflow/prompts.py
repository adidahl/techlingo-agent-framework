from __future__ import annotations

from textwrap import dedent
from typing import Any
from .config import WorkflowConfig, DifficultyLevel


SYSTEM_JSON_ONLY = dedent(
    """\
    You are Techlingo-ID-Architect, an expert Instructional Designer and Duolingo content specialist.
    You MUST follow instructions precisely.

    Output rules:
    - Return ONLY valid JSON.
    - Do NOT include markdown fences.
    - Do NOT include commentary or explanations.
    """
)


def difficulty_contract(difficulty: DifficultyLevel) -> str:
    # IMPORTANT: difficulty controls the LANGUAGE and scenario complexity, never
    # the amount of information. A beginner course still teaches the full
    # content of the source — it just explains it in plain words.
    if difficulty == DifficultyLevel.novice:
        return dedent(
            """\
            Difficulty: novice
            - Use extremely simple, everyday language and short sentences.
            - Define every technical term inline the first time it appears.
            - Simplify the WORDING, never the CONTENT: still teach every real fact,
              term, and distinction from the source. Do not dumb questions down to
              self-evident trivia.
            - Scenarios should be simple and everyday.
            """
        )
    if difficulty == DifficultyLevel.beginner:
        return dedent(
            """\
            Difficulty: beginner
            - Use simple language; define jargon inline when first used.
            - Keep questions short and concrete.
            - Simplify the WORDING, never the CONTENT: teach the full facts, terms,
              and distinctions from the source. A beginner question is easy to READ,
              not empty of substance.
            - Scenarios should be everyday workplace situations with minimal ambiguity.
            - Distractors should be common novice mistakes (real confusions between
              related concepts), never absurd options.
            - Feedback should be encouraging and explanatory.
            """
        )
    if difficulty == DifficultyLevel.intermediate:
        return dedent(
            """\
            Difficulty: intermediate
            - Moderate technical vocabulary is allowed; define uncommon terms briefly.
            - Scenarios should be realistic implementation tasks with some tradeoffs.
            - Distractors should reflect plausible misconceptions, not silly errors.
            - Feedback should highlight the key principle and the tradeoff.
            """
        )
    return dedent(
        """\
        Difficulty: advanced
        - Use more technical phrasing appropriate for practitioners (still clear and unambiguous).
        - Scenarios should involve operational constraints, governance, or failure modes.
        - Distractors should be subtle and realistic (edge cases, misapplied best practices).
        - Feedback should be precise and principle-driven.
        """
    )


def a1_modularizer_prompt(
    source_text: str,
    *,
    difficulty: DifficultyLevel,
    config: WorkflowConfig,
    override_title: str | None = None,
    analysis_json: str | None = None,
) -> str:
    target_title = override_title if override_title else "AI Core Capabilities and Responsibility"

    # Deterministic per-module lesson plan. The self-correction loop re-runs A2
    # (not A1), so A1's module/lesson counts are never fixed downstream — they
    # must be exact on the first try. Giving explicit per-module counts (instead
    # of a range) dramatically improves adherence. Target the top of the allowed
    # range and distribute as evenly as possible across modules.
    target_total = max(config.max_lessons_total, config.modules_count)
    base, rem = divmod(target_total, config.modules_count)
    per_module = [base + (1 if i < rem else 0) for i in range(config.modules_count)]
    lesson_plan = "\n".join(
        f"          - Module {i + 1}: EXACTLY {n} lesson(s)." for i, n in enumerate(per_module)
    )

    # Concepts per lesson must fit the exercise budget: with E exercises a lesson
    # can meaningfully cover at most E concepts, and demanding a perfect 1:1
    # mapping is brittle — cap at E-1 so one concept can carry two exercises.
    max_concepts = max(2, min(6, config.exercises_per_lesson - 1))

    inventory_section = ""
    if analysis_json:
        inventory_section = dedent(
            f"""\
            Content inventory (terms/definitions/explanations/examples extracted from the source —
            use it as a COVERAGE CHECKLIST: every inventory item must end up inside some lesson's concepts):
            {analysis_json}
            """
        )

    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Source Content:
        {source_text}

        {inventory_section}
        Task (A1 Modularizer - Curriculum Mapping & Chunking):
        Create a course map for: "{target_title}".

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.

        STRICT CONSTRAINT: You must cover ALL subjects, terms, and parts present in the source text. Do not miss any concepts.

        STRICT CONSTRAINT: SKIP meta/navigation content. Welcome paragraphs, "move on to the
        next unit", descriptions of what the course/module itself covers, and other prefatory
        text are NOT learnable material. Never create a lesson, SLO, or concept about the
        course itself — only about the domain subject matter.

        CONTENT PACKS: For every lesson, list the CONCEPTS it covers. A concept is one
        teachable atom from the source: a term with its definition, a distinction between two
        things, a fact, or a named example. These concepts are the item bank the question
        generator will draw from, so make each summary a complete, self-contained fact.
        - 2 to {max_concepts} concepts per lesson (each lesson has only {config.exercises_per_lesson} exercises,
          so more concepts than that cannot be covered).
        - Each concept has: id (kebab-case, unique across the WHOLE course), label, summary
          (the fact itself, 1-2 sentences, stated directly from the source), and
          confusable_with (ids of sibling concepts a learner could mix it up with — e.g.
          image-classification vs object-detection vs semantic-segmentation; these become
          distractor material).
        - Every substantive fact from the source must live in exactly one concept.
        - Concepts must be DISJOINT facts. If two candidate concepts are the same mechanism
          under different names (e.g., "speech recognition" and "speech-to-text"), MERGE
          them into ONE concept and mention the alternative name in its summary. Never
          create two concepts a single exercise could legitimately test at once.

        Constraints:
        - EXACTLY {config.modules_count} modules — no more, no fewer.
        - The number of lessons per module MUST be exactly:
{lesson_plan}
        - This gives EXACTLY {sum(per_module)} lessons in total (within the allowed {config.min_lessons_total}–{config.max_lessons_total}).
        - Each lesson must have exactly one SLO (single, clear, measurable learning objective)
          about the domain content (never about the course).
        - Keep lesson titles and SLOs novice-friendly (no unexplained jargon).

        Output JSON schema:
        {{
          "thought_process": [
            "Step 1: Analyzed source text...",
            "Step 2: identified key themes...",
            "Step 3: decided on module structure..."
          ],
          "title": "{target_title}",
          "modules": [
            {{
              "title": "Module Title",
              "lessons": [
                {{
                  "title": "Lesson Title",
                  "slo": "Single measurable objective",
                  "concepts": [
                    {{
                      "id": "object-detection",
                      "label": "Object detection",
                      "summary": "Object detection is a form of computer vision where the model is trained to identify the location of specific objects in an image.",
                      "confusable_with": ["image-classification", "semantic-segmentation"]
                    }}
                  ]
                }}
              ]
            }}
          ]
        }}
        """
    )


def _bloom_type_plan(config: WorkflowConfig) -> str:
    """Deterministic feasible Bloom-level assignment per question type.

    The coupling rule (Applying/Analyzing => choice types; true_false/fill_gaps/
    rearrange => Remembering/Understanding) is a constraint-satisfaction step
    LLMs routinely get wrong, so solve it here and hand A2 the finished plan.
    Feasibility is guaranteed by the WorkflowConfig validator.
    """
    blooms = config.blooms_distribution
    types = config.question_type_distribution
    higher = ["Applying"] * blooms.get("Applying", 0) + ["Analyzing/Evaluating"] * blooms.get(
        "Analyzing/Evaluating", 0
    )
    lower = ["Understanding"] * blooms.get("Understanding", 0) + ["Remembering"] * blooms.get(
        "Remembering", 0
    )
    lines: list[str] = []
    for qtype in ("single_choice", "multi_choice"):
        for _ in range(types.get(qtype, 0)):
            level = higher.pop(0) if higher else lower.pop(0)
            lines.append(f"          - {qtype}: {level}")
    for qtype in ("true_false", "fill_gaps", "rearrange"):
        for _ in range(types.get(qtype, 0)):
            lines.append(f"          - {qtype}: {lower.pop(0)}")
    return "\n".join(lines)


def a2_lesson_prompt(
    lesson_map_json: str,
    source_text: str,
    *,
    difficulty: DifficultyLevel,
    config: WorkflowConfig,
    course_title: str,
    module_title: str,
    other_lessons_note: str,
    tf_answers: list[bool],
    validation_issues: list[dict[str, Any]] | None = None,
) -> str:
    """Per-lesson generation prompt (chunked A2).

    One lesson per completion keeps output size bounded by lesson size, so
    exercises_per_lesson can grow without hitting output-token limits, and
    lesson calls can run concurrently.
    """
    blooms_reqs = "\n".join([f"- {k}: {v} exercises" for k, v in config.blooms_distribution.items()])
    type_reqs = "\n".join([f"    - {k}: {v}" for k, v in config.question_type_distribution.items()])
    bloom_plan = _bloom_type_plan(config)
    tf_pattern = ", ".join("false" if not a else "true" for a in tf_answers)

    # Construct feedback section if issues exist
    feedback_section = ""
    if validation_issues:
        issues_str = "\n".join([f"- {i['severity'].upper()} at {i['path']}: {i['message']}" for i in validation_issues])
        feedback_section = dedent(f"""
        CRITICAL INSTRUCTION - THE PREVIOUS VERSION OF THIS LESSON FAILED VALIDATION
        It had the following errors. You MUST fix them in this new attempt:
        {issues_str}

        Fix ONLY what the errors describe; everything else must still satisfy the
        constraints below (exact exercise count, type mix, Bloom assignment).
        """)

    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        {feedback_section}

        Source Text (the ground truth — every question, answer, and distractor must be
        grounded in it):
        {source_text}

        You are generating ONE lesson of the course "{course_title}", module "{module_title}".
        {other_lessons_note}

        This lesson's map entry (its "concepts" content pack is the item bank your
        exercises must cover):
        {lesson_map_json}

        Task (A2 Scaffolder - Q&A Generator, single lesson):
        Generate exactly {config.exercises_per_lesson} exercises for THIS lesson, drawing on its
        concepts and the source text, using this Bloom distribution:
        {blooms_reqs}

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.
        STRICT CONSTRAINT: Do NOT reference the source text in your questions.
        - Forbidden: "According to the text...", "As mentioned in the document...", "In this example...".
        - Required: Teach the concept directly as a fact. (e.g., "AI is..." instead of "The text says AI is...").

        CONCEPT COVERAGE (hard rules):
        - Every exercise MUST include "concept_id" set to the id of one concept from THIS lesson's concepts.
        - Cover as many DISTINCT concepts as possible: when the lesson has at least as many
          concepts as exercises, EVERY exercise must use a DIFFERENT concept_id. Only
          revisit a concept when there are more exercises than concepts.
        - Every exercise must test a DIFFERENT fact or aspect. NEVER re-ask the same fact
          with a different question type — that is the #1 failure mode to avoid. If two
          exercises share a concept, they must probe different details of it (e.g., its
          definition vs. its use case vs. how it differs from a sibling concept).
        - Copy the lesson's "concepts" array VERBATIM from the map entry into your output.

        BLOOM/TYPE COUPLING (hard rules):
        - Applying and Analyzing/Evaluating exercises MUST be single_choice or multi_choice,
          and their prompt TEXT must describe a realistic scenario with a decision point.
        - true_false, fill_gaps, and rearrange exercises MUST be Remembering or Understanding.
        - USE THIS EXACT ASSIGNMENT (it satisfies both distributions; you may swap
          Remembering<->Understanding between the lower-order types when it fits the content
          better, but NEVER put Applying or Analyzing/Evaluating on true_false/fill_gaps/rearrange):
{bloom_plan}

        DISTRACTOR POLICY (hard rules):
        - Every distractor must be PLAUSIBLE: a real concept, term, or fact from this course
          (prefer the concept's confusable_with siblings) or a documented common misconception.
        - FORBIDDEN: absurd, joke, or out-of-domain options (e.g., "Cook lunch", "Free snacks",
          "Paint walls", "Plant trees"). A learner who knows nothing should NOT be able to
          eliminate a distractor by common sense alone.
        - error_type must name the actual confusion (e.g., "confuses object detection with image classification").
        - Keep all options similar in length and grammatical style; the correct option must
          NOT be systematically the longest or most detailed one.
        - An option must never restate the prompt (no tautologies).

        VARIETY:
        - Vary the phrasing of prompts across exercises; never repeat an identical stem twice.

        Constraints:
        - exercises: exactly {config.exercises_per_lesson} items, with this exact mix:
        {type_reqs}
        - flashcards: exactly {config.flashcards_per_lesson} items.
          - STRICT CONSTRAINT: Atomic & Concise. Each flashcard must cover exactly ONE concept,
            and different flashcards must cover DIFFERENT concepts.
          - Front: specific term, question, or scenario (max 10 words).
          - Back: clear, direct definition or answer (max 15 words).
          - FORBIDDEN: Do NOT generate "summaries", "lists of items", or "overview" cards.
          - FORBIDDEN: Do NOT enable "List 3 types of..." style cards.
          - Good: "What is X?" -> "X is ..."
          - Good: "Action for X?" -> "Do Y."
        - Every exercise must include:
          - blooms_level (one of: Remembering, Understanding, Applying, Analyzing/Evaluating)
          - question_type: MUST be EXACTLY one of: single_choice, multi_choice, true_false, fill_gaps, rearrange. NEVER invent other values (e.g. "scenario_based" is INVALID).
          - prompt (learner-facing prompt; may include scenario/context)
          - concept_id (see CONCEPT COVERAGE above)
        - Keep answers concise and unambiguous.
        - single_choice:
          - options: 4 options, each has text + is_correct + (error_type for incorrect options)
          - exactly 1 option where is_correct=true
        - multi_choice:
          - options: 4 options, each has text + is_correct + (error_type for incorrect options)
          - 2 or 3 options where is_correct=true
          - Set option feedback/rationale/better_fit fields to null in A2 (they will be added later).
        - true_false:
          - prompt: The learner-facing question or instruction (e.g., "True or false?").
          - statement: the statement to judge
          - correct_answer: true/false
          - MANDATORY ANSWER PATTERN: this lesson has exactly {len(tf_answers)} true_false
            exercise(s); in the order you write them, their correct_answer values MUST be:
            [{tf_pattern}]. This pattern balances answers across the whole course.
          - A FALSE statement must be a true statement with exactly ONE detail swapped for a
            confusable sibling term or fact (minimal corruption) — e.g. attribute object
            detection's behavior to image classification. Never write absurd false statements.
        - fill_gaps:
          - parts: array of objects with discriminator field 'type'
            - {{"type":"text","text":"..."}}
            - {{"type":"gap","accepted_answers":["..."],"placeholder":"..."}}
          - MUST include EXACTLY ONE gap part (the target app has a single text input). Never create two or more gaps in one fill_gaps exercise.
          - The single gap may list multiple accepted_answers (synonyms/valid alternatives) — that is encouraged.
          - The gap must be a KEY TECHNICAL TERM (a concept label or a term from the source),
            never a generic word like "fair", "content", or "good".
          - STRICT CONSTRAINT: Semantic Coherence. The text surrounding the gap MUST provide enough context so that the accepted answer is the only logical choice. Do not create gaps where any random noun could fit.
        - rearrange:
          - word_bank: 4 to 8 tokens; each token is at most 4 words.
          - correct_order: list of tokens in correct order.
          - correct_order must use the same tokens as word_bank.
          - PREFER ordering the steps of a process described in the source (e.g., how a model
            is trained and then used). Sentence reconstruction is allowed only for definition
            sentences, split into genuinely reorderable pieces — never 2-3 giveaway chunks.
          - STRICT CONSTRAINT: exactly ONE valid order. The app grades against a single
            correct_order, so every other arrangement of the tokens must be wrong (ungrammatical
            or logically broken). NEVER build the sentence around a comma-separated list of
            interchangeable items (e.g. "power chatbots, create content, translate text") —
            those items can be reordered and still be correct, which makes the exercise
            unfair. If the fact you want to test is a list, use multi_choice instead.
          - Task must be "Reconstruct the sentence" or "Order the steps". Do NOT use scenarios for this type.

        Output JSON schema — return ONE lesson object (NOT a course, NOT an array):
        {{
          "thought_process": [
            "Step 1: Reviewed the lesson concepts...",
            "Step 2: Assigned exercises to concepts...",
            "Step 3: Verified the type mix and Bloom assignment..."
          ],
          "title": "Lesson Title (keep from the map entry)",
          "slo": "SLO (keep from the map entry)",
          "concepts": [
            {{ "id": "...", "label": "...", "summary": "...", "confusable_with": ["..."] }}
          ],
          "exercises": [
            {{
              "blooms_level": "Remembering|Understanding|Applying|Analyzing/Evaluating",
              "question_type": "single_choice|multi_choice|true_false|fill_gaps|rearrange",
              "prompt": "...",
              "concept_id": "...",

              "options": [
                {{ "text": "...", "is_correct": true, "error_type": null, "feedback": null, "rationale": null, "better_fit": null }},
                {{ "text": "...", "is_correct": false, "error_type": "...", "feedback": null, "rationale": null, "better_fit": null }}
              ],

              "statement": "...",
              "correct_answer": true,

              "parts": [
                {{ "type": "text", "text": "..." }},
                {{ "type": "gap", "accepted_answers": ["..."], "placeholder": "..." }}
              ],

              "word_bank": ["..."],
              "correct_order": ["..."]
            }}
          ],
          "flashcards": [
            {{ "front": "...", "back": "...", "hint": "..." }}
          ]
        }}
        """
    )
    
def a3_lesson_prompt(lesson_json: str, source_text: str, *, difficulty: DifficultyLevel, config: WorkflowConfig) -> str:
    blooms_counts = "/".join([str(v) for v in config.blooms_distribution.values()])
    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Source Text (ground truth for every scenario detail):
        {source_text}

        Input lesson JSON (one lesson of a larger course):
        {lesson_json}

        Task (A3 Merrill’s Agent - Scenario Designer, single lesson):
        Rewrite this lesson's exercises to ensure contextual relevance and stylistic variety:

        CRITICAL: `question_type` MUST stay EXACTLY one of:
        single_choice, multi_choice, true_false, fill_gaps, rearrange.
        NEVER invent other values (e.g. "scenario_based" is INVALID). "Scenario-based"
        below describes the wording of the `prompt` text, NOT the question_type.

        1. **Higher-Order Thinking (Applying, Analyzing/Evaluating)**:
           - These are always single_choice or multi_choice. The `prompt` TEXT must describe a real-world scenario.
           - Include a brief scenario + clear decision point/problem in the prompt text.
           - Ground the scenario in a concrete situation from the source (e.g., the source's
             own examples: admissions systems, loan approval, retail stock monitoring) rather
             than a generic office setting.

        2. **Lower-Order Thinking (Remembering, Understanding)**:
           - MUST be **DIRECT** questions (NO "Scenario:" prefix, NO "You are a..." framing).
           - Focus on clear, concise concept checking.
           - Exception: If the concept is abstract, a very brief example is okay, but avoid full role-play scenarios.

        3. **Specific Type Constraints**:
           - **rearrange**: Do NOT use scenarios. Prompt should be "Arrange the following steps..." or "Reconstruct the sentence...".
           - **fill_gaps**: Do NOT use scenarios. Prompt should be a direct statement with missing key terms. Ensure the sentence makes sense grammatically even with the gap. Keep EXACTLY ONE gap part — never split into multiple gaps.

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge.
        STRICT CONSTRAINT: No Meta-References.
        - The scenarios and questions must exist in the real world, not "in the document".
        - NEVER say "As described in the text".

        Scenario requirements (for Applying/Analyzing only, excluding rearrange/fill_gaps):
        - Problem-centered trigger event
        - Relatable protagonist (role/title)
        - Clear decision point in the question

        Constraints:
        - Preserve the lesson's "concepts" array and every exercise's "concept_id" EXACTLY as given.
        - Preserve Bloom level counts ({blooms_counts}).
        - Preserve each exercise's question_type, order, and required fields.
        - Preserve every true_false exercise's correct_answer value EXACTLY (do not flip answers).
        - Keep distractors plausible (sibling concepts / real misconceptions); never introduce
          absurd or out-of-domain options while rewriting.
        - For fill_gaps and rearrange, keep the structure valid (parts/word_bank/correct_order).
        - Do not add or remove exercises.
        - Do not add or remove flashcards.
        - Keep the correct answer semantically correct.

        Output: return the FULL updated lesson JSON (same schema as the input lesson —
        a single lesson object, NOT a course).

        IMPORTANT: Start your JSON with a "thought_process" field (array of strings) explaining your decisions for the scenario updates.
        """
    )


def a4_lesson_prompt(lesson_json: str, source_text: str, *, difficulty: DifficultyLevel, config: WorkflowConfig) -> str:
    return dedent(
        f"""\
        {difficulty_contract(difficulty)}

        Source Text (ground truth — every explanation must be verifiable against it):
        {source_text}

        Input lesson JSON (one lesson of a larger course):
        {lesson_json}

        Task (A4 Feedback Architect - Instructional Coaching, single lesson):
        You must populate all feedback fields for every exercise in this lesson.
        
        For ALL single_choice and multi_choice exercises:
        - Add 'feedback_for_correct' (1-2 sentences reinforcing why the answer is right).
        - Add a 'rationale' (2-3 sentences) for EVERY option (both correct and incorrect).
        - Add a 'better_fit' (1-2 sentences) for EVERY incorrect option.
        - For EVERY incorrect option, you MUST add a 'feedback' object with:
            - intrinsic: realistic consequence within the context of the scenario (never a software system error unless the context IS software systems).
            - instructional: conversational coaching that explains the violated principle.

        For true_false exercises:
        - Add 'feedback_for_correct' (1-2 sentences).
        - Add 'feedback_for_incorrect' object (intrinsic + instructional) explaining why the user's choice was wrong.

        For fill_gaps / rearrange:
        - Add 'feedback_for_correct' (brief reinforcement).

        STRICT CONSTRAINT: Use ONLY information present in the source text. Do not use external knowledge. Verify that every explanation can be pointed to in the source text.
        
        NEGATIVE CONSTRAINTS:
        - Do NOT use terms like 'SLO', 'learning objective', 'system', 'tool', or 'AI'.
        - Do NOT frame feedback as 'The system will...' or 'The tool suggests...'.
        - Do NOT reference "the text", "the document", or "the example" in feedback.
        - Explain the concept as an expert, not as someone reading a book.
        - Ensure feedback sounds like a human mentor, not a software debugger.

        Constraints:
        - Populate 'rationale' for all options.
        - Populate 'better_fit' for all incorrect options.
        - Ensure 'feedback' object (intrinsic + instructional) is present for ALL incorrect options in single/multi choice.
        - Ensure 'feedback_for_correct' is present for ALL exercises.
        - Ensure 'feedback_for_incorrect' is present for ALL true_false exercises.
        - Do not remove existing fields.
        - Preserve the lesson's "concepts" array and every exercise's "concept_id" EXACTLY as given.
        - Preserve every true_false exercise's correct_answer value EXACTLY.
        - Keep feedback/rationale concise and learner-friendly.
        - Do not add or remove exercises or flashcards.

        Output: return the FULL updated lesson JSON (a single lesson object, NOT a course).

        IMPORTANT: Start your JSON with a "thought_process" field (array of strings) explaining your feedback generation strategy.
        """
    )


def a5_source_check_prompt(course_json: str, source_text: str) -> str:
    return dedent(
        f"""\
        You are a precise Fact Checker. Flag ONLY confirmed, concrete defects.

        Source Text:
        {source_text}

        Course Content:
        {course_json}

        Task:
        Report an issue ONLY when you are highly confident it is a real defect and
        can quote the exact offending text. Flag ONLY these categories:

        1. **Hallucinations**: The CORRECT ANSWER states a fact that is NOT supported
           anywhere in the source text. Quote the unsupported claim. (Do NOT flag
           mere paraphrasing or reasonable summarization of source content.)
        2. **Formatting Artifacts**: A literal leftover string from scraping/PDFs is
           present (e.g., "Expand table", "Image 1", "Click to view", "Page 5"). Quote it.
        3. **true_false integrity**: The `statement` field is NOT a declarative
           statement that can be judged true/false, OR the statement text itself
           lists multiple options to choose from. A scenario/context sentence
           BEFORE a judgeable statement is ACCEPTABLE — do NOT flag it.
        4. **Meta-References**: The content refers to the SOURCE MATERIAL itself —
           phrases like "the text says", "as described in the document", "the section
           above", "this example shows". Quote the phrase.
           - NOT a meta-reference: the words "document", "text", "example", etc. used as
             normal DOMAIN vocabulary (e.g., "document analysis", "extract data from
             documents", "text classification", "a text message"). Only flag references
             to the source material the course was built from.

        STRICT SCOPING RULES (to avoid false positives):
        - DO NOT flag `rearrange` exercises for the correct_order "restating",
          "mirroring", "matching", or being "redundant with" the prompt — that is
          the INTENDED nature of a rearrange task and is NOT a defect.
        - DO NOT flag lesson `concepts` summaries for paraphrasing or restating the
          source — restating source facts is exactly what they are for.
        - DO NOT report stylistic preferences, "could be clearer", "should be
          rephrased", or anything hedged with "appears", "potentially", "might",
          "slightly", or "needs checking". If you are not certain it is a real
          defect, do not report it. NEVER report an issue whose own description
          concludes no defect is confirmed.

        Output JSON schema:
        {{
          "thought_process": ["Step 1: ...", "Step 2: ..."],
          "issues": [
            {{
              "path": "modules[0].lessons[0].exercises[0]",
              "message": "Confirmed defect with exact quote, e.g. Formatting artifact 'Expand table' present."
            }}
          ]
        }}

        Return ONLY valid JSON. If no confirmed defects, return {{ "issues": [] }}.
        """
    )


def a5_tf_rebalance_prompt(items_json: str, source_text: str, *, to_false: bool = True) -> str:
    """Targeted micro-repair: flip the truth value of selected T/F statements.

    Whole-course repair proved unreliable for this (models regenerate structure);
    a narrow rewrite of just the affected statements is far safer.
    """
    src, dst = ("TRUE", "FALSE") if to_false else ("FALSE", "TRUE")
    how = (
        "swap exactly ONE detail for a plausible confusable sibling term or fact from the "
        "same subject area (e.g., attribute object detection's behavior to image classification)"
        if to_false
        else "correct the one wrong detail so the statement states the actual fact"
    )
    return dedent(
        f"""\
        You are an assessment editor. Below are true/false quiz statements that are all
        currently {src}. Rewrite EACH listed statement so it becomes {dst} using minimal
        corruption: {how}. Keep the wording style and length; never make the statement
        absurd or obviously wrong.

        STRICT CONSTRAINT: Ground every detail in the source text below. A false statement
        must be something a real learner could mistakenly believe.

        Source Text:
        {source_text}

        Statements to flip (JSON):
        {items_json}

        Output JSON schema (one fix per input item, same "index" values):
        {{
          "fixes": [
            {{
              "index": 0,
              "statement": "The rewritten, now-{dst} statement.",
              "feedback_for_correct": "1 sentence confirming the right judgment.",
              "feedback_for_incorrect": {{
                "intrinsic": "Realistic consequence of judging it wrongly.",
                "instructional": "Coaching that states the correct fact."
              }}
            }}
          ]
        }}

        Return ONLY valid JSON.
        """
    )


def a5_lesson_repair_prompt(lesson_json: str, issues_json: str, config: WorkflowConfig) -> str:
    """Per-lesson repair (chunked A5). Only lessons with errors get repaired,
    so clean lessons can never be damaged by a whole-course rewrite."""
    blooms_reqs = ", ".join([f"{v} {k}" for k, v in config.blooms_distribution.items()])
    type_reqs = "\n".join([f"          - {k}: {v}" for k, v in config.question_type_distribution.items()])

    return dedent(
        f"""\
        You must repair ONE lesson of a course so it satisfies all constraints.
        Return ONLY the corrected lesson JSON (a single lesson object, NOT a course).

        IMPORTANT: Start your JSON with a "thought_process" field (array of strings) explaining the repairs you are making.

        Constraints to satisfy:
        - The lesson has exactly {config.exercises_per_lesson} exercises.
        - Bloom distribution: {blooms_reqs}.
        - Exercise type mix (exact counts within the {config.exercises_per_lesson} exercises):
{type_reqs}
        - The lesson has exactly {config.flashcards_per_lesson} flashcards.
        - **Bloom/type coupling**: Applying and Analyzing/Evaluating exercises MUST be
          single_choice or multi_choice with a scenario + decision point in the prompt text.
          true_false, fill_gaps, and rearrange MUST be Remembering or Understanding.
        - **Concept coverage**: every exercise must have a `concept_id` matching one concept
          from the lesson's `concepts` array; cover as many distinct concepts as possible
          (when concepts >= exercises, every exercise uses a different concept).
          Preserve the `concepts` array.
        - **No duplicates**: no two exercises may test the same fact (even with different
          question types). If flagged as near-duplicates, rewrite one to target a different
          fact or aspect of the concept.
        - **Distractors**: every distractor must be a plausible sibling concept or real
          misconception from the course; REPLACE absurd/out-of-domain options (e.g., "Cook
          lunch") with plausible ones. No option may restate the prompt.
        - For scenario-based single_choice and multi_choice exercises: each incorrect option must include paired feedback (intrinsic + instructional).
        - For single_choice and multi_choice exercises:
          - ALL options must have a 'rationale' (2-3 sentences explaining why it is correct/incorrect).
          - ALL incorrect options must have a 'better_fit' (1-2 sentences describing where it would be correct).
        - **True/False answers**: keep every true_false exercise's correct_answer value
          EXACTLY as it is (the answer pattern is balanced course-wide); fix only the
          statement/prompt wording when flagged.
        - For fill_gaps: Ensure grammatical correctness, semantic coherence, that context uniquely determines the answer, and that there is EXACTLY ONE gap part. The gap must be a key technical term, never a generic word.
        - For rearrange: word_bank must have 4-8 tokens, each at most 4 words; the final order forms a valid grammatical sentence or logical process steps. Exactly ONE order may be valid — never comma-separated interchangeable list items (any other arrangement must be wrong).
        - **Formatting**: REMOVE all scraping artifacts (e.g., "Expand table", "Image 1", "click here").
        - **Integrity**: TRUE/FALSE questions MUST be statements (declarative sentences), NOT instructions (e.g., "Choose the tool").
        - **Schema**: TRUE/FALSE exercises MUST have both:
            - `prompt`: The learner-facing question/scenario (e.g., "True or false?").
            - `statement`: The core statement to judge (e.g., "The tool is appropriate.").
        - **Logic**: Ensure all questions and answers are logically valid and properly structured.
        - **Meta-References**: REMOVE all pointers to "the text", "the document", or "examples above". Rewrite as direct statements. Never write questions ABOUT the course itself (e.g., "What is this course about?").

        Validation issues for THIS lesson (paths are relative to the course; ignore the
        module/lesson prefix and fix the referenced exercises):
        {issues_json}

        Current lesson JSON:
        {lesson_json}
        """
    )



def analyzer_prompt(source_text: str) -> str:
    return dedent(
        f"""\
        You are an expert Text Analyst and Linguist.
        Your goal is to break down the source text into its constituent parts to ensure absolutely NOTHING is missed.
        
        Source Text:
        {source_text}
        
        Task:
        Analyze the text and identify EVERY:
        - Term (key vocabulary)
        - Definition (explicit or implicit)
        - Explanation (how things work, why they matter)
        - Example (illustrations of concepts)
        - Analogy (comparisons)
        - Subject (main topics)

        You must also recommend a course structure (Workflow Config) based on the depth and breadth of the content.
        - modules_count: typically 1 (since input is usually a single module/unit). Only suggest >1 if content is massive and clearly distinct sections.
        - lessons_total: typically 5-15 depending on content length.
        - exercises_per_lesson: typically 15-30 (as requested for robust practice).
        - flashcards_per_lesson: typically 6-10
        - blooms_distribution: how many of each level per lesson (Remembering/Understanding/Applying/Analyzing/Evaluating)
        - question_type_distribution: exact mix of question types per lesson. 
          STRICT CONSTRAINTS:
          1. Must sum EXACTLY to exercises_per_lesson.
          2. Use ONLY these keys: "single_choice", "multi_choice", "true_false", "fill_gaps", "rearrange".
          3. DO NOT use "short_answer" or any other keys.

        
        Output JSON schema:
        {{
            "thought_process": [
                "Step 1: Reading text...",
                "Step 2: Identified main subjects...",
                "Step 3: Extracting terms..."
            ],
            "input_summary": "Brief summary of the text",
            "parts": [
                {{
                    "type": "term|definition|explanation|example|analogy|subject",
                    "content": "The actual text snippet or summary of the part",
                    "context": "Optional context if needed"
                }}
            ],
            "metadata": {{
                "total_parts": 0,
                "parts_by_type": {{
                    "term": 0,
                    "definition": 0,
                    "explanation": 0,
                    "example": 0,
                    "analogy": 0,
                    "subject": 0
                }},
                "estimated_questions_needed": 0,
                "completeness_score": 0.0
            }},
            "recommended_config": {{
                "difficulty": "beginner|intermediate|advanced",
                "modules_count": 0,
                "min_lessons_total": 0,
                "max_lessons_total": 0,
                "exercises_per_lesson": 0,
                "flashcards_per_lesson": 0,
                "blooms_distribution": {{
                    "Remembering": 0,
                    "Understanding": 0,
                    "Applying": 0,
                    "Analyzing/Evaluating": 0
                }},
                "question_type_distribution": {{
                    "single_choice": 0,
                    "multi_choice": 0,
                    "true_false": 0,
                    "fill_gaps": 0,
                    "rearrange": 0
                }}
            }}
        }}
        
        STRICT CONSTRAINT FOR 'difficulty':
        You MUST pick exactly ONE value: "beginner", "intermediate", or "advanced".
        Do NOT combine them (e.g., "beginner-to-intermediate" is INVALID).
        pick the single closest difficulty level.
        """
    )


def reviewer_prompt(source_text: str, current_analysis_json: str) -> str:
    return dedent(
        f"""\
        You are a meticulous Content Reviewer.
        Your goal is to check if the previous analysis missed ANYTHING from the source text.
        
        Source Text:
        {source_text}
        
        Current Analysis:
        {current_analysis_json}
        
        Task:
        1. Compare the Source Text against the Current Analysis.
        2. Identify any missing Terms, Definitions, Explanations, Examples, Analogies, or Subjects.
        3. If items are missing, add them.
        4. If items are incorrect, fix them.
        6. Verify and Refine the `recommended_config`:
           - Ensure it pushes for deep learning (e.g., higher Bloom's levels).
           - Ensure exercises_per_lesson is 15-30.
           - Ensure question_type_distribution sums EXACTLY to exercises_per_lesson.
           - Ensure question_type_distribution ONLY uses keys: "single_choice", "multi_choice", "true_false", "fill_gaps", "rearrange".
           - REMOVE any invalid keys like "short_answer".
        
        STRICT CONSTRAINT FOR 'difficulty':
        Ensure 'difficulty' is exactly one of: "beginner", "intermediate", "advanced".
        If it contains a range like "beginner-to-intermediate", CHANGE it to the single highest level (e.g., "intermediate").
        
        Output the FULL validated/corrected JSON using the same schema.
        
        IMPORTANT: Start your JSON with a "thought_process" field (array of strings) detailing your review findings and what you fixed.
        """
    )
