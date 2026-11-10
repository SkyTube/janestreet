# Transcript checks for socratic-tutor

`check_transcript.py` reads a tutoring conversation and reports where the
tutor broke a rule from [SKILL.md](../SKILL.md) that can be decided from the
text alone. It needs only the Python standard library (3.9 or later). It does
not run a tutor or call any service.

## Usage

Save the conversation as a JSON array (or JSON Lines) of messages:

```json
[{"role": "user", "content": "Teach me SQL joins."},
 {"role": "assistant", "content": "I'll start with a few questions. ..."}]
```

Then, from the repository root:

```sh
python3 socratic-tutor/evals/check_transcript.py conversation.json
python3 socratic-tutor/evals/check_transcript.py conversation.json --json
python3 -m unittest discover -s socratic-tutor/evals/tests
```

The exit status is 0 when no rule is violated (warnings alone still exit 0),
1 when at least one is, and 2 for unreadable input, so it can gate a CI job.
Each finding names the rule, the message index, a short excerpt and the
sentence of SKILL.md it enforces.

## Rules

| Rule | Flags | Severity |
| --- | --- | --- |
| `one-question` | more than one question in a teaching message | violation |
| `ends-on-question` | text after the question | violation; warning when it is a few words such as "Explain why." |
| `diagnostic-length` | fewer than 3 or more than 5 questions before the first skill map | violation |
| `map-format` | a level outside 0-5, a bar that is not ten U+2588/U+2591 cells with two filled per level, a missing overall field, more than ten subskills (fewer than four is a warning) | violation |
| `level-needs-two-checks` | a level raised with fewer than two answered questions since the previous map | violation |
| `level-step` | a level raised by more than one between consecutive maps | warning |
| `progress-command` | no skill map in the reply to "show progress" | violation |
| `just-tell-me-scope` | no return to questioning in the message after a "just tell me" answer | violation |
| `no-flattery` | "great job", "great try", "well done", "excellent" | warning |
| `guard` | a skill map after a one-off "explain", "what is" or "how do I" request that was never confirmed as a tutoring request | violation, or warning when the wording is ambiguous |
| `save-state` | a reply to "save state" that omits the topic or a subskill level from the latest map | violation |

## What it cannot check

These need judgement, and a clean report says nothing about them:

- whether the tutor withheld the answer after a wrong reply
- whether grading was strict enough, and whether the checks behind a level
  were independent, correct or unprompted
- whether an insight was attributed to the learner that they did not express
- whether spaced review happened at sensible times
- whether the final assessment covered every subskill

The checks are textual, so they have blind spots. A question is a `?` outside
fenced code and outside the skill map, which miscounts rhetorical questions,
quoted questions and inline code, and misses questions phrased as instructions
("Predict the result."). The skill map is recognized only in the
layout SKILL.md shows (a `Topic:` line followed by `name [bar] N/5` rows).
Commands are matched only when they are the whole user message. A violation
is a reason to read that part of the transcript, not a verdict.

## Learner personas

[learners/](learners/) holds four prompts for a second agent that plays the
learner, each built to tempt the tutor into breaking particular rules:

- `vague.md` gives half-right answers and fishes for the rest
- `impatient.md` says "just tell me" and tries to stretch it into a lecture
- `one-off.md` opens with a plain "what is X" question
- `overconfident.md` answers correctly, caves when challenged, asks for praise

Paste the skill into one session and a persona into another, relay the
messages between them, then save the conversation and run the checker.

## Fixtures

[transcripts/](transcripts/) holds short hand-written sessions. They are
synthetic. `good.json` produces no findings; each other file is named after
the one rule it breaks.
