"""Tests for the course workspace (workspace.py) and concept-graph merge
(graph_merge.py) — all deterministic, no LLM.

Run without pytest:  PYTHONPATH=src python tests/test_workspace.py
Or with pytest:      PYTHONPATH=src pytest tests/test_workspace.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from techlingo_workflow.graph_merge import merge_source_concepts
from techlingo_workflow.models import ConceptAtom
from techlingo_workflow.workspace import (
    BankFlashcard,
    BankItem,
    BuildState,
    CompileConfig,
    Concept,
    ConceptGraph,
    Curriculum,
    CurriculumLesson,
    CurriculumModule,
    LessonBank,
    SourceState,
    Workspace,
    derive_rung,
    init_workspace,
    make_item_key,
    natural_sort_key,
    payload_hash,
)


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def test_derive_rung_mapping():
    # R1: recall × recognition
    assert derive_rung("Remembering", "single_choice") == 1
    assert derive_rung("Remembering", "multi_choice") == 1
    assert derive_rung("Remembering", "true_false") == 1
    # R2: understanding × recognition
    assert derive_rung("Understanding", "single_choice") == 2
    assert derive_rung("Understanding", "true_false") == 2
    # R3: production mechanic (Bloom/type coupling guarantees R/U here)
    assert derive_rung("Remembering", "fill_gaps") == 3
    assert derive_rung("Understanding", "rearrange") == 3
    # R4/R5: scenario application/analysis
    assert derive_rung("Applying", "single_choice") == 4
    assert derive_rung("Analyzing/Evaluating", "multi_choice") == 5


def test_natural_sort_key_orders_numbered_files():
    names = ["10. Ten.md", "2. Two.md", "1. One.md"]
    assert sorted(names, key=natural_sort_key) == ["1. One.md", "2. Two.md", "10. Ten.md"]


def test_make_item_key_handles_missing_concept():
    assert make_item_key("lesson-a", "llm-vs-slm", 2, 1) == "lesson-a/llm-vs-slm/r2/v1"
    assert make_item_key("lesson-a", None, 1, 3) == "lesson-a/general/r1/v3"


# ---------------------------------------------------------------------------
# Workspace IO round-trips
# ---------------------------------------------------------------------------


def _make_ws(tmp: Path) -> Workspace:
    src_dir = tmp / "docs"
    src_dir.mkdir()
    (src_dir / "2. Second.md").write_text("# Second\ncontent b", encoding="utf-8")
    (src_dir / "1. First.md").write_text("# First\ncontent a", encoding="utf-8")
    return init_workspace(
        tmp / "courses" / "demo",
        course_id="demo",
        title="Demo Course",
        source_files=list(src_dir.iterdir()),
    )


def test_init_workspace_creates_layout_and_sorted_sources():
    with tempfile.TemporaryDirectory() as td:
        ws = _make_ws(Path(td))
        assert ws.exists()
        assert ws.load_meta().id == "demo"
        assert [p.name for p in ws.iter_sources()] == ["1. First.md", "2. Second.md"]
        assert ws.load_graph().concepts == []
        assert ws.load_curriculum().modules == []
        assert ws.load_build_state().sources == {}
        # Phase-2 compile defaults: levels + checkpoints on (ARCHITECTURE §5).
        cfg = ws.load_compile_config()
        assert (cfg.levels, cfg.checkpoints, cfg.final_review) == (3, "per_module", True)
        assert cfg.recycle == {"l2": 0.40, "l3": 0.30} and cfg.seed == 901
        assert cfg.experience.max_same_mechanic_streak == 2
        assert cfg.experience.relaxation_order[-1] == "concept_adjacency"
        assert cfg.sequence_quality.block_on_errors is True
        assert cfg.gauntlet.qualitative_required_for_publication is False


def test_compile_quality_configuration_fails_closed_when_incoherent():
    with pytest.raises(ValidationError, match="min_mechanics_per_window"):
        CompileConfig.model_validate(
            {"experience": {"mechanics_window_size": 2, "min_mechanics_per_window": 3}}
        )
    with pytest.raises(ValidationError, match="relaxation_order"):
        CompileConfig.model_validate(
            {"experience": {"relaxation_order": ["mechanic_streak"] * 4}}
        )
    with pytest.raises(ValidationError, match="configured together"):
        CompileConfig.model_validate({"gauntlet": {"critic_backend": "codex"}})
    with pytest.raises(ValidationError, match="required when qualitative QA"):
        CompileConfig.model_validate(
            {"gauntlet": {"qualitative_required_for_publication": True}}
        )


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"session_size_hint": 0}, "session_size_hint"),
        ({"recycle": {"l2": -0.01}}, "between 0 and 1"),
        ({"recycle": {"l3": 1.01}}, "between 0 and 1"),
        ({"recycle": {"l2": float("nan")}}, "finite values"),
        ({"recycle": {"l4": 0.2}}, "unknown recycle levels"),
        ({"recycle": {"l2": True}}, "not booleans"),
        ({"experince": {}}, "Extra inputs are not permitted"),
        (
            {"experience": {"max_same_mechanic_strek": 2}},
            "Extra inputs are not permitted",
        ),
        (
            {"sequence_quality": {"block_on_erors": True}},
            "Extra inputs are not permitted",
        ),
        (
            {"gauntlet": {"critic_backed": "codex"}},
            "Extra inputs are not permitted",
        ),
    ],
)
def test_compile_configuration_rejects_invalid_values_and_unknown_keys(
    payload, message
):
    with pytest.raises(ValidationError, match=message):
        CompileConfig.model_validate(payload)


def test_compile_configuration_keeps_valid_partial_recycle_maps_compatible():
    cfg = CompileConfig.model_validate(
        {
            "session_size_hint": 1,
            "recycle": {"l2": 0},
            "experience": {
                "max_same_ui_family_streak": 3,
                "relaxation_order": [
                    "mechanics_window",
                    "true_false_answer_streak",
                    "mechanic_streak",
                    "concept_adjacency",
                ],
            },
        }
    )
    assert cfg.recycle == {"l2": 0.0}
    assert cfg.experience.relaxation_order[2:4] == [
        "ui_family_streak",
        "mechanic_streak",
    ]


def test_init_workspace_refuses_overwrite():
    with tempfile.TemporaryDirectory() as td:
        ws = _make_ws(Path(td))
        try:
            init_workspace(ws.root, course_id="demo", title="x", source_files=[])
            assert False, "expected WorkspaceError"
        except Exception as e:
            assert "already contains" in str(e)


def test_workspace_roundtrips_graph_curriculum_bank_state():
    with tempfile.TemporaryDirectory() as td:
        ws = _make_ws(Path(td))

        graph = ConceptGraph(
            concepts=[
                Concept(
                    id="llm-vs-slm",
                    label="LLM vs SLM",
                    summary="Large vs small language models.",
                    confusable_with=["fine-tuning"],
                    source={"file": "1. First.md"},
                    lessons=["intro"],
                )
            ]
        )
        ws.save_graph(graph)
        assert ws.load_graph() == graph

        curriculum = Curriculum(
            modules=[
                CurriculumModule(
                    key="m1",
                    title="First",
                    source_file="1. First.md",
                    lessons=[CurriculumLesson(key="intro", title="Intro", slo="Explain X", concepts=["llm-vs-slm"])],
                )
            ]
        )
        ws.save_curriculum(curriculum)
        assert ws.load_curriculum() == curriculum

        payload = {"question_type": "true_false", "statement": "s", "correct_answer": True, "blooms_level": "Understanding", "prompt": "p"}
        bank = LessonBank(
            lesson="intro",
            module="m1",
            items=[
                BankItem(
                    item_key="intro/llm-vs-slm/r2/v1",
                    concept_id="llm-vs-slm",
                    rung=2,
                    variant=1,
                    payload=payload,
                    payload_hash=payload_hash(payload),
                )
            ],
            flashcards=[BankFlashcard(front="f", back="b")],
        )
        ws.save_bank(bank)
        assert ws.load_bank("intro") == bank
        assert [b.lesson for b in ws.iter_banks()] == ["intro"]

        state = BuildState(
            workflow_config_hash="abc",
            sources={"1. First.md": SourceState(sha256="x", status="ok", built_at="t", module_keys=["m1"], validation_ok=True)},
        )
        ws.save_build_state(state)
        assert ws.load_build_state() == state

        ws.delete_bank("intro")
        assert list(ws.iter_banks()) == []


# ---------------------------------------------------------------------------
# Concept graph merge — stable ids
# ---------------------------------------------------------------------------


def _atoms_file1() -> dict[str, list[ConceptAtom]]:
    return {
        "what-is-genai": [
            ConceptAtom(
                id="generative-ai",
                label="Generative AI",
                summary="AI that generates new content such as text, images, and code.",
                confusable_with=["nlp"],
            ),
            ConceptAtom(
                id="nlp",
                label="Natural language processing",
                summary="Techniques for making sense of human language.",
            ),
        ],
        "agents-basics": [
            ConceptAtom(
                id="ai-agents",
                label="AI agents",
                summary="Applications with a model, instructions, and tools that automate tasks.",
            ),
        ],
    }


def test_merge_first_pass_creates_all():
    result = merge_source_concepts(ConceptGraph(), _atoms_file1(), source_file="1. First.md")
    assert sorted(result.created) == ["ai-agents", "generative-ai", "nlp"]
    assert result.matched == [] and result.retired == []
    graph = result.graph
    genai = graph.by_id()["generative-ai"]
    assert genai.lessons == ["what-is-genai"]
    assert genai.confusable_with == ["nlp"]  # remapped and kept
    assert genai.source == {"file": "1. First.md"}


def test_merge_rebuild_is_id_stable():
    first = merge_source_concepts(ConceptGraph(), _atoms_file1(), source_file="1. First.md")
    second = merge_source_concepts(
        first.graph,
        _atoms_file1(),
        source_file="1. First.md",
        replaced_lesson_keys={"what-is-genai", "agents-basics"},
    )
    assert second.created == [] and second.retired == []
    assert sorted(set(second.matched)) == ["ai-agents", "generative-ai", "nlp"]
    # Identical graphs — ids, lessons, everything.
    assert second.graph == first.graph


def test_merge_owner_rebuild_refreshes_wording():
    first = merge_source_concepts(ConceptGraph(), _atoms_file1(), source_file="1. First.md")
    atoms = _atoms_file1()
    atoms["what-is-genai"][0] = ConceptAtom(
        id="generative-ai",
        label="Generative AI",
        summary="A branch of AI that produces original content from prompts.",
    )
    second = merge_source_concepts(
        first.graph, atoms, source_file="1. First.md", replaced_lesson_keys={"what-is-genai", "agents-basics"}
    )
    genai = second.graph.by_id()["generative-ai"]
    assert "original content" in genai.summary  # owner rebuild refreshed it
    assert second.created == []


def test_merge_cross_file_match_adds_lesson_but_keeps_wording():
    first = merge_source_concepts(ConceptGraph(), _atoms_file1(), source_file="1. First.md")
    atoms_file2 = {
        "nlp-deep-dive": [
            ConceptAtom(
                id="natural-language-processing",  # different slug, same concept
                label="Natural Language Processing",
                summary="A different wording about understanding language.",
            )
        ]
    }
    second = merge_source_concepts(first.graph, atoms_file2, source_file="2. Second.md")
    assert second.created == []
    assert second.id_remap["natural-language-processing"] == "nlp"
    nlp = second.graph.by_id()["nlp"]
    assert "making sense of human language" in nlp.summary  # first wording kept
    assert nlp.lessons == ["what-is-genai", "nlp-deep-dive"]
    assert nlp.source == {"file": "1. First.md"}  # owner unchanged


def test_merge_id_collision_gets_suffixed():
    first = merge_source_concepts(ConceptGraph(), _atoms_file1(), source_file="1. First.md")
    atoms_file2 = {
        "vision-lesson": [
            ConceptAtom(
                id="nlp",  # accidental id collision, unrelated content
                label="Object detection",
                summary="Locating objects in images with bounding boxes.",
            )
        ]
    }
    second = merge_source_concepts(first.graph, atoms_file2, source_file="2. Second.md")
    assert second.id_remap["nlp"] == "nlp-2"
    assert second.graph.by_id()["nlp-2"].label == "Object detection"
    assert second.graph.by_id()["nlp"].label == "Natural language processing"


def test_merge_retires_missing_and_protects_pinned():
    first = merge_source_concepts(ConceptGraph(), _atoms_file1(), source_file="1. First.md")
    graph = first.graph
    graph.by_id()["ai-agents"].pinned = True

    atoms = _atoms_file1()
    del atoms["agents-basics"]  # agents no longer extracted
    atoms["what-is-genai"] = [atoms["what-is-genai"][0]]  # nlp no longer extracted

    second = merge_source_concepts(
        graph, atoms, source_file="1. First.md", replaced_lesson_keys={"what-is-genai", "agents-basics"}
    )
    by_id = second.graph.by_id()
    assert by_id["nlp"].status == "retired"
    assert "nlp" in second.retired
    assert by_id["ai-agents"].status == "active"  # pinned survives
    assert by_id["generative-ai"].status == "active"


def test_merge_never_retires_other_files_concepts():
    first = merge_source_concepts(ConceptGraph(), _atoms_file1(), source_file="1. First.md")
    atoms_file2 = {
        "vision-lesson": [
            ConceptAtom(id="computer-vision", label="Computer vision", summary="AI that interprets images and video.")
        ]
    }
    second = merge_source_concepts(first.graph, atoms_file2, source_file="2. Second.md")
    assert second.retired == []
    assert {c.id for c in second.graph.concepts if c.status == "active"} == {
        "generative-ai", "nlp", "ai-agents", "computer-vision",
    }


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    return failures


if __name__ == "__main__":
    import sys

    sys.exit(1 if _run_all() else 0)
