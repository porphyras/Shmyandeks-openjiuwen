"""Negative controls: ensure the new tests reject selected plausible mistakes.

python3 bench/tools/check_test_sensitivity.py

Mutations exist only inside mock.patch contexts in this process. No source files
are edited, no model is called, and no third-party packages are needed.
This is a curated smoke check, not an exhaustive mutation-testing score.
"""
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness import fastpath  # noqa: E402
import run_agent  # noqa: E402
from test_metamorphic import MetamorphicTests  # noqa: E402
from test_run_faults import RunFaultTests  # noqa: E402
from test_inverse_search import InverseSearchTests  # noqa: E402


def mutations():
    shipping = list(fastpath._OPS)
    shipping[0] = (shipping[0][0], lambda stock, rate, days: stock + rate * days)
    rounding = list(fastpath._OPS)
    rounding[2] = (rounding[2][0], lambda stock, parts: (stock + parts - 1) // parts)
    all_regions = list(fastpath._DATA_Q)
    rx, _, aggregate = all_regions[3]
    all_regions[3] = (rx, lambda row, region: True, aggregate)
    original_sentences = fastpath._sentences
    original_walk = fastpath._walk
    original_init = run_agent.Run.__init__
    original_inverse_base = fastpath._inverse_base

    def ignore_second_mentee(task):
        base = original_inverse_base(task)
        if base is None:
            return None
        cards, titles = base
        cards.pop("daniel young", None)
        return cards, titles

    def allow_truncated_base(task):
        cards = fastpath._cards(task)
        if not cards:
            return None
        titles = {text.splitlines()[0][2:].strip().lower(): text.splitlines()[0][2:].strip()
                  for _, text in fastpath.ctx.kb_docs(task)}
        return cards, titles

    def no_second_pass(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.second_pass = False

    return [
        ("shipping adds instead of subtracting", MetamorphicTests,
         "test_shipping_expansion_and_receipt_cancellation", patch.object(fastpath, "_OPS", shipping)),
        ("split uses ceil instead of floor", MetamorphicTests,
         "test_rounding_boundaries_and_unknown_suffixes", patch.object(fastpath, "_OPS", rounding)),
        ("unknown sentence is silently dropped", MetamorphicTests,
         "test_rounding_boundaries_and_unknown_suffixes",
         patch.object(fastpath, "_sentences", lambda s: [x for x in original_sentences(s)
                                                      if not x.startswith("It reserves")])),
        ("revenue ignores region filter", MetamorphicTests,
         "test_table_permutation_duplication_and_irrelevant_rows",
         patch.object(fastpath, "_DATA_Q", all_regions)),
        ("unstated rounding tie is forced upward", MetamorphicTests,
         "test_unstated_rounding_ties_fall_back",
         patch.object(fastpath, "_nearest", lambda total, n: (2 * total + n) // (2 * n))),
        ("knowledge-base traversal skips one hop", MetamorphicTests,
         "test_kb_file_order_distractors_and_counterfactual_fact",
         patch.object(fastpath, "_walk", lambda task, name, chain: original_walk(task, name, chain[1:]))),
        ("knowledge-base answer is a memorized constant", MetamorphicTests,
         "test_kb_file_order_distractors_and_counterfactual_fact",
         patch.object(fastpath, "_walk", lambda *args: ("solved", "Graz"))),
        ("final answer recovery is disabled", RunFaultTests,
         "test_quota_exhaustion_preserves_incremental_answers_and_stops_calls",
         patch.object(run_agent.Run, "rewrite_answers", lambda self: None)),
        ("technical errors never get a second pass", RunFaultTests,
         "test_transport_error_does_not_discard_other_tasks_and_gets_second_pass",
         patch.object(run_agent.Run, "__init__", no_second_pass)),
        ("inverse Mentor edge is followed forward", InverseSearchTests,
         "test_direction_and_mixed_chains",
         patch.object(fastpath, "_walk_inverse", lambda task, name, chain:
                      original_walk(task, name, ["Mentor" if edge == "^Mentor" else edge for edge in chain]))),
        ("second mentee is ignored", InverseSearchTests,
         "test_zero_or_multiple_matches_never_answer_or_declare_infeasible",
         patch.object(fastpath, "_inverse_base", ignore_second_mentee)),
        ("truncated KB is treated as complete", InverseSearchTests,
         "test_truncated_unreadable_or_unsupported_documents_fall_back",
         patch.object(fastpath, "_inverse_base", allow_truncated_base)),
    ]


def run_test(case, name):
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream).run(unittest.TestSuite([case(name)]))
    return result, stream.getvalue()


def main():
    mutants = mutations()
    # A test already failing on the real code cannot demonstrate sensitivity.
    for case, name in dict.fromkeys((case, name) for _, case, name, _ in mutants):
        result, output = run_test(case, name)
        if not result.wasSuccessful():
            print("BASELINE FAILED: " + name)
            print(output)
            return 1
    killed = 0
    for label, case, name, mutation in mutants:
        with mutation:
            result, output = run_test(case, name)
        if result.failures and not result.errors:
            killed += 1
            print("DETECTED: " + label)
        else:
            print(("TEST ERROR: " if result.errors else "MISSED: ") + label)
            if result.errors:
                print(output)
    print("Detected %d/%d curated mutations" % (killed, len(mutants)))
    return 0 if killed == len(mutants) else 1


if __name__ == "__main__":
    sys.exit(main())
