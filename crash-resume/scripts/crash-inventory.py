#!/usr/bin/env python3
"""List the Claude Code sessions and teammates that were running at a crash.

Every claude process writes a cost-state record to its transcript when it exits, so
a transcript last modified within seconds of the crash belonged to a process that was
running then. For each such transcript this prints what a restore needs: whether it
is a lead or a teammate, its stamps, current model and effort, the launch commands
recorded for it, and the evidence for a lead's team name. It only reads.

usage: crash-inventory.py --at "YYYY-MM-DD HH:MM:SS" [--before 60] [--after 180]
       crash-inventory.py --sessions <uuid or 8-hex prefix>...
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys

HOME = os.path.expanduser('~')
PROJECTS = os.path.join(HOME, '.claude', 'projects')
TASKS = os.path.join(HOME, '.claude', 'tasks')
HISTORY = os.path.join(HOME, '.zsh_history')
LAUNCH_LOG = os.path.join(os.environ.get('XDG_STATE_HOME', os.path.join(HOME, '.local', 'state')),
                          'crash-resume', 'launches.log')

COMMAND_ARGS = re.compile(r'<command-name>/(model|effort)</command-name>.*?<command-args>(.*?)</command-args>', re.S)
SPAWN_ID = re.compile(r'agent_id: ([\w.-]+@session-[0-9a-f]{8})')


def local(ts):
    return datetime.datetime.fromtimestamp(ts).strftime('%m-%d %H:%M:%S')


def utc_to_local(iso):
    if not iso:
        return '?'
    t = datetime.datetime.fromisoformat(iso.replace('Z', '+00:00'))
    return t.astimezone().strftime('%m-%d %H:%M:%S')


def message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if item.get('type') == 'text':
                parts.append(item.get('text', ''))
            elif item.get('type') == 'tool_result':
                inner = item.get('content')
                parts.append(inner if isinstance(inner, str) else message_text(inner))
        return '\n'.join(parts)
    return ''


def read_transcript(path):
    info = {'agent': [], 'team': [], 'model': None, 'model_from': None, 'effort': None,
            'title': None, 'cwd': None, 'version': None, 'first_user': None,
            'last_typed': None, 'spawns': {}, 'task_text': [], 'last_ts': None}
    task_calls = set()
    with open(path, 'rb') as fh:
        data = fh.read()
    stripped = data.rstrip(b'\0')
    info['nul_tail'] = len(data) - len(stripped)
    for raw in stripped.split(b'\n'):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        for key, field in (('agent', 'agentName'), ('team', 'teamName')):
            value = d.get(field)
            if value and value not in info[key]:
                info[key].append(value)
        info['cwd'] = d.get('cwd') or info['cwd']
        info['version'] = d.get('version') or info['version']
        info['last_ts'] = d.get('timestamp') or info['last_ts']
        effort = d.get('perTurnEffort') or d.get('effort')
        if isinstance(effort, str):
            info['effort'] = effort
        kind = d.get('type')
        attachment = d.get('attachment') if isinstance(d.get('attachment'), dict) else {}
        if kind == 'attachment' and attachment.get('type') == 'task_reminder':
            # The task list Claude Code reminds the session of: the list of the team it runs as.
            info['task_text'].append(json.dumps(attachment, ensure_ascii=False))
        if kind in ('custom-title', 'ai-title'):
            info['title'] = d.get('customTitle') or d.get('aiTitle') or info['title']
        message = d.get('message') if isinstance(d.get('message'), dict) else {}
        if kind == 'assistant':
            model = message.get('model')
            if model and model != '<synthetic>':
                info['model'], info['model_from'] = model, 'last reply'
            for item in message.get('content') or []:
                if isinstance(item, dict) and item.get('type') == 'tool_use' and str(item.get('name')).startswith('Task'):
                    task_calls.add(item.get('id'))
                    info['task_text'].append(json.dumps(item.get('input'), ensure_ascii=False))
        if kind != 'user':
            continue
        text = message_text(message.get('content'))
        for item in message.get('content') if isinstance(message.get('content'), list) else []:
            if isinstance(item, dict) and item.get('type') == 'tool_result' and item.get('tool_use_id') in task_calls:
                inner = item.get('content')
                info['task_text'].append(inner if isinstance(inner, str) else message_text(inner))
        for name, args in COMMAND_ARGS.findall(text):
            args = args.strip()
            if args and name == 'model':
                info['model'], info['model_from'] = args, '/model after the last reply'
            elif args and name == 'effort':
                info['effort'] = args
        for agent_id in SPAWN_ID.findall(text):
            team = agent_id.split('@', 1)[1]
            info['spawns'].setdefault(team, set()).add(agent_id.split('@', 1)[0])
        if d.get('isMeta') or not isinstance(message.get('content'), str):
            continue
        content = message['content']
        if info['first_user'] is None:
            info['first_user'] = content
        if not content.startswith('<') and not content.startswith('Another Claude session sent a message'):
            info['last_typed'] = (d.get('timestamp'), content)
    return info


def history_entries():
    try:
        raw = open(HISTORY, 'rb').read().decode('utf-8', 'replace')
    except OSError:
        return []
    entries = []
    for chunk in ('\n' + raw).split('\n: ')[1:]:
        m = re.match(r'(\d+):(\d+);(.*)', chunk, re.S)
        if m:
            entries.append((int(m.group(1)), int(m.group(2)), m.group(3).strip()))
    return entries


def launch_log_lines():
    try:
        return open(LAUNCH_LOG, encoding='utf-8', errors='replace').read().splitlines()
    except OSError:
        return []


def task_lists():
    lists = {}
    for task_dir in glob.glob(os.path.join(TASKS, 'session-*')):
        subjects = set()
        for task_file in glob.glob(os.path.join(task_dir, '*.json')):
            try:
                subject = json.load(open(task_file)).get('subject')
            except (OSError, ValueError, AttributeError):
                continue
            if subject:
                subjects.add(subject)
        if subjects:
            lists[os.path.basename(task_dir)] = subjects
    return lists


def select(args):
    paths = [p for p in glob.glob(os.path.join(PROJECTS, '*', '*.jsonl'))]
    if args.sessions:
        return [p for p in paths if any(os.path.basename(p).startswith(s) for s in args.sessions)]
    at = datetime.datetime.strptime(args.at, '%Y-%m-%d %H:%M:%S').timestamp()
    return [p for p in paths if at - args.before <= os.path.getmtime(p) <= at + args.after]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--at', help='crash time, local, "YYYY-MM-DD HH:MM:SS"')
    group.add_argument('--sessions', nargs='+', help='session ids or prefixes to inspect instead')
    parser.add_argument('--before', type=int, default=60, help='seconds before --at to include (default 60)')
    parser.add_argument('--after', type=int, default=180, help='seconds after --at to include (default 180)')
    args = parser.parse_args()

    paths = select(args)
    if not paths:
        sys.exit('no transcript was written in that window')
    history = history_entries()
    launches = launch_log_lines()
    lists = task_lists()

    sessions = []
    for path in paths:
        info = read_transcript(path)
        info['path'] = path
        info['id'] = os.path.basename(path)[:-len('.jsonl')]
        info['mtime'] = os.path.getmtime(path)
        info['teammate'] = (info['first_user'] or '').startswith('<teammate-message')
        sessions.append(info)

    leads = [s for s in sessions if not s['teammate']]
    teammates = [s for s in sessions if s['teammate']]
    print(f'{len(leads)} lead or plain sessions, {len(teammates)} teammates\n')

    for s in sorted(leads, key=lambda s: s['mtime']):
        print(f"== {s['id']}  lead or plain session")
        print(f"   transcript {os.path.relpath(s['path'], PROJECTS)}, last written {local(s['mtime'])}, "
              f"NUL bytes at the end: {s['nul_tail']}")
        print(f"   title: {s['title']!r}   cwd: {s['cwd']}   version: {s['version']}")
        print(f"   stamps: agentName {s['agent'] or '-'}, teamName {s['team'] or '-'}")
        print(f"   model now: {s['model']} ({s['model_from']})   effort: {s['effort']}")
        if s['last_typed']:
            print(f"   last typed by the user: {utc_to_local(s['last_typed'][0])} {s['last_typed'][1][:80]!r}")
        for start, duration, command in history:
            if s['id'] in command:
                print(f"   shell history: {local(start)} {duration}s {command[:200]}")
        for line in launches:
            if s['id'] in line:
                print(f"   restore-tab log: {line[:200]}")
        for team, names in sorted(s['spawns'].items()):
            print(f"   spawned teammates into {team}: {', '.join(sorted(names))}")
        task_text = '\n'.join(s['task_text'])
        counts = {name: sum(1 for subject in subjects if subject in task_text) for name, subjects in lists.items()}
        for name, n in sorted(((k, v) for k, v in counts.items() if v), key=lambda kv: -kv[1])[:3]:
            print(f"   task list {name}: {n} of its {len(lists[name])} task subjects appear in this session's task calls and task reminders")
        own = 'session-' + s['id'][:8]
        state = 'exists' if os.path.isdir(os.path.join(TASKS, own)) else 'missing'
        print(f"   its own task list {own}: {state}\n")

    by_team = {}
    for s in teammates:
        by_team.setdefault(s['team'][-1] if s['team'] else '?', []).append(s)
    for team, members in sorted(by_team.items()):
        print(f'== teammates of {team}')
        for s in sorted(members, key=lambda s: s['mtime']):
            name = s['agent'][-1] if s['agent'] else '?'
            print(f"   {name}@{team}  {s['id']}  last written {local(s['mtime'])}  cwd {s['cwd']}")
            print(f"      model now: {s['model']} ({s['model_from']})   effort: {s['effort']}   "
                  f"last record {utc_to_local(s['last_ts'])}")
        print()


if __name__ == '__main__':
    main()
