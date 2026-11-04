"""Label-free, deterministic and globally exclusive HotpotQA episode selection.

Similarity is a reproducible lexical proxy, not an embedding/LLM judgement.
Only source ID, question, public reasoning type and difficulty are inspected.
"""

from collections import Counter, defaultdict
from hashlib import sha256
from math import log, sqrt
import re
import unicodedata

from .manifest import HoldoutSelection


POLICY_VERSION = "hotpotqa.exclusive_question_tfidf.v1"
STOPWORDS = frozenset("a an the and or of in on at to for from by with as is are was were be been being "
    "do does did has have had can could will would should may might must this that these those "
    "it its their his her they them he she you your we our i both which what who whom whose "
    "where when how why".split())


def normalized_question(question):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", question).casefold()))


def question_form(question):
    """Ordered rules over the question only; these do not infer gold answer types."""
    text = normalized_question(question)
    if re.match(r"^(is|are|was|were|do|does|did|has|have|had|can|could|will|would|should)\b", text):
        return "yes_no"
    if re.search(r"\b(where|what (city|country|state|location)|which (city|country|state))\b", text):
        return "location"
    if re.search(r"\b(when|what (year|date)|which year)\b", text):
        return "time"
    if re.search(r"\bhow (many|much|long|old|tall|far)\b", text):
        return "quantity"
    if re.search(r"\b(who|whom|whose)\b", text):
        return "person"
    if re.search(r"\bwhy\b", text):
        return "reason"
    return "entity_or_other"


def public_features(row):
    question = row["question"]
    if not isinstance(question, str) or not question.strip():
        raise ValueError("selection requires a nonempty question")
    if row["type"] not in {"bridge", "comparison"} or row["level"] not in {"easy", "medium", "hard"}:
        raise ValueError("selection requires a known public type and difficulty")
    return {"type": row["type"], "question_form": question_form(question), "level": row["level"]}


def select_exclusive_episodes(rows, *, seed, task_count, similar_count=1, regression_count=1,
                              test_rows=(), min_similarity=0.1):
    if min(task_count, similar_count, regression_count) < 1 or not 0 <= min_similarity <= 1:
        raise ValueError("positive task counts and similarity in [0,1] required")
    order = lambda key: sha256(f"{seed}:{key}".encode()).hexdigest()
    # Keep no references to answers, supporting facts, or source context.
    questions, features = {}, {}
    for row in rows:
        key = row["_id"]
        if not isinstance(key, str) or not key or key in questions:
            raise ValueError("source task IDs must be nonempty and unique")
        questions[key] = normalized_question(row["question"])
        features[key] = public_features(row)
    tests = {row["_id"]: normalized_question(row["question"]) for row in test_rows}
    if len(tests) != len(test_rows) or set(tests) & set(questions):
        raise ValueError("reserved tests must have unique, disjoint IDs")
    if len(set(tests.values())) != len(tests):
        raise ValueError("reserved test questions must be distinct")
    # Canonicalize duplicate question text before selecting any role.
    seen_text = set(tests.values())
    available = []
    for key in sorted(questions, key=order):
        if questions[key] not in seen_text:
            seen_text.add(questions[key])
            available.append(key)
    tokens = {key: Counter(word for word in questions[key].split() if word not in STOPWORDS)
              for key in available}
    frequencies = Counter(word for bag in tokens.values() for word in bag)
    idf = {word: log((1 + len(tokens)) / (1 + count)) + 1 for word, count in frequencies.items()}
    vectors = {}
    for key, bag in tokens.items():
        weights = {word: (1 + log(count)) * idf[word] for word, count in bag.items()}
        norm = sqrt(sum(value * value for value in weights.values()))
        vectors[key] = {word: value / norm for word, value in weights.items()} if norm else {}

    by_type = {kind: [key for key in available if features[key]["type"] == kind]
               for kind in ("bridge", "comparison")}
    counts = {"bridge": (task_count + 1) // 2, "comparison": task_count // 2}
    if any(len(by_type[kind]) < count for kind, count in counts.items()):
        raise ValueError("not enough current tasks for the balanced reasoning-type sample")
    current, used = [], set()
    roles = {key: "test" for key in tests}
    buckets = defaultdict(list)
    for key in available:
        f = features[key]
        buckets[(f["type"], f["question_form"], f["level"])].append(key)
    selections, evidence, ineligible = {}, {}, []
    cursors = {kind: iter(by_type[kind]) for kind in counts}
    for index in range(max(counts.values())):
        for kind in counts:
            if index >= counts[kind]:
                continue
            for key in cursors[kind]:
                if key in used:
                    continue
                f, vector = features[key], vectors[key]
                pool = buckets[(f["type"], f["question_form"], f["level"])]
                ranked = []
                for other in pool:
                    if other in used or other == key:
                        continue
                    score = sum(weight * vectors[other].get(word, 0) for word, weight in vector.items())
                    if score >= min_similarity:
                        ranked.append((other, score))
                ranked.sort(key=lambda pair: (-pair[1], order(pair[0])))
                if len(ranked) < similar_count:
                    ineligible.append({"task_id": key, "reason": "no_available_similar_match"})
                    continue
                chosen = ranked[:similar_count]
                current.append(key)
                used.add(key)
                used.update(other for other, _ in chosen)
                roles[key] = "dev"
                roles.update({other: "similar" for other, _ in chosen})
                selections[key] = {"similar": [other for other, _ in chosen]}
                evidence[key] = {"current": f, "similar": [
                    {"task_id": other, "features": features[other], "question_cosine": round(score, 8)}
                    for other, score in chosen]}
                break
            else:
                raise ValueError(f"not enough {kind} tasks with distinct similar matches; rules were not relaxed")
    # Hash-sample outside the current capability stratum, with matched difficulty.
    # Reserve similar assignments first, so regression cannot consume a close match.
    for key in current:
        f = features[key]
        pool = [other for other in available if other not in used
                and features[other]["type"] != f["type"]
                and features[other]["question_form"] != f["question_form"]
                and features[other]["level"] == f["level"]]
        if len(pool) < regression_count:
            raise ValueError(f"{key}: insufficient regression tasks for the declared rules")
        chosen = pool[:regression_count]
        used.update(chosen)
        roles.update({other: "regression" for other in chosen})
        selections[key]["regression"] = chosen
        evidence[key]["regression"] = [{"task_id": other, "features": features[other]} for other in chosen]
    policy = {"version": POLICY_VERSION, "seed": seed, "dev_count": task_count,
        "similar_per_task": similar_count, "regression_per_task": regression_count,
        "current_sampling": "seeded hash order; balanced bridge/comparison; require an unused similar match; no performance filtering",
        "similar_rule": "same type, question_form and level; highest question TF-IDF cosine among unused tasks",
        "regression_rule": "different type AND question_form; same level; seeded hash order among unused tasks",
        "min_question_cosine": min_similarity, "question_form_version": "ordered_regex.v1",
        "tfidf": "NFKC casefold word tokens; fixed stopwords; (1+ln(tf))*(1+ln((N+1)/(df+1))); L2 cosine",
        "tie_break": "sha256(seed:task_id)", "deduplication": "global ID and normalized question text",
        "selection_inputs": ["_id", "question", "type", "level"],
        "unique_source_questions": len(available), "ineligible_current_tasks": ineligible, "assignments": evidence}
    return roles, {key: HoldoutSelection(**value) for key, value in selections.items()}, policy


def validate_selected_rows(rows, manifest):
    """Verify declared matching rules against source questions at benchmark startup."""
    if manifest.selection_policy.get("version") != POLICY_VERSION:
        return
    if manifest.holdout_reuse != "forbidden":
        raise ValueError("exclusive selection requires holdout_reuse=forbidden")
    normalized = [normalized_question(row["question"]) for row in rows.values()]
    if len(normalized) != len(set(normalized)):
        raise ValueError("exclusive selection contains duplicate normalized questions")
    policy = manifest.selection_policy
    if len(manifest.validation_sets) != policy["dev_count"]:
        raise ValueError("current task count differs from selection policy")
    features = {key: public_features(row) for key, row in rows.items()}
    for key, selection in manifest.validation_sets.items():
        current = features[key]
        for group in ("similar", "regression"):
            chosen = getattr(selection, group)
            if len(chosen) != policy[group + "_per_task"]:
                raise ValueError("holdout count differs from selection policy")
            for other in chosen:
                f = features[other]
                valid = (f == current if group == "similar" else
                    f["type"] != current["type"] and f["question_form"] != current["question_form"]
                    and f["level"] == current["level"])
                if not valid:
                    raise ValueError(f"{key}.{group}: task violates declared question/type/difficulty rules")
        evidence = policy["assignments"][key]
        if evidence["current"] != current:
            raise ValueError("selection evidence differs from source features")
        for group in ("similar", "regression"):
            if [item["task_id"] for item in evidence[group]] != getattr(selection, group):
                raise ValueError("selection evidence differs from assigned tasks")
            if any(item["features"] != features[item["task_id"]] for item in evidence[group]):
                raise ValueError("selection evidence differs from holdout source features")
