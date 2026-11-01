"""Bound public history without discarding failure identity or inventing a diagnosis."""

import json


def limit_construction_feedback(feedback, max_chars):
    if feedback is None:
        return None
    if len(feedback.model_dump_json()) <= max_chars:
        return feedback
    # Keep unresolved causes before recovered attempts. Preserve event/call indices for chronology.
    ordered = sorted(enumerate(feedback.issues), key=lambda pair: (
        pair[1].status == "resolved", pair[1].stage == "termination", -pair[0]))
    result = feedback.replace(issues=[], omitted_issues=feedback.omitted_issues + len(feedback.issues), truncated=True)
    for _, issue in ordered:
        full = result.replace(issues=[*result.issues, issue], omitted_issues=result.omitted_issues - 1)
        if len(full.model_dump_json()) <= max_chars:
            result = full
            continue
        # Preserve a valid JSON envelope and explicit truncation; never silently cut raw JSON.
        short = issue.replace(message=issue.message[:800], details={}, resolution_details={}, truncated=True)
        candidate = result.replace(issues=[*result.issues, short], omitted_issues=result.omitted_issues - 1)
        room = max_chars - len(candidate.model_dump_json()) - 100
        if room < 0:
            continue
        data = issue.model_dump(mode="json")
        detail = json.dumps({"failure": data["details"], "resolution": data["resolution_details"]}, ensure_ascii=False)
        excerpt = detail[:room]
        while excerpt:
            shortened = short.replace(details={"excerpt": excerpt})
            candidate = result.replace(issues=[*result.issues, shortened], omitted_issues=result.omitted_issues - 1)
            if len(candidate.model_dump_json()) <= max_chars:
                break
            excerpt = excerpt[:len(excerpt) // 2]
        else:
            candidate = result.replace(issues=[*result.issues, short], omitted_issues=result.omitted_issues - 1)
        result = candidate
    return result
