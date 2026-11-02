---
name: task-output-contract
description: Use when a task supplies a structured output contract and the answer must match its declared fields and types.
---

Read the output contract in the public task input before preparing the final answer.
Check required fields, their types, and whether additional fields are permitted.
Compute the result using the task data, then return the requested structure without surrounding prose or Markdown fences.
