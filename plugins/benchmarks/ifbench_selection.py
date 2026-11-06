"""Reproducible constraint-based IFBench pairing, without model/performance filtering."""

from collections import Counter, defaultdict
from hashlib import sha256
import re
import unicodedata

from .manifest import HoldoutSelection


POLICY_VERSION = "ifbench.exclusive_constraints.v1"


def normalized_prompt(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def validate_rows(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("IFBench source must contain records")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("key"), (str, int)) or isinstance(row["key"], bool):
            raise ValueError("IFBench requires a string/integer key")
        key = str(row["key"])
        if not key.strip() or key in seen:
            raise ValueError("IFBench keys must be nonempty and globally unique")
        seen.add(key)
        ids, kwargs = row.get("instruction_id_list"), row.get("kwargs")
        if not isinstance(row.get("prompt"), str) or not row["prompt"].strip():
            raise ValueError("IFBench requires a nonempty prompt")
        if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or ":" not in i for i in ids)
                or not isinstance(kwargs, list) or len(ids) != len(kwargs)
                or any(not isinstance(item, dict) for item in kwargs)):
            raise ValueError("IFBench requires aligned constraint IDs and kwargs")


def features(row):
    ids = sorted(row["instruction_id_list"])
    return {"constraints": ids, "families": sorted({i.split(":", 1)[0] for i in ids}), "count": len(ids)}


def select_episodes(rows, *, task_count=80, seed=42):
    validate_rows(rows)
    if task_count < 1:
        raise ValueError("task_count must be positive")
    order = lambda key, attempt=0: sha256(f"{seed}:{attempt}:{key}".encode()).hexdigest()
    by_id = {str(row["key"]): row for row in rows}
    seen, available, duplicates = set(), [], []
    for key in sorted(by_id, key=order):
        prompt = normalized_prompt(by_id[key]["prompt"])
        if prompt in seen:
            duplicates.append(key)
        else:
            seen.add(prompt)
            available.append(key)
    if len(available) < 3 * task_count:
        raise ValueError("Need three globally distinct prompts per current task")
    f = {key: features(by_id[key]) for key in available}
    tokens = {key: set(re.findall(r"\w+", normalized_prompt(by_id[key]["prompt"]))) for key in available}
    def similarity(a, b):
        union = tokens[a] | tokens[b]
        return len(tokens[a] & tokens[b]) / len(union) if union else 0.0
    counts = Counter(f[key]["count"] for key in available)
    # Largest-remainder allocation preserves the source's single/multi-constraint mix.
    quotas = {n: task_count * count // len(available) for n, count in counts.items()}
    for n in sorted(counts, key=lambda n: (-(task_count * counts[n] % len(available)), n))[:task_count - sum(quotas.values())]:
        quotas[n] += 1
    groups = defaultdict(list)
    for key in available:
        groups[tuple(f[key]["constraints"])].append(key)
    for n, quota in quotas.items():
        capacity = sum(len(ids) // 2 for signature, ids in groups.items() if len(signature) == n)
        if capacity < quota or counts[n] < 3 * quota:
            raise ValueError(f"Insufficient exact constraint pairs/regressions for count={n}; rules not relaxed")
    # Search only deterministic hash orderings; never weaken matching criteria.
    # Bipartite matching reserves globally unique regression tasks after pairing.
    for attempt in range(256):
        pairs, used = [], set()
        for n, quota in sorted(quotas.items()):
            signatures = sorted((s for s in groups if len(s) == n), key=lambda s: order('|'.join(s), attempt))
            pool = {s: sorted(groups[s], key=lambda key: order(key, attempt)) for s in signatures}
            chosen = 0
            while chosen < quota:
                progress = False
                for signature in signatures:
                    ids = pool[signature]
                    if len(ids) < 2 or chosen == quota:
                        continue
                    current = ids.pop(0)
                    similar = min(ids, key=lambda other: (-similarity(current, other), order(other, attempt)))
                    ids.remove(similar)
                    pairs.append((current, similar))
                    used.update((current, similar))
                    chosen += 1
                    progress = True
                if not progress:
                    raise ValueError("Insufficient exact constraint pairs")
        remaining = [key for key in available if key not in used]
        eligible = {current: sorted((key for key in remaining if f[key]["count"] == f[current]["count"]
            and not set(f[key]["families"]) & set(f[current]["families"])), key=lambda key: order(key, attempt))
            for current, _ in pairs}
        owners = {}
        def assign(current, visited):
            for other in eligible[current]:
                if other in visited:
                    continue
                visited.add(other)
                if other not in owners or assign(owners[other], visited):
                    owners[other] = current
                    return True
            return False
        if all(assign(current, set()) for current, _ in sorted(pairs, key=lambda p: (len(eligible[p[0]]), order(p[0], attempt)))):
            break
    else:
        raise ValueError("No exclusive regression assignment in 256 deterministic orderings; rules not relaxed")
    regression = {current: other for other, current in owners.items()}
    roles, selections, evidence = {}, {}, {}
    for current, similar in sorted(pairs, key=lambda pair: order(pair[0])):
        other = regression[current]
        roles[current], roles[similar], roles[other] = "dev", "similar", "regression"
        selections[current] = HoldoutSelection(similar=[similar], regression=[other])
        evidence[current] = {"current": f[current], "similar": {"task_id": similar, **f[similar],
            "prompt_token_jaccard": round(similarity(current, similar), 8)},
            "regression": {"task_id": other, **f[other]}}
    policy = {"version": POLICY_VERSION, "seed": seed, "dev_count": task_count,
        "similar_per_task": 1, "regression_per_task": 1, "source_count": len(rows),
        "unique_prompt_count": len(available), "duplicate_prompt_ids_excluded": duplicates,
        "constraint_count_quotas": {str(n): q for n, q in sorted(quotas.items())}, "ordering_attempt": attempt,
        "current_rule": "largest-remainder constraint-count quotas; round-robin exact signatures in seeded hash order; requires a distinct similar match",
        "similar_rule": "identical sorted constraint IDs (including count); highest normalized prompt word-set Jaccard within remaining signature pool; no topic-similarity threshold",
        "regression_rule": "same constraint count; disjoint constraint families (prefix before colon); exclusive bipartite matching",
        "deduplication": "global source key and NFKC/casefold/whitespace-normalized prompt",
        "tie_break": "sha256(seed:ordering_attempt:key); first feasible ordering, at most 256",
        "selection_inputs": ["key", "prompt", "instruction_id_list"],
        "performance_filtering": False, "assignments": evidence,
        "unused_ids": [key for key in available if key not in roles],
        "note": "AER adaptation/validation subset of official test data; holdouts are acceptance data, not an independent final test. Regression tasks need not pass the initial prompt."}
    return roles, selections, policy


def validate_selection(rows, manifest):
    policy = manifest.selection_policy
    if policy.get("version") != POLICY_VERSION or manifest.holdout_reuse != "forbidden":
        raise ValueError("IFBench requires the versioned exclusive selection policy")
    roles, selections, expected = select_episodes(rows, task_count=policy["dev_count"], seed=policy["seed"])
    if (list(roles.items()) != list(manifest.roles.items())
            or list(selections.items()) != list(manifest.validation_sets.items()) or expected != policy):
        raise ValueError("IFBench manifest does not reproduce its declared selection rules/evidence")
