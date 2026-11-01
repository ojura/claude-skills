#!/usr/bin/env python3
"""Measure Claude's token usage for full-mode agent summary requests.

The input is synthetic. Credentials stay in memory, and output contains only
request settings, response identifiers, generated labels, timings and usage.
The cache markers reproduce two 2.1.316 full-mode message arrangements. This
replays API requests directly; it does not run Claude Code's summary timer.
"""

import argparse
import copy
import datetime
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


API_URL = 'https://api.anthropic.com/v1/messages'
IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
MODEL_NAMES = (
    'claude-fable-5-1',
    'claude-opus-5-5',
    'claude-sonnet-5-5',
)
SUMMARY_TEMPLATE = (
    'Describe your most recent action in 3-5 words using present tense (-ing). '
    'Name the file or function, not the branch. Do not use tools.\n'
    '{previous}\n'
    'Good: "Reading runAgent.ts"\n'
    'Good: "Fixing null check in validate.ts"\n'
    'Good: "Running auth module tests"\n'
    'Good: "Adding retry logic to fetchUser"\n'
    '\n'
    'Bad (past tense): "Analyzed the branch diff"\n'
    'Bad (too vague): "Investigating the issue"\n'
    'Bad (too long): "Reviewing full branch diff and AgentTool.tsx integration"\n'
    'Bad (branch name): "Analyzed adam/background-summary branch diff"'
)
TOOLS = [{
    'name': 'Read',
    'description': 'Read a source file.',
    'input_schema': {
        'type': 'object',
        'properties': {'file_path': {'type': 'string'}},
        'required': ['file_path'],
    },
}]


def summary_prompt(previous=None):
    """Use the full-mode Oi() prompt from Claude Code 2.1.316."""
    line = ''
    if previous:
        line = f'\nPrevious: {json.dumps(previous)} {chr(0x2014)} say something NEW.\n'
    return SUMMARY_TEMPLATE.format(previous=line)


def input_messages(nonce, long_tail=False):
    """Construct a synthetic read operation with a stable cached prefix."""
    records = '\n'.join(
        f'Case {i:05d}: validateSession.ts rejects a missing access token; '
        'expected status 401, observed status 401. The authentication check '
        'runs before the protected handler and preserves the request ID.'
        for i in range(512)
    )
    first = (
        f'Synthetic measurement {nonce}. The following records are test data.\n'
        f'{records}\n\n'
        'Read validateSession.ts and check its missing-token branch. '
        'Report one sentence, and do not call another tool.'
    )
    comments = ''
    if long_tail:
        comments = '\n'.join(
            f'// Case {i:05d}: missing access token returns 401 before the '
            'handler runs; preserve the request ID in the response.'
            for i in range(512)
        ) + '\n'
    code = (
        comments
        + 'export function validateSession(request) {\n'
        + "  const token = request.headers.get('authorization');\n"
        + '  if (!token || !token.trim()) {\n'
        + '    return {status: 401, requestId: request.id};\n'
        + '  }\n'
        + '  return {status: 200, requestId: request.id};\n'
        + '}\n'
    )
    return [
        {'role': 'user', 'content': [{'type': 'text', 'text': first}]},
        {'role': 'assistant', 'content': [{
            'type': 'tool_use',
            'id': 'toolu_synthetic_read',
            'name': 'Read',
            'input': {'file_path': '/synthetic/validateSession.ts'},
        }]},
        {'role': 'user', 'content': [{
            'type': 'tool_result',
            'tool_use_id': 'toolu_synthetic_read',
            'content': code,
        }]},
    ]


def request_body(model, effort, messages):
    """Use the inference settings of full-mode summaries."""
    return {
        'model': model,
        'system': [{'type': 'text', 'text': IDENTITY}],
        'tools': TOOLS,
        'messages': messages,
        'max_tokens': 32000,
        'thinking': {'type': 'adaptive', 'display': 'summarized'},
        'output_config': {'effort': effort},
    }


def main_body(model, messages):
    """Mark the latest business message and its preceding assistant message."""
    messages = copy.deepcopy(messages)
    for index in (-2, -1):
        messages[index]['content'][-1]['cache_control'] = {'type': 'ephemeral'}
    return request_body(model, 'high', messages)


def in_flight_body(model, effort, messages, previous=None):
    """Append Describe text and mark the preceding assistant message."""
    messages = copy.deepcopy(messages)
    messages[-2]['content'][-1]['cache_control'] = {'type': 'ephemeral'}
    messages[-1]['content'].append({
        'type': 'text', 'text': summary_prompt(previous),
    })
    return request_body(model, effort, messages)


def after_reply_body(model, messages, reply, previous=None):
    """After a completed reply, the stepped-back marker covers that reply."""
    messages = copy.deepcopy(messages)
    reply = copy.deepcopy(reply)
    reply[-1]['cache_control'] = {'type': 'ephemeral'}
    messages.append({'role': 'assistant', 'content': reply})
    messages.append({'role': 'user', 'content': summary_prompt(previous)})
    return request_body(model, 'high', messages)


def call(body, token):
    """Send one request and retain its accounting without its credentials."""
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        headers={
            'Authorization': 'Bearer ' + token,
            'anthropic-version': '2023-06-01',
            'anthropic-beta': 'oauth-2025-04-20,claude-code-20250219',
            'Content-Type': 'application/json',
            'User-Agent': 'claude-cli/2.1.316 (external, cli)',
        },
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            failure = json.load(error).get('error', {})
        except (ValueError, AttributeError):
            failure = {'type': 'unreadable_error'}
        return {
            'http_status': error.code,
            'elapsed_seconds': round(time.monotonic() - start, 6),
            'error': failure,
        }, None
    text = '\n'.join(
        block.get('text', '') for block in result.get('content', [])
        if block.get('type') == 'text'
    )
    return {
        'http_status': 200,
        'elapsed_seconds': round(time.monotonic() - start, 6),
        'response_id': result.get('id'),
        'response_model': result.get('model'),
        'usage': result.get('usage'),
        'stop_reason': result.get('stop_reason'),
        'text': text,
        'content_types': [b.get('type') for b in result.get('content', [])],
    }, result.get('content')


def main():
    """Run the measurements and return failure if an API request fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--models', nargs='+', default=list(MODEL_NAMES))
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--credentials', type=Path,
                        default=Path.home() / '.claude' / '.credentials.json')
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text())['claudeAiOauth']
    token = credentials['accessToken']
    if credentials.get('expiresAt', 0) / 1000 <= time.time():
        raise SystemExit('The native OAuth credential has expired.')
    run_id = datetime.datetime.now(datetime.timezone.utc).isoformat()
    failures = 0
    with args.output.open('x') as output:
        def measure(body, model, phase, tail, effort, iteration):
            nonlocal failures
            row, reply = call(body, token)
            failures += row['http_status'] != 200
            row.update({
                'run_id': run_id,
                'timestamp_utc': datetime.datetime.now(
                    datetime.timezone.utc).isoformat(),
                'model': model, 'phase': phase, 'tail': tail,
                'effort': effort, 'iteration': iteration,
            })
            output.write(json.dumps(row, ensure_ascii=False) + '\n')
            output.flush()
            print(json.dumps(row, ensure_ascii=False), flush=True)
            return row, reply

        for model in args.models:
            nonce = run_id + ':' + model
            for tail in ('short', 'long'):
                messages = input_messages(nonce, long_tail=tail == 'long')
                seed, reply = measure(main_body(model, messages), model,
                                      'work', tail, 'high', 0)
                if seed['http_status'] != 200:
                    continue
                for effort in ('high', 'xhigh'):
                    previous = None
                    for iteration in range(args.rounds):
                        body = in_flight_body(model, effort, messages, previous)
                        row, _ = measure(body, model, 'summary_in_flight', tail,
                                         effort, iteration)
                        if row['http_status'] != 200:
                            break
                        previous = row['text']
                if tail == 'short' and reply and seed['stop_reason'] == 'end_turn':
                    previous = None
                    for iteration in range(args.rounds):
                        body = after_reply_body(model, messages, reply, previous)
                        row, _ = measure(body, model, 'summary_after_reply', tail,
                                         'high', iteration)
                        if row['http_status'] != 200:
                            break
                        previous = row['text']
    return int(failures > 0)


if __name__ == '__main__':
    sys.exit(main())
