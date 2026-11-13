"""Milestone checks for __ID__.

One function per milestone in ../lesson.json: milestone `some-id` is checked by
`check_some_id(project)`, which gets the project folder and returns
(passed, detail). Keep the detail short and actionable: it is what the learner
sees next to FAIL. Python 3.9+, standard library only; call real tools with
subprocess when a milestone needs them.
"""
from __future__ import annotations

import os
from typing import Tuple

Result = Tuple[bool, str]


def check_first_milestone(project: str) -> Result:
    if os.path.exists(os.path.join(project, "TODO")):
        return False, "TODO: describe what is missing and how to fix it"
    return False, "TODO: implement this check"


def check_second_milestone(project: str) -> Result:
    return False, "TODO: implement this check"


def check_third_milestone(project: str) -> Result:
    return False, "TODO: implement this check"
