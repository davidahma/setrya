"""Keep one private, paired human handoff study in a local JSON file."""

import argparse
import json
import os
from pathlib import Path


ARMS = ("setrya", "pytest")


def change():
    return dict(commit="", evidence="", setup_seconds=None, review_seconds=None,
                reviewer_answer="", correct=None, help="", incorrect_conclusions="")


def session():
    return {
        "repository": "", "own_application": None, "engineer": "", "independent_reviewer": "",
        "date": "", "first_arm": "", "agent_model_tools_and_budget_for_both": "",
        "arms": {name: {"first_change": change(), "assigned_second_change": change()} for name in ARMS},
        "later_setrya_use": {"commit": "", "evidence": "", "use": "", "without_facilitator": None,
                             "team_chose_to_reuse": None},
    }


def text(row, key):
    value = row.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value.strip()


def flag(row, key):
    value = row[key]
    if value is not None and type(value) is not bool:
        raise ValueError(f"{key} must be true, false, or null")
    return value


def seconds(row, key):
    value = row[key]
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{key} must be a nonnegative whole number or null")
    return value


def observation(row):
    setup, review = (seconds(row, k) for k in ("setup_seconds", "review_seconds"))
    correct = flag(row, "correct")
    observed = all(text(row, k) for k in ("commit", "evidence", "reviewer_answer"))
    if correct is False:
        return False, "incorrect decision"
    if not (correct and observed and setup is not None and review is not None):
        return False, "incomplete"
    return True, f"correct · setup {setup}s · independent review {review}s"


def summarize(data):
    engineer, reviewer = (text(data, key) for key in ("engineer", "independent_reviewer"))
    if engineer and reviewer and engineer.casefold() == reviewer.casefold():
        raise ValueError("the engineer and reviewer must be different participants")
    first_arm = text(data, "first_arm")
    if first_arm and first_arm not in ARMS:
        raise ValueError("first_arm must be setrya or pytest")
    people = bool(engineer and reviewer)
    controls = all(text(data, k) for k in ("repository", "date", "agent_model_tools_and_budget_for_both"))
    comparable = bool(people and controls and first_arm)
    own = flag(data, "own_application")
    if set(data["arms"]) != set(ARMS):
        raise ValueError("supply one setrya arm and one pytest arm")
    first_complete = {}
    all_observed = True
    lines = []
    for arm in ARMS:
        first, second = (data["arms"][arm][stage] for stage in ("first_change", "assigned_second_change"))
        for index, row in enumerate((first, second), start=1):
            passed, detail = observation(row)
            if index == 1:
                first_complete[arm] = passed
                if passed:
                    setup = "within 1h" if row["setup_seconds"] <= 3600 else "over 1h"
                    review = "within 2min" if row["review_seconds"] <= 120 else "over 2min"
                    detail += f" ({setup} setup, {review} review)"
            elif passed and text(first, "commit") == text(second, "commit"):
                raise ValueError("the assigned second change needs its own commit")
            all_observed = all_observed and passed
            lines.append(f"{arm}, change {index}: {detail}")
    matched_changes = all(
        text(data["arms"]["setrya"][stage], "commit") == text(data["arms"]["pytest"][stage], "commit")
        for stage in ("first_change", "assigned_second_change")
    )
    if comparable and all_observed:
        paired = "ready to compare correct decisions" if matched_changes else "different application revisions"
    else:
        paired = "incomplete; retain incorrect or unfinished decisions"
    lines.append("Paired comparison: " + paired)
    activated = bool(people and text(data, "repository") and own and first_complete["setrya"])
    later = data["later_setrya_use"]
    kind = text(later, "use")
    if kind not in ("", "same-drill", "second-route"):
        raise ValueError("later use must be same-drill or second-route")
    assigned = {text(row, "commit") for arm in data["arms"].values() for row in arm.values()}
    later_commit = text(later, "commit")
    if later_commit and later_commit in assigned:
        raise ValueError("later use needs a change outside both assigned exercises")
    independent = flag(later, "without_facilitator")
    team_choice = "team_chose_to_reuse" in later and flag(later, "team_chose_to_reuse")
    reused = bool(activated and independent and team_choice and kind and later_commit and text(later, "evidence"))
    lines.append("Own-application activation: " + ("observed" if activated else "unmeasured"))
    lines.append("Independent use on a later change: " + (kind if reused else "unmeasured"))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "report"))
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "init":
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            with os.fdopen(os.open(args.file, flags, 0o600), "w", encoding="utf-8") as out:
                json.dump(session(), out, indent=2)
                out.write("\n")
            print(f"Created a blank session in {args.file}")
        else:
            print(summarize(json.loads(args.file.read_text(encoding="utf-8"))))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        parser.exit(2, f"Study file error: {error}\n")


if __name__ == "__main__":
    main()
