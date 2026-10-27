#!/usr/bin/env python3
"""List the Claude Code sessions and teammates a crash ended, with what a restore needs.

A claude process appends a cost-state record to its transcript when it exits normally,
so after a crash that ended processes through their exit path (a GNOME Shell or
gnome-terminal-server crash), the transcripts last modified around the crash belong to
the processes that were running then: select them with --at. A freeze or a failed
suspend ends processes without that record: select the transcripts of the boot being
recovered that end without it with --boot. --sessions inspects given sessions.

For each transcript this prints whether it is a lead or plain session or a teammate,
whether a process runs it now, its entrypoint, launch directory, current model and
effort, the shell history lines and restore-tab.sh launch-log lines that mention it,
and for a lead the evidence for its team name. It only reads.

usage: crash-inventory.py --at "YYYY-MM-DD HH:MM:SS" [--before 60] [--after 180]
       crash-inventory.py --boot -1
       crash-inventory.py --sessions <uuid or prefix>...
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys

HOME = os.path.expanduser('~')
PROJECTS = os.path.join(HOME, '.claude', 'projects')
TASKS = os.path.join(HOME, '.claude', 'tasks')
SESSIONS = os.path.join(HOME, '.claude', 'sessions')
HISTORY = os.path.join(HOME, '.zsh_history')
LAUNCH_LOG = os.path.join(os.environ.get('XDG_STATE_HOME') or os.path.join(HOME, '.local', 'state'),
                          'crash-resume', 'launches.log')

EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')
MODEL_COMMAND = re.compile(r'<command-name>/model</command-name>.*?<command-args>(.*?)</command-args>', re.S)
MODEL_SET = 'local-command-stdout>Set model to'
EFFORT_SET = re.compile(r'local-command-stdout>Set effort level to (\w+)')
SPAWN_ID = re.compile(r'agent_id: ([\w.-]+)@([\w.-]+)')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
TEAM_OR_PROXY = re.compile(r'CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME|proxy-codex')


def local(ts):
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')


def utc_to_local(iso):
    if not iso:
        return '?'
    return datetime.datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d %H:%M:%S')


def iso_to_epoch(iso):
    return datetime.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()


def message_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'text':
            parts.append(item.get('text', ''))
        elif item.get('type') == 'tool_result':
            parts.append(message_text(item.get('content')))
    return '\n'.join(parts)


def first_timestamp(path):
    with open(path, 'rb') as fh:
        for raw in fh:
            try:
                ts = json.loads(raw.strip(b'\0')).get('timestamp')
            except (ValueError, AttributeError):
                continue
            if ts:
                return ts
    return None


def trailing_nuls(path):
    with open(path, 'rb') as fh:
        fh.seek(max(0, os.path.getsize(path) - 65536))
        tail = fh.read()
    return len(tail) - len(tail.rstrip(b'\0'))


def last_record_type(path):
    with open(path, 'rb') as fh:
        fh.seek(max(0, os.path.getsize(path) - 262144))
        lines = fh.read().rstrip(b'\0').split(b'\n')
    for raw in reversed(lines):
        try:
            return json.loads(raw.strip(b'\0')).get('type')
        except (ValueError, AttributeError):
            continue
    return None


def read_transcript(path):
    info = {'agent': None, 'team': None, 'model': None, 'model_from': None, 'effort': None,
            'title': None, 'first_cwd': None, 'cwd': None, 'version': None, 'entrypoint': None,
            'permission_mode': None, 'first_user': None, 'last_typed': None, 'last_ts': None,
            'replies': 0, 'api_errors': 0, 'spawns': {}, 'task_text': []}
    agent_calls, task_calls = set(), set()
    model_args = None
    with open(path, 'rb') as fh:
        for raw in fh:
            try:
                d = json.loads(raw.strip(b'\0'))
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            kind = d.get('type')
            ts = d.get('timestamp')
            info['last_ts'] = ts or info['last_ts']
            if kind not in ('agent-name', 'custom-title', 'ai-title'):
                info['agent'] = d.get('agentName') or info['agent']
                info['team'] = d.get('teamName') or info['team']
            if d.get('cwd'):
                info['first_cwd'] = info['first_cwd'] or d['cwd']
                info['cwd'] = d['cwd']
            info['version'] = d.get('version') or info['version']
            info['entrypoint'] = info['entrypoint'] or d.get('entrypoint')
            if kind == 'permission-mode':
                info['permission_mode'] = d.get('permissionMode')
            effort = d.get('perTurnEffort') or d.get('effort')
            if effort in EFFORT_LEVELS:
                info['effort'] = effort
            if kind in ('custom-title', 'ai-title'):
                info['title'] = d.get('customTitle') or d.get('aiTitle') or info['title']
            attachment = d.get('attachment') if isinstance(d.get('attachment'), dict) else {}
            if kind == 'attachment' and attachment.get('type') == 'model':
                model_id = (attachment.get('identity') or {}).get('modelId')
                if model_id:
                    info['model'], info['model_from'] = model_id, 'model attachment'
            if kind == 'attachment' and attachment.get('type') == 'task_reminder':
                # The task list Claude Code reminds the session of: the list of the team it runs as.
                info['task_text'].append(json.dumps(attachment, ensure_ascii=False))
            message = d.get('message') if isinstance(d.get('message'), dict) else {}
            content = message.get('content')
            if kind == 'assistant':
                info['replies'] += 1
                if d.get('isApiErrorMessage'):
                    info['api_errors'] += 1
                model = message.get('model')
                if model and model != '<synthetic>':
                    info['model'], info['model_from'] = model, 'last reply'
                for item in content if isinstance(content, list) else []:
                    if not isinstance(item, dict) or item.get('type') != 'tool_use':
                        continue
                    if item.get('name') == 'Agent':
                        agent_calls.add(item.get('id'))
                    if str(item.get('name')).startswith('Task'):
                        task_calls.add(item.get('id'))
                        info['task_text'].append(json.dumps(item.get('input'), ensure_ascii=False))
            if kind != 'user':
                continue
            for item in content if isinstance(content, list) else []:
                if not isinstance(item, dict) or item.get('type') != 'tool_result':
                    continue
                if item.get('tool_use_id') in agent_calls:
                    for name, team in SPAWN_ID.findall(message_text(item.get('content'))):
                        info['spawns'].setdefault(team, {})[name] = ts or ''
                if item.get('tool_use_id') in task_calls:
                    info['task_text'].append(message_text(item.get('content')))
            if not isinstance(content, str) or d.get('isMeta'):
                continue
            # Slash commands and their output arrive as user records with string content.
            found = MODEL_COMMAND.search(content)
            if found and found.group(1).strip():
                model_args = found.group(1).strip()
            if MODEL_SET in content and model_args:
                info['model'], info['model_from'] = model_args, '/model'
            effort_set = EFFORT_SET.search(content)
            if effort_set and effort_set.group(1) in EFFORT_LEVELS:
                info['effort'] = effort_set.group(1)
            if info['first_user'] is None:
                info['first_user'] = content
            if not content.startswith('<') and not content.startswith('Another Claude session sent a message'):
                info['last_typed'] = (ts, content)
    return info


def unmetafy(data):
    # zsh writes bytes 0x83-0xa2 in history as 0x83 followed by the byte XOR 32.
    out, i = bytearray(), 0
    while i < len(data):
        if data[i] == 0x83 and i + 1 < len(data):
            out.append(data[i + 1] ^ 32)
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def history_entries():
    try:
        raw = unmetafy(open(HISTORY, 'rb').read()).decode('utf-8', 'replace')
    except OSError:
        return []
    entries = []
    for chunk in ('\n' + raw).split('\n: ')[1:]:
        m = re.match(r'(\d+):(\d+);(.*)', chunk, re.S)
        if m:
            command = m.group(3).strip().replace('\\\n', ' ')
            entries.append((int(m.group(1)), int(m.group(2)), command))
    return entries


def history_for(session_id, entries):
    """History lines that mention the session, each with the team and proxy lines of the
    same shell before it. A shell appends its whole history when it exits, in start-time
    order, so the block of one shell ends where the start times step backwards."""
    found = []
    for i, (start, duration, command) in enumerate(entries):
        if session_id not in command:
            continue
        context = []
        for j in range(i - 1, max(-1, i - 200), -1):
            if entries[j][0] > entries[j + 1][0]:
                break  # an earlier shell's block
            if TEAM_OR_PROXY.search(entries[j][2]) and session_id not in entries[j][2]:
                context.insert(0, entries[j])
        found.append(((start, duration, command), context))
    return sorted(found, key=lambda item: item[0][0])


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


def running_sessions():
    """Session ids that a live process runs now, from the registry and process command lines."""
    running = {}
    for entry in glob.glob(os.path.join(SESSIONS, '*.json')):
        try:
            d = json.load(open(entry))
        except (OSError, ValueError):
            continue
        if d.get('sessionId') and os.path.exists(f"/proc/{d.get('pid')}"):
            running[d['sessionId']] = d['pid']
    for cmdline in glob.glob('/proc/[0-9]*/cmdline'):
        try:
            args = open(cmdline, 'rb').read().split(b'\0')
        except OSError:
            continue
        if not args or not os.path.basename(args[0].decode('utf-8', 'replace')).startswith('claude'):
            continue
        for i, arg in enumerate(args[:-1]):
            if arg == b'--resume' and UUID.fullmatch(args[i + 1].decode('utf-8', 'replace')):
                running.setdefault(args[i + 1].decode(), int(cmdline.split('/')[2]))
    return running


def boot_range(index):
    out = subprocess.run(['journalctl', '--list-boots', '-o', 'json'], capture_output=True, text=True, check=True)
    for boot in json.loads(out.stdout):
        if boot['index'] == index:
            return boot['first_entry'] / 1e6, boot['last_entry'] / 1e6
    sys.exit(f'journalctl does not list a boot with index {index}')


def select(args):
    paths = glob.glob(os.path.join(PROJECTS, '*', '*.jsonl'))
    if args.sessions:
        return [p for p in paths if any(os.path.basename(p).startswith(s) for s in args.sessions)], None
    if args.boot is not None:
        first, last = boot_range(args.boot)
        chosen = [p for p in paths if first <= os.path.getmtime(p) <= last + args.after
                  and last_record_type(p) != 'cost-state']
        return chosen, f'boot {args.boot} ({local(first)} to {local(last)}), ending without an exit record'
    at = datetime.datetime.strptime(args.at, '%Y-%m-%d %H:%M:%S').timestamp()
    chosen = []
    for p in paths:
        if not at - args.before <= os.path.getmtime(p) <= at + args.after:
            continue
        started = first_timestamp(p)
        if started and iso_to_epoch(started) > at:
            continue  # started after the crash, for example the restoring session
        chosen.append(p)
    return chosen, f'{local(at - args.before)} to {local(at + args.after)}'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--at', help='crash time, local, "YYYY-MM-DD HH:MM:SS"')
    group.add_argument('--boot', type=int, help='journalctl boot index of a boot that ended in a freeze, e.g. -1')
    group.add_argument('--sessions', nargs='+', help='session ids or prefixes to inspect')
    parser.add_argument('--before', type=int, default=60, help='seconds before --at to include (default 60)')
    parser.add_argument('--after', type=int, default=180, help='seconds after --at or the boot end (default 180)')
    args = parser.parse_args()

    paths, window = select(args)
    if not paths:
        sys.exit(f'Every transcript was last modified outside {window}. After a freeze or a failed '
                 f'suspend, try --boot with the index of the boot that froze (journalctl --list-boots).')
    if window:
        print(f'transcripts selected: last modified in {window}')
    history = history_entries()
    launches = launch_log_lines()
    lists = task_lists()
    running = running_sessions()

    sessions = []
    for path in paths:
        info = read_transcript(path)
        info['path'] = path
        info['id'] = os.path.basename(path)[:-len('.jsonl')]
        info['mtime'] = os.path.getmtime(path)
        info['nul_tail'] = trailing_nuls(path)
        info['teammate'] = (info['first_user'] or '').startswith('<teammate-message')
        info['running'] = running.get(info['id'])
        sessions.append(info)

    leads = [s for s in sessions if not s['teammate']]
    teammates = [s for s in sessions if s['teammate']]
    print(f'{len(leads)} lead or plain sessions, {len(teammates)} teammates\n')

    def common(s, indent):
        state = f"RUNNING NOW as pid {s['running']}" if s['running'] else 'not running'
        print(f"{indent}{state}; transcript {os.path.relpath(s['path'], PROJECTS)}, last modified "
              f"{local(s['mtime'])}, NUL bytes at the end: {s['nul_tail']}")
        print(f"{indent}entrypoint {s['entrypoint']}, launch directory {s['first_cwd']}, last cwd {s['cwd']}, "
              f"version {s['version']}, permission mode {s['permission_mode']}")
        print(f"{indent}model now: {s['model']} (from the {s['model_from']}), effort: {s['effort']}; "
              f"{s['replies']} replies, {s['api_errors']} of them API errors; last record {utc_to_local(s['last_ts'])}")

    for s in sorted(leads, key=lambda s: s['mtime']):
        print(f"== {s['id']}  lead or plain session, title {s['title']!r}")
        common(s, '   ')
        print(f"   agentName {s['agent'] or '-'}, teamName {s['team'] or '-'} (a lead records these only "
              f"while it runs under a team identity)")
        if s['last_typed']:
            print(f"   last typed by the user: {utc_to_local(s['last_typed'][0])} {s['last_typed'][1][:80]!r}")
        for (start, duration, command), context in history_for(s['id'], history):
            for c_start, _, c_command in context:
                print(f"   shell history, before it: {local(c_start)} {c_command[:160]}")
            print(f"   shell history: {local(start)} {duration}s {command[:200]}")
        for line in launches:
            if s['id'] in line:
                print(f"   restore-tab log: {line[:220]}")
        for team, names in sorted(s['spawns'].items(), key=lambda kv: max(kv[1].values() or [''])):
            last = max(names.values() or [''])
            print(f"   spawned into {team}: {', '.join(sorted(names))}; last spawn {utc_to_local(last)}")
        task_text = '\n'.join(s['task_text'])
        counts = {name: sum(1 for subject in subjects if subject in task_text) for name, subjects in lists.items()}
        for name, n in sorted(((k, v) for k, v in counts.items() if v), key=lambda kv: -kv[1])[:3]:
            print(f"   task list {name}: {n} of its {len(lists[name])} task subjects appear in this "
                  f"session's task calls and task reminders")
        own = 'session-' + s['id'][:8]
        state = 'exists' if os.path.isdir(os.path.join(TASKS, own)) else 'missing'
        print(f"   its own task list {own}: {state}\n")

    by_team = {}
    for s in teammates:
        by_team.setdefault(s['team'] or '?', []).append(s)
    for team, members in sorted(by_team.items()):
        print(f'== teammates of {team}')
        for s in sorted(members, key=lambda s: s['mtime']):
            print(f"   {s['agent'] or '?'}@{team}  {s['id']}")
            common(s, '      ')
        print()


if __name__ == '__main__':
    main()
