"""Shared delivery intent for this plugin's native planner and code prompts."""


DELIVERY_GUIDANCE = """
Final tools: fill a capability gap beyond the agent's existing shell read/search/edit operations.
Expose the useful top-level callable with caller-supplied JSON data or repository-relative paths.
Example: a dependency analyzer accepts definitions and roots, and returns reachable dependencies.
Keep fixed fixtures and test harnesses outside that callable; do not deliver a wrapper returning only
demo results, a function object or source text. Workspace scanners may legitimately have no public
arguments. Return JSON findings (lists, not sets) usable for the caller's next decision.
Compare representative actual results with expected results; serialization/type checks alone do not
validate behavior. Mark separate validation nodes deliverable=false. Internal helpers remain allowed.
Document inputs, outputs, limitations and side effects. Keep final descriptions, parameter names and
completion criteria consistent with established analysis; diagnostic hypotheses are not requirements.
"""
