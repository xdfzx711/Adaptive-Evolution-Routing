"""Test-only exact solvers for auditing authored dataset labels, never model inputs."""

import itertools
import re
from collections import Counter
from fractions import Fraction


def invoice(data):
    subtotal = sum((item["quantity"] - item["returned"]) * item["unit_cents"]
                   for item in data["lines"] if not item["cancelled"])
    discount = subtotal * data["discount_bps"] // 10000 if subtotal >= data["discount_threshold_cents"] else 0
    net = subtotal - discount
    shipping = 0 if net >= data["free_shipping_threshold_cents"] else data["shipping_cents"]
    taxable = net + (shipping if data["shipping_taxable"] else 0)
    tax = (taxable * data["tax_bps"] + 5000) // 10000
    return dict(subtotal_cents=subtotal, discount_cents=discount, shipping_cents=shipping,
                tax_cents=tax, total_cents=net + shipping + tax)


def allocate(data):
    records = {item["id"]: item for item in data["recipients"]}
    amounts = dict.fromkeys(records, 0)
    active = {key for key, item in records.items() if item["enabled"] and item["cap"] > 0}
    remaining = min(data["budget"], sum(records[key]["cap"] for key in active))
    target = remaining
    while active:
        total_weight = sum(records[key]["weight"] for key in active)
        shares = {key: Fraction(remaining * records[key]["weight"], total_weight) for key in active}
        capped = {key for key in active if shares[key] > records[key]["cap"]}
        if capped:
            for key in capped:
                amounts[key] = records[key]["cap"]
                remaining -= amounts[key]
            active -= capped
            continue
        for key in active:
            amounts[key] = shares[key].numerator // shares[key].denominator
        extras = remaining - sum(amounts[key] for key in active)
        ordered = sorted(active, key=lambda key: (-(shares[key] - amounts[key]), key))
        for key in ordered[:extras]:
            amounts[key] += 1
        break
    assert sum(amounts.values()) == target
    return dict(allocations=";".join(f"{key}={amounts[key]}" for key in sorted(records)),
                unallocated=data["budget"] - target)


def revisions(data):
    latest = {}
    for index, event in enumerate(data["events"]):
        key = event["id"]
        if key not in latest or (event["revision"], index) > (latest[key][0]["revision"], latest[key][1]):
            latest[key] = (event, index)
    eligible = [item for item, _ in latest.values() if not item["deleted"] and
                item["region"] == data["region"] and item["score"] >= data["minimum_score"]]
    eligible.sort(key=lambda item: (-item["score"], item["updated"], item["id"]))
    owners = set()
    selected = []
    for item in eligible:
        if item["owner"] not in owners:
            selected.append(item)
            owners.add(item["owner"])
    return dict(ids=",".join(item["id"] for item in selected), count=len(selected),
                score_total=sum(item["score"] for item in selected))


def scheduling(data):
    duration = data["duration"]
    for start in range(data["earliest"], data["latest"] - duration + 1):
        if start % data["grid"]:
            continue
        end = start + duration
        if all(any(left <= start and end <= right for left, right in person["available"]) and
               not any(start < right and left < end for left, right in person["busy"])
               for person in data["people"]):
            return dict(start_minute=start, end_minute=end, possible=True)
    return dict(start_minute=-1, end_minute=-1, possible=False)


def canonical(value):
    return re.sub(r"[ _-]+", "-", value.strip(" _-").lower())


def tags(data):
    blocked = {canonical(value) for value in data["blocked"]}
    counts = Counter(canonical(value) for value in data["tags"])
    counts = {key: value for key, value in counts.items() if key and key not in blocked and value >= data["minimum_frequency"]}
    chosen = sorted(counts, key=lambda key: (-counts[key], len(key), key))[:data["top_k"]]
    return dict(tags="|".join(key.upper() for key in chosen),
                counts=",".join(str(counts[key]) for key in chosen), unique_count=len(counts))


def selection(data):
    solutions = []
    for chosen in itertools.combinations([item for item in data["items"] if item["active"]], data["choose"]):
        ids = sorted(item["id"] for item in chosen)
        cost = sum(item["cost"] for item in chosen)
        categories = {item["category"] for item in chosen}
        if cost > data["budget"] or not set(data["required_categories"]) <= categories:
            continue
        if any(set(pair) <= set(ids) for pair in data["conflicts"]):
            continue
        value = sum(item["value"] for item in chosen)
        solutions.append((-value, cost, ids))
    if not solutions:
        return dict(ids="", cost=0, value=0, possible=False)
    minus_value, cost, ids = min(solutions)
    return dict(ids=",".join(ids), cost=cost, value=-minus_value, possible=True)


SOLVERS = {"invoice": invoice, "allocation": allocate, "revisions": revisions,
           "scheduling": scheduling, "tags": tags, "selection": selection}
