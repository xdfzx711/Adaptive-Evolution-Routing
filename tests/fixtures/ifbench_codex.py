"""Offline Codex-shaped transport for raw-text IFBench integration tests."""
import json
import sys
import tomllib

if '--version' in sys.argv:
    print('fake-codex 0.162.1')
else:
    task = json.load(sys.stdin)
    assert 'instruction_id_list' not in task['input'] and 'kwargs' not in task['input']
    assert '--output-schema' not in sys.argv
    values = [sys.argv[i + 1] for i, arg in enumerate(sys.argv[:-1]) if arg == '--config']
    prompt = next(tomllib.loads(value)['developer_instructions'] for value in values if value.startswith('developer_instructions='))
    output = 'Hello' if prompt.startswith('IMPROVED') else 'bad answer for task'
    for event in [{'type': 'thread.started', 'thread_id': 'fixture'}, {'type': 'turn.started'},
        {'type': 'item.completed', 'item': {'id': 'answer', 'type': 'agent_message', 'text': output}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}}]:
        print(json.dumps(event), flush=True)
