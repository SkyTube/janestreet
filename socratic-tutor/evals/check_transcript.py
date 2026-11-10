#!/usr/bin/env python3
"""Check the mechanically observable parts of a tutoring transcript."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys


LIMITATIONS = (
    "Cannot check withholding the answer after a wrong reply; strict grading; "
    "misattributed insight; sensible spaced review timing; or a final assessment "
    "covering every subskill. Textual detection has false positives and negatives: "
    "question marks do not establish independent, correct checks or recall. "
    "Target level is context only. A clean report does not establish tutor quality."
)

SOURCES = {
    "one-question": "Ask ONE question at a time.",
    "ends-on-question": "End the message immediately after the question and wait for the answer; do not add hints, example answers, or teaching content after it.",
    "diagnostic-length": "Run a short diagnostic: 3-5 probing questions spanning easy to hard, to place the user's starting level per subskill.",
    "map-format": "Render the map as ASCII after every level change, and whenever the user asks for progress:",
    "level-needs-two-checks": "Raise a level only on demonstrated performance across 2+ independent checks, at least one of them unprompted recall (not immediately after the user was told the answer).",
    "progress-command": '"show progress" — render the skill map',
    "just-tell-me-scope": '"just tell me" — override Socratic mode for the current question only',
    "no-flattery": "Do not flatter.",
    "guard": 'If the user asked a one-off question ("explain X", "what is X", "how do I use X"), answer it normally and do not start the tutoring loop.',
    "save-state": '"save state" — emit a compact block (topic, skill map with levels, notes on misconceptions and what has been covered) that can be pasted into a new session to resume',
}
SOURCES["level-step"] = SOURCES["level-needs-two-checks"]
COUNT_SOURCE = "Break the topic into 4-10 concrete subskills (fewer for narrow topics)."
BAR_SOURCE = "Use solid/light block characters (U+2588 / U+2591) for the bars when rendering."
LEVEL_SOURCE = "Track each subskill at levels 0-5:"
CONFIRMATION = "Want a quick answer, or should I actually teach you this properly?"
TRIGGER = re.compile(r"\b(?:teach me|tutor me|quiz me|drill me|help me master)\b", re.I)
HEADER = re.compile(r"^\s*Topic:\s*(.+?)(?:\s+overall:\s*(.*))?\s*$")
ROW = re.compile(r"^\s*([^\[\]]+?)\s+\[([^\]]*)\]\s+(\S+)/5(?:\s+.*)?\s*$")
SHORT_TAIL_WORDS = 4
COMMANDS = {"show progress", "just tell me", "save state", "harder", "easier"}
QUICK_ANSWER = re.compile(r"(?:yes,? |no, )?(?:(?:i (?:only )?(?:need|want)|just|only) )?(?:a )?quick answer(?:,? please)?")


@dataclass
class SkillMap:
    index: int
    start: int
    end: int
    topic: str
    rows: list
    overall: str
    malformed_rows: list


def normalized(text):
    return " ".join(text.split()).casefold()


def command(text):
    return normalized(text).rstrip(".!?").rstrip()


def is_confirmation(text):
    return normalized(text).strip('\"\'“”‘’') == normalized(CONFIRMATION)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("nonstandard JSON constant: " + value)


def load_transcript(text):
    def decode(part):
        try:
            return json.loads(part, object_pairs_hook=unique_object, parse_constant=reject_constant)
        except RecursionError:
            raise ValueError("JSON nesting is too deep") from None

    if not text.strip():
        raise ValueError("expected a JSON array or JSON Lines records, got empty input")
    if text.lstrip().startswith("["):
        messages = decode(text)
    else:
        messages = [decode(line) for line in text.splitlines() if line.strip()]
    for index, record in enumerate(messages):
        if (not isinstance(record, dict) or set(record) != {"role", "content"}
                or record["role"] not in ("user", "assistant")
                or not isinstance(record["content"], str)):
            raise ValueError("message {} must be exactly {{role: user|assistant, content: string}}".format(index))
        try:
            record["content"].encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("message {} content contains a lone Unicode surrogate".format(index)) from None
    return messages


def parse_maps(text, index):
    """Recognize a Topic header followed by contiguous bracketed skill rows."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    maps, i = [], 0
    while i < len(lines):
        header = HEADER.fullmatch(lines[i].rstrip("\r\n"))
        if not header:
            i += 1
            continue
        topic, overall = header.groups()
        rows, malformed, j = [], [], i + 1
        has_overall = overall is not None
        while j < len(lines):
            line = lines[j].strip()
            row = ROW.fullmatch(line)
            if row:
                name, bar, level = row.groups()
                level = level.lstrip("0") or "0"
                level = int(level) if len(level) == 1 and level in "012345" else None
                rows.append((name.strip(), bar, level))
            elif line.startswith("overall:"):
                has_overall = True
                overall = line[len("overall:"):].strip()
            elif (not line.endswith("?") and ("[" in line or "]" in line
                  or re.search(r"\s\S+/5(?:\s+.*)?$", line))):
                malformed.append(line)
            elif line and not re.fullmatch(r"[-─]+", line):
                break
            j += 1
        if rows or has_overall or any(re.search(r"[\[\]█░]", line) for line in malformed):
            maps.append(SkillMap(index, offsets[i], offsets[j], topic.strip(), rows, overall or "", malformed))
        i = j
    return maps


def question_offsets(text, maps):
    """Keep original offsets: ignored question marks must not erase a suffix."""
    positions, fence, offset = [], None, 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = None
        elif marker:
            fence = marker.group(1)
        else:
            positions.extend(offset + match.start() for match in re.finditer(r"\?", line))
        offset += len(line)
    return [p for p in positions if not any(m.start <= p < m.end for m in maps)]


def check_questions(messages, by_message, questions, exempt, add):
    teaching = False
    for i, item in enumerate(messages):
        if item["role"] == "user":
            teaching = teaching or bool(TRIGGER.search(item["content"]))
            continue
        teaching = teaching or bool(by_message[i])
        if not teaching or i in exempt:
            continue
        if len(questions[i]) > 1:
            add("one-question", i, "Found {} question marks outside maps and fenced code.".format(len(questions[i])))
        tail = item["content"][questions[i][-1] + 1:] if questions[i] else ""
        if tail and not re.fullmatch(r'''[\s\]})"'’”»]*''', tail):
            # "Explain why." after a question is part of the prompt, not a hint.
            short = len(tail.split()) <= SHORT_TAIL_WORDS and "\n" not in tail.strip()
            add("ends-on-question", i,
                "A short instruction follows the question; check it is not a hint." if short
                else "Substantive content follows the last question.",
                "warning" if short else "violation")


def check_diagnostic(messages, maps, questions, add):
    if not maps:
        return
    first = maps[0]
    count = sum(sum(p < first.start if i == first.index else True for p in questions[i])
                for i in range(first.index + 1) if not is_confirmation(messages[i]["content"]))
    if not 3 <= count <= 5:
        add("diagnostic-length", first.index, "Found {} diagnostic question marks before the first map; expected 3–5.".format(count))


def check_map_format(maps, add):
    for m in maps:
        count = len(m.rows) + len(m.malformed_rows)
        if not 4 <= count <= 10:
            severity = "warning" if 0 < count < 4 else "violation"
            add("map-format", m.index, "Map has {} subskill rows; expected 4–10, with fewer allowed for narrow topics.".format(count), severity, COUNT_SOURCE)
        for row in m.malformed_rows:
            add("map-format", m.index, "Malformed subskill row; expected name [bar] N/5: " + row[:80])
        if not m.overall.strip():
            add("map-format", m.index, "Map is missing a nonempty overall: line or header field.")
        for name, bar, level in m.rows:
            if level is None:
                add("map-format", m.index, "{} needs an unsigned integer level from 0–5.".format(name), source=LEVEL_SOURCE)
            elif len(bar) != 10 or set(bar) - {"█", "░"} or bar.count("█") != 2 * level:
                add("map-format", m.index, "{} needs ten block cells with twice its level filled.".format(name), source=BAR_SOURCE)


def answered_questions(messages, questions, previous, current):
    count = 0
    for i in range(previous.index, current.index):
        eligible = [p for p in questions[i] if i != previous.index or p >= previous.end]
        if not eligible:
            continue
        for reply in messages[i + 1:current.index]:
            if reply["role"] == "assistant":
                break
            text = command(reply["content"])
            if text and text not in COMMANDS and not text.startswith("resume from:"):
                count += len(eligible)
                break
    return count


def check_levels(messages, maps, questions, add):
    since_change = {}  # answered questions since each subskill's level last changed
    for previous, current in zip(maps, maps[1:]):
        if normalized(previous.topic) != normalized(current.topic):
            since_change = {}
            continue
        old = {normalized(name): level for name, _, level in previous.rows}
        asked = answered_questions(messages, questions, previous, current)
        for name, _, level in current.rows:
            key = normalized(name)
            prior = old.get(key)
            # A map re-rendered for "show progress" must not restart the count.
            checks = since_change.get(key, 0) + asked
            since_change[key] = checks if prior == level else 0
            if prior is None or level is None or not 0 <= prior < level <= 5:
                continue
            if checks < 2:
                add("level-needs-two-checks", current.index, "{} rose with only {} answered questions between maps; two is only a necessary count, not proof of independent performance.".format(name, checks))
            if level - prior > 1:
                add("level-step", current.index, "{} rose by {} levels; review this jump (the source does not prohibit it).".format(name, level - prior), "warning")


def check_progress(messages, next_assistant, by_message, add):
    for i, item in enumerate(messages):
        response = next_assistant[i]
        if item["role"] == "user" and command(item["content"]) == "show progress" and response is not None:
            if not by_message[response]:
                add("progress-command", response, "The response to show progress contains no recognized map.")


def check_override(exempt, next_assistant, questions, add):
    for i in sorted(exempt):
        following = next_assistant[i]
        if following is not None and following not in exempt and not questions[following]:
            add("just-tell-me-scope", following, "The assistant response after the override must return to a question.")


def check_flattery(messages, add):
    for i, item in enumerate(messages):
        if item["role"] == "assistant" and re.search(r"\b(?:great\s+job|great\s+try|well\s+done|excellent)\b", item["content"], re.I):
            add("no-flattery", i, "Possible flattery from the documented phrase list; quoted or descriptive uses may be false positives.", "warning")


def check_guard(messages, maps, questions, add):
    first_user = next((i for i, item in enumerate(messages) if item["role"] == "user"), None)
    if first_user is None or not maps:
        return
    request = messages[first_user]["content"].strip()
    if TRIGGER.search(request) or not re.match(r"(?:explain\b|what is\b|how do i\b)", request, re.I):
        return
    first_map = maps[0].index
    confirmations = [i for i in range(first_user + 1, first_map)
                     if messages[i]["role"] == "assistant" and is_confirmation(messages[i]["content"])]
    if not confirmations:
        if any(QUICK_ANSWER.fullmatch(command(m["content"]))
               for m in messages[first_user + 1:first_map] if m["role"] == "user"):
            add("guard", first_map, "An explicit request for a quick answer was followed by a tutoring map.")
            return
        severity = "warning" if any(questions[first_user:first_map]) else "violation"
        add("guard", first_map, "A one-off request reached a map without recognized confirmation; alternate phrasing needs human review.", severity)
        return
    replies = [m["content"] for m in messages[confirmations[-1] + 1:first_map] if m["role"] == "user"]
    reply = command(replies[0]) if replies else ""
    if re.fullmatch(r"(?:yes[, ]+)?(?:please )?(?:teach me(?: properly)?|tutor me|quiz me|drill me|help me master)", reply):
        return
    refused = reply == "no" or QUICK_ANSWER.fullmatch(reply)
    add("guard", first_map, "No explicit tutoring agreement follows the confirmation; bare yes is ambiguous to an either/or question.",
        "violation" if refused or not replies else "warning")


def check_save_state(messages, maps, next_assistant, add):
    for i, item in enumerate(messages):
        response = next_assistant[i]
        if item["role"] != "user" or command(item["content"]) != "save state" or response is None:
            continue
        previous = [m for m in maps if m.index < i]
        if not previous:
            add("save-state", response, "No prior recognized map is available to verify saved topic and levels.", "warning")
            continue
        latest, text = previous[-1], messages[response]["content"]
        if latest.malformed_rows or any(level is None for _, _, level in latest.rows):
            add("save-state", response, "The latest map has malformed rows or levels; saved state needs human review.", "warning")
            continue
        missing = []
        topic_pattern = r"(?:topic:\s*)?" + re.escape(normalized(latest.topic)) + r"(?:\s+.*)?"
        if not any(re.fullmatch(topic_pattern, normalized(line)) for line in text.splitlines()):
            missing.append("topic " + latest.topic)
        for name, _, level in latest.rows:
            pattern = r"^\s*(?:[-*]\s+)?" + re.escape(normalized(name)) + r"\s*(?::|[-–—])?\s*(?:\[[^\]]*\]\s*)?" + str(level) + r"(?:/5)?(?:\s+.*)?$"
            if not any(re.fullmatch(pattern, normalized(line)) for line in text.splitlines()):
                missing.append("{} {}/5".format(name, level))
        if missing:
            add("save-state", response, "Saved state is missing or changes: " + ", ".join(missing) + ".")


def check_transcript(messages, target_level=4):
    findings = []

    def add(rule, index, message, severity="violation", source=None):
        findings.append({"rule": rule, "severity": severity, "message_index": index,
                         "excerpt": " ".join(messages[index]["content"].split())[:120],
                         "source": source or SOURCES[rule], "message": message})

    by_message = [parse_maps(m["content"], i) if m["role"] == "assistant" else [] for i, m in enumerate(messages)]
    maps = [m for group in by_message for m in group]
    questions = [question_offsets(m["content"], by_message[i]) if m["role"] == "assistant" else [] for i, m in enumerate(messages)]
    next_assistant, following = [None] * len(messages), None
    for i in range(len(messages) - 1, -1, -1):
        next_assistant[i] = following
        if messages[i]["role"] == "assistant":
            following = i
    exempt = {next_assistant[i] for i, m in enumerate(messages)
              if m["role"] == "user" and command(m["content"]) == "just tell me" and next_assistant[i] is not None}
    check_questions(messages, by_message, questions, exempt, add)
    check_diagnostic(messages, maps, questions, add)
    check_map_format(maps, add)
    check_levels(messages, maps, questions, add)
    check_progress(messages, next_assistant, by_message, add)
    check_override(exempt, next_assistant, questions, add)
    check_flattery(messages, add)
    check_guard(messages, maps, questions, add)
    check_save_state(messages, maps, next_assistant, add)
    findings.sort(key=lambda f: (f["message_index"], f["rule"], f["message"]))
    return {"target_level": target_level, "message_count": len(messages),
            "findings": findings, "limitations": LIMITATIONS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--target-level", type=int, choices=range(6), default=4)
    args = parser.parse_args(argv)
    try:
        messages = load_transcript(args.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        if args.json_output:
            print(json.dumps({"error": str(error)}, sort_keys=True))
        else:
            print("Invalid input: " + str(error), file=sys.stderr)
        return 2
    report = check_transcript(messages, args.target_level)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("Target level: {}/5; messages: {}".format(args.target_level, len(messages)))
        if not report["findings"]:
            print("No findings.")
        for finding in report["findings"]:
            print("{severity} {rule} at message {message_index}: {message}\n  Excerpt: {excerpt}\n  Source: {source}".format(**finding))
        print("\nLimitations: " + LIMITATIONS)
    return 1 if any(f["severity"] == "violation" for f in report["findings"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
