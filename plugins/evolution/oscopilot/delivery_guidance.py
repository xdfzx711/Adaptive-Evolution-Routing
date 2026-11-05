"""Shared delivery intent for this plugin's native planner and code prompts."""


DELIVERY_GUIDANCE = """
Final tools: address the observed capability gap using the public task, execution and outer agent prompt.
Respect the task's information sources and permitted operations; do not assume files, network access,
shell access or an application environment are available. Expose a useful top-level callable taking
caller-supplied JSON data. When the task supplies files and permits access, workspace-relative paths
are also appropriate. Preserve caller-provided identifiers and indices in results that reference inputs.
The callable must operate on the actual invocation arguments or the current task workspace, not a
captured development example. New tasks provide their own data; no dataset or scoring files are supplied.
Keep fixed fixtures and test harnesses outside that callable; do not deliver a wrapper returning only
demo results, a function object or source text. Workspace scanners may legitimately have no public
arguments when the task supplies a workspace to inspect. Return JSON results (lists, not sets) usable
for the caller's next decision. Use small representative examples for construction and delivery;
example size does not limit the amount of task data accepted by the public callable.
Compare representative actual results with expected results; serialization/type checks alone do not
validate behavior. Mark separate validation nodes deliverable=false. Internal helpers remain allowed.
Document inputs, outputs, limitations and side effects. Keep final descriptions, parameter names and
completion criteria consistent with established analysis; diagnostic hypotheses are not requirements.
"""
