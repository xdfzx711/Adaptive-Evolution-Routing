"""Describe actual component changes without relying on an Evolver's prose."""

import re
from collections.abc import Iterable
from difflib import SequenceMatcher

from aer_core.schemas.history import ComponentEdit, ScoreChange, ValidationSummary
from aer_core.schemas.validation import ValidationReport


def component_edits(component: str, before: str, after: str) -> tuple[ComponentEdit, ...]:
    # Preserve whitespace and complete changed instructions, including one-line prompts.
    old, new = re.findall(r"\s+|\S+", before), re.findall(r"\s+|\S+", after)
    return tuple(ComponentEdit(component=component, operation=kind,
        before="".join(old[start:end]), after="".join(new[new_start:new_end]))
        for kind, start, end, new_start, new_end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
        if kind != "equal")


def limit_edits(edits: Iterable[ComponentEdit], max_chars: int) -> tuple[tuple[ComponentEdit, ...], bool]:
    """Bound all before/after text in a candidate, explicitly marking omissions."""
    result, remaining, truncated = [], max_chars, False
    for edit in edits:
        size = len(edit.before) + len(edit.after)
        if size > remaining:
            # Show both sides of a replacement when possible.
            before_size = min(len(edit.before), remaining // 2 if edit.after else remaining)
            after_size = min(len(edit.after), remaining - before_size)
            before_size = min(len(edit.before), remaining - after_size)
            result.append(edit.replace(before=edit.before[:before_size], after=edit.after[:after_size], truncated=True))
            return tuple(result), True
        result.append(edit)
        remaining -= size
        truncated = truncated or edit.truncated
    return tuple(result), truncated


def validation_summary(report: ValidationReport) -> ValidationSummary:
    def pair(before, after):
        return ScoreChange(before=before, after=after, delta=after - before)

    local, old, new = report.local_metrics, report.baseline_metrics, report.candidate_metrics
    return ValidationSummary(status=report.status,
        current_task=pair(local.baseline_score, local.candidate_score) if local else None,
        independent=pair(old.score, new.score) if old and new else None,
        similar=pair(old.similar_score, new.similar_score) if old and new else None,
        regression=pair(old.regression_score, new.regression_score) if old and new else None)
