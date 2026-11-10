"""Run with: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s socratic-tutor/evals/tests."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


EVALS = Path(__file__).resolve().parents[1]
CHECKER = EVALS / "check_transcript.py"
FIXTURES = EVALS / "transcripts"
sys.path.insert(0, str(EVALS))
import check_transcript as checker


def turns(*items):
    return [{"role": role, "content": content} for role, content in items]


def skill_map(level=0, count=4, topic="Fractions"):
    names = ["Equivalent fractions", "Comparing", "Adding", "Reducing"]
    names.extend("Practice {}".format(i) for i in range(4, count))
    rows = ["{} [{}] {}/5".format(name, "█" * (2 * level) + "░" * (10 - 2 * level), level)
            for name in names[:count]]
    return "Topic: {}  overall: 0%\n".format(topic) + "\n".join(rows)


def lesson(map_text=None, diagnostic_count=3):
    items = [("user", "Teach me fractions.")]
    for i in range(diagnostic_count):
        items.extend([("assistant", "What is your reasoning for example {}?".format(i)),
                      ("user", "The pieces have to be equal.")])
    if map_text is not None:
        items.append(("assistant", map_text))
    return turns(*items)


class CommandLineTests(unittest.TestCase):
    def run_checker(self, path, *args):
        return subprocess.run(
            [sys.executable, "-B", str(CHECKER), str(path), *args],
            capture_output=True, text=True, check=False,
        )

    def test_good_fixture_has_no_findings(self):
        result = self.run_checker(FIXTURES / "good.json", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["target_level"], 4)

    def test_stable_json_and_target_context(self):
        first = self.run_checker(FIXTURES / "good.json", "--json", "--target-level", "5")
        second = self.run_checker(FIXTURES / "good.json", "--json", "--target-level", "5")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(first.stdout)["target_level"], 5)

    def test_invalid_input_exits_two_in_both_formats(self):
        for options in [(), ("--json",)]:
            result = self.run_checker(CHECKER, *options)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            if options:
                self.assertIn("error", json.loads(result.stdout))
            else:
                self.assertIn("Invalid input", result.stderr)

    def test_missing_file_and_invalid_target_exit_two(self):
        for path, options in [(EVALS / "absent.json", ("--json",)),
                              (FIXTURES / "good.json", ("--target-level", "6"))]:
            self.assertEqual(self.run_checker(path, *options).returncode, 2)

    def test_cli_json_lines_and_invalid_records(self):
        cases = [(' {"role":"user","content":"Teach me fractions."}\n'
                  '{"role":"assistant","content":"What is a denominator?"}\n', 0),
                 ('{"role":"user","content":null}\n', 2),
                 ('{"role":"user","content":"Hi"}\nnot json', 2),
                 ('[' * 2000 + ']' * 2000, 2)]
        for content, expected in cases:
            with tempfile.NamedTemporaryFile(mode="w", dir=FIXTURES, suffix=".json", delete=False) as handle:
                handle.write(content)
                path = Path(handle.name)
            try:
                result = self.run_checker(path, "--json")
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIsInstance(json.loads(result.stdout), dict)
                self.assertNotIn("Traceback", result.stderr)
            finally:
                path.unlink()

    def test_cli_rejects_lone_surrogates_and_accepts_astral_characters(self):
        cases = [(json.dumps([{"role": "assistant", "content": "Great job \ud800"}]), 2),
                 (r'{"\ud800":1,"\ud800":2}', 2),
                 (json.dumps([{"role": "assistant", "content": "Great job 🎲"}]), 0)]
        for content, expected in cases:
            for options in [(), ("--json",)]:
                with self.subTest(content=content, options=options):
                    with tempfile.NamedTemporaryFile(mode="w", dir=FIXTURES, suffix=".json", delete=False) as handle:
                        handle.write(content)
                        path = Path(handle.name)
                    try:
                        result = self.run_checker(path, *options)
                        self.assertEqual(result.returncode, expected, result.stderr)
                        self.assertNotIn("Traceback", result.stderr)
                        if options:
                            self.assertIn("error" if expected == 2 else "findings", json.loads(result.stdout))
                        elif expected == 2:
                            self.assertIn("Invalid input", result.stderr)
                    finally:
                        path.unlink()

    def test_default_text_report_explains_limits(self):
        result = self.run_checker(FIXTURES / "good.json")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Target level: 4/5", result.stdout)
        for phrase in ["withholding", "strict grading", "misattributed insight",
                       "spaced review", "final assessment", "textual detection"]:
            self.assertIn(phrase, result.stdout.lower())

    def test_each_failure_fixture_is_isolated(self):
        rules = {"one-question", "ends-on-question", "diagnostic-length", "map-format",
                 "level-needs-two-checks", "level-step", "progress-command",
                 "just-tell-me-scope", "no-flattery", "guard", "save-state"}
        self.assertEqual({p.stem for p in FIXTURES.glob("*.json")}, rules | {"good"})
        source = " ".join((EVALS.parent / "SKILL.md").read_text().split())
        for rule in sorted(rules):
            with self.subTest(rule=rule):
                result = self.run_checker(FIXTURES / (rule + ".json"), "--json")
                report = json.loads(result.stdout)
                self.assertEqual([f["rule"] for f in report["findings"]], [rule])
                severity = "warning" if rule in {"no-flattery", "level-step"} else "violation"
                self.assertEqual(result.returncode, 0 if severity == "warning" else 1)
                finding = report["findings"][0]
                self.assertEqual(finding["severity"], severity)
                self.assertIsInstance(finding["message_index"], int)
                self.assertGreaterEqual(finding["message_index"], 0)
                self.assertTrue(0 < len(finding["excerpt"]) <= 120)
                self.assertIn(finding["source"], source)
                self.assertTrue(finding["message"])


class InputTests(unittest.TestCase):
    def test_json_array_and_json_lines(self):
        messages = [{"role": "user", "content": "Teach me fractions.\nStart small."},
                    {"role": "assistant", "content": "What is a denominator?"}]
        self.assertEqual(checker.load_transcript(json.dumps(messages)), messages)
        self.assertEqual(checker.load_transcript("\n".join(map(json.dumps, messages))), messages)
        self.assertEqual(checker.load_transcript("[]"), [])

    def test_rejects_invalid_shapes_and_nonstandard_json(self):
        inputs = ["", "null", "true", "1", '"text"', "[{}]", "[[]]", "[null]",
                  '{"role":"user","content":"hi","extra":0}',
                  '{"role":"system","content":"hi"}',
                  '{"role":true,"content":"hi"}',
                  '{"role":"user","content":null}',
                  '{"role":"user","content":3}',
                  '{"role":"user","content":false}',
                  '{"role":"user","content":NaN}',
                  '{"role":"user","role":"assistant","content":"hi"}',
                  '[{"role":"user","content":"hi"},]',
                  '{"role":"user","content":"hi"}\n[]']
        for text in inputs:
            with self.subTest(text=text), self.assertRaises(ValueError):
                checker.load_transcript(text)


class RuleTests(unittest.TestCase):
    def findings(self, messages, rule=None):
        findings = checker.check_transcript(messages)["findings"]
        return [f for f in findings if rule is None or f["rule"] == rule]

    def test_questions_ignore_fenced_code_and_maps(self):
        for fence in ["```", "~~~~"]:
            text = fence + "text\nWhat? Why?\n" + fence + "\nHow do equal pieces help?"
            self.assertEqual(self.findings(turns(("user", "Teach me fractions."), ("assistant", text))), [])
        text = skill_map(topic="Fractions?").replace("Comparing", "Comparing?")
        self.assertEqual(self.findings(lesson(text + "\nWhat comes next?")), [])
        self.assertEqual(self.findings(lesson("```text\n" + text + "\n```")), [])

    def test_substantive_code_or_map_after_question_is_not_ignored(self):
        for suffix in ["\n```text\nhint\n```", "\n" + skill_map(), "\nTry equal pieces."]:
            findings = self.findings(lesson() + turns(("assistant", "Why?" + suffix)), "ends-on-question")
            self.assertEqual(len(findings), 1)

    def test_closing_quotes_brackets_and_whitespace_are_allowed(self):
        for ending in ['?"', "?’", "?)]}\n", "?  \n"]:
            self.assertEqual(self.findings(turns(("user", "Teach me fractions."),
                                                ("assistant", "Why" + ending))), [])

    def test_one_off_answer_does_not_enter_teaching_loop(self):
        self.assertEqual(self.findings(turns(("user", "What is a fraction?"),
                                            ("assistant", "A part of a whole. Why? How? Examples follow."))), [])

    def test_diagnostic_boundaries_and_incomplete_diagnostic(self):
        for count in [2, 3, 5, 6]:
            findings = self.findings(lesson(skill_map(), count), "diagnostic-length")
            self.assertEqual(len(findings), 0 if count in [3, 5] else 1)
        self.assertEqual(self.findings(lesson(diagnostic_count=6), "diagnostic-length"), [])

    def test_map_count_boundaries_preserve_narrow_topic_exception(self):
        for count, severity in [(1, "warning"), (3, "warning"), (4, None), (10, None), (11, "violation")]:
            findings = self.findings(lesson(skill_map(count=count)), "map-format")
            self.assertEqual([f["severity"] for f in findings], [] if severity is None else [severity])

    def test_map_levels_bars_and_overall(self):
        for level in range(6):
            self.assertEqual(self.findings(lesson(skill_map(level=level))), [])
        for bad in [skill_map().replace("░░░░░░░░░░", "░░░░░░░░░", 1),
                    skill_map().replace("░░░░░░░░░░", "██░░░░░░░░", 1),
                    skill_map().replace("0/5", "6/5", 1),
                    skill_map().replace("0/5", "-1/5", 1),
                    skill_map().replace("  overall: 0%", "")]:
            self.assertTrue(self.findings(lesson(bad), "map-format"))
        self.assertEqual(self.findings(lesson(skill_map().replace("  overall: 0%", "\noverall: 0%"))), [])

    def test_empty_map_and_malformed_rows_cannot_silently_disappear(self):
        self.assertTrue(self.findings(lesson("Topic: Fractions  overall: 0%"), "map-format"))
        for row_index in [1, 3, 4]:
            for malformed in ["Skill [░░░░░░░░░░]", "Skill [░░░░░░░░░░] 2.5/5",
                              "Skill [░░░░░░░░░░ 0/5", "Skill ░░░░░░░░░░] 0/5",
                              "Skill ░░░░░░░░░░ 0/5"]:
                lines = skill_map().splitlines()
                lines[row_index] = malformed
                findings = self.findings(lesson("\n".join(lines)), "map-format")
                self.assertTrue(any(f["severity"] == "violation" for f in findings), malformed)

    def test_arbitrarily_long_map_levels_are_reported_without_crashing(self):
        content = skill_map().replace("0/5", "9" * 5000 + "/5", 1)
        self.assertTrue(self.findings(lesson(content), "map-format"))

    def test_level_checks_require_nonempty_noncommand_answers(self):
        for reply in ["", "   ", "harder", "easier", "show progress", "just tell me", "save state", "resume from: old notes"]:
            items = lesson(skill_map()) + turns(("assistant", "How many fourths?"), ("user", reply),
                                                ("assistant", "Why are they equal?"), ("user", "They cover the same area."),
                                                ("assistant", skill_map(level=1)))
            self.assertTrue(self.findings(items, "level-needs-two-checks"), reply)
        items = lesson(skill_map()) + turns(("assistant", "How many fourths?"), ("user", "Two."),
                                            ("assistant", "Why are they equal?"), ("user", "They cover the same area."),
                                            ("assistant", skill_map(level=1)))
        self.assertEqual(self.findings(items), [])

    def test_levels_do_not_compare_different_topics_or_count_diagnostic_answers(self):
        self.assertEqual(self.findings(lesson(skill_map()) + turns(("assistant", skill_map(level=5, topic="Decimals")))), [])
        self.assertTrue(self.findings(lesson(skill_map()) + turns(("assistant", skill_map(level=1))), "level-needs-two-checks"))

    def test_exact_commands_and_unfinished_obligations(self):
        for command in ["show progress", "just tell me", "save state"]:
            self.assertEqual(self.findings(lesson() + turns(("user", command))), [])
        self.assertTrue(self.findings(lesson() + turns(("user", " SHOW PROGRESS! "),
                                                      ("assistant", "Let's continue.")), "progress-command"))
        self.assertEqual(self.findings(lesson() + turns(("user", "Do not show progress yet."),
                                                       ("assistant", "Let's continue."))), [])

    def test_override_exempts_exactly_one_reply_and_repeated_overrides(self):
        items = lesson() + turns(("user", "just tell me"),
                                 ("assistant", "What are sixths? Why use them? Here is the explanation."))
        self.assertEqual(self.findings(items), [])
        repeated = items + turns(("user", "just tell me"), ("assistant", "Why? How? Here is more detail."))
        self.assertEqual(self.findings(repeated), [])
        resumed = repeated + turns(("user", "Keep lecturing."), ("assistant", "One half is three sixths."))
        self.assertEqual([f["rule"] for f in self.findings(resumed)], ["just-tell-me-scope"])
        self.assertEqual(self.findings(repeated + turns(("assistant", "Which pieces are equal?"))), [])

    def test_flattery_word_boundaries(self):
        for phrase in ["GREAT JOB", "Great try", "Well done", "Excellent"]:
            self.assertEqual([f["severity"] for f in self.findings(lesson() + turns(("assistant", phrase)))], ["warning"])
        self.assertEqual(self.findings(lesson() + turns(("assistant", "An excellently cut circle."))), [])

    def test_guard_confirmation_is_not_diagnostic_and_consent_is_conservative(self):
        for reply, expected in [("teach me", []), ("Yes, teach me properly.", []),
                                ("no", ["violation"]), ("yes, quick answer please", ["violation"]),
                                ("yes", ["warning"]), ("maybe later", ["warning"])]:
            items = turns(("user", "What is a fraction?"),
                          ("assistant", "Want a quick answer, or should I actually teach you this properly?"),
                          ("user", reply)) + lesson(skill_map())[1:]
            findings = self.findings(items)
            self.assertEqual([f["severity"] for f in findings], expected, reply)
            self.assertTrue(all(f["rule"] == "guard" for f in findings))

    def test_guard_other_wording_is_warning_and_teaching_trigger_takes_precedence(self):
        items = turns(("user", "Explain fractions."), ("assistant", "Would you prefer a lesson?"),
                      ("user", "Yes please.")) + lesson(skill_map(), 2)[1:]
        self.assertEqual([f["severity"] for f in self.findings(items, "guard")], ["warning"])
        items[0]["content"] = "Explain fractions; teach me to master them."
        self.assertEqual(self.findings(items, "guard"), [])

    def test_guard_explicit_quick_answer_request_survives_alternate_confirmation(self):
        for confirmation in ["Would you like an ongoing lesson?", checker.CONFIRMATION]:
            for reply, expected in [("No, I only need a quick answer.", "violation"),
                                    ("yes, quick answer please", "violation"),
                                    ("I do not want a quick answer", "warning"),
                                    ("No problem.", "warning"),
                                    ("No, teach me properly.", "warning"),
                                    ("no", "violation" if confirmation == checker.CONFIRMATION else "warning")]:
                with self.subTest(confirmation=confirmation, reply=reply):
                    items = checker.load_transcript((FIXTURES / "guard.json").read_text())
                    items[1]["content"] = confirmation
                    items[2]["content"] = reply
                    findings = self.findings(items)
                    self.assertEqual([f["rule"] for f in findings], ["guard"])
                    self.assertEqual([f["severity"] for f in findings], [expected])

    def test_save_state_accepts_compact_or_full_map_and_requires_latest_levels(self):
        state = "Fractions\nEquivalent fractions: 0/5\nComparing: 0/5\nAdding: 0/5\nReducing: 0/5"
        for response in [state, skill_map()]:
            self.assertEqual(self.findings(lesson(skill_map()) + turns(("user", "save state"), ("assistant", response))), [])
        for response in [state.replace("Comparing: 0/5", "Comparing: 1/5"), state.replace("Fractions", "Decimals")]:
            self.assertTrue(self.findings(lesson(skill_map()) + turns(("user", "save state"),
                                                                    ("assistant", response)), "save-state"))
        findings = self.findings(lesson() + turns(("user", "save state"), ("assistant", "No map yet.")), "save-state")
        self.assertEqual([f["severity"] for f in findings], ["warning"])

    def test_save_state_accepts_bare_levels_and_annotated_topic(self):
        # Layout a real tutor produced: a note after the topic, levels without "/5".
        response = ("Topic: Fractions (learner knows halves)\nSkill map (level/5):\n"
                    "Equivalent fractions    0\nComparing    0  <- current focus\n"
                    "Adding    0\nReducing    0\nOverall: 0%")
        session = lesson(skill_map()) + turns(("user", "save state"), ("assistant", response))
        self.assertEqual(self.findings(session, "save-state"), [])
        wrong = response.replace("Adding    0", "Adding    3")
        session = lesson(skill_map()) + turns(("user", "save state"), ("assistant", wrong))
        self.assertTrue(self.findings(session, "save-state"))

    def test_rerendered_map_does_not_restart_the_check_count(self):
        session = lesson(skill_map()) + turns(
            ("assistant", "Which is larger, a half or a third?"), ("user", "A half."),
            ("user", "show progress"), ("assistant", skill_map()),
            ("assistant", "Why is a half larger?"), ("user", "Fewer, bigger pieces."),
            ("assistant", skill_map(1)))
        self.assertEqual(self.findings(session, "level-needs-two-checks"), [])
        session = lesson(skill_map()) + turns(
            ("user", "show progress"), ("assistant", skill_map()),
            ("assistant", "Why is a half larger?"), ("user", "Fewer, bigger pieces."),
            ("assistant", skill_map(1)))
        self.assertTrue(self.findings(session, "level-needs-two-checks"))

    def test_short_instruction_after_question_is_only_a_warning(self):
        session = lesson() + turns(("assistant", "Does Priya appear? Explain why."))
        self.assertEqual([f["severity"] for f in self.findings(session, "ends-on-question")], ["warning"])
        session = lesson() + turns(("assistant", "Does Priya appear? Remember that a left join keeps every left row."))
        self.assertEqual([f["severity"] for f in self.findings(session, "ends-on-question")], ["violation"])


if __name__ == "__main__":
    unittest.main()
