"""Check that wrapper selection and descriptor-table regressions fail their tests."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe  # noqa: E402


CASES = [
    (
        "wrapper-command-is-selected-once",
        '        argv = prefix + native if objection is None else ()\n',
        '        argv = native if objection is None else ()\n',
    ),
    (
        "wrapper-query-uses-selected-environment",
        '        query = self._wrapper_query(launch["process"]["environment"]["values"], native, cwd)\n',
        '        query = self._wrapper_query(dict(os.environ), native, cwd)\n',
    ),
    (
        "wrapper-command-is-revalidated",
        '        matches = current == fact["query"] and fact["objection"] is None\n',
        '        matches = fact["objection"] is None\n',
    ),
    (
        "wrapper-error-prevents-unwrapped-launch",
        '        die(command.fact["objection"])\n',
        '        return command\n',
    ),
    (
        "wrapper-report-keeps-private-arguments",
        '    print(wrap_command(native, f"{prefix}command  ", f"{prefix}{\'\':11}", paste=True))\n',
        '    print(wrap_command(tuple(command), f"{prefix}command  ", f"{prefix}{\'\':11}", paste=True))\n',
    ),
    (
        "wrapper-report-keeps-private-arguments",
        '                + (command.fact["display"] if isinstance(command, LaunchCommand) else shlex.join(command)))\n',
        '                + shlex.join(command))\n',
    ),
    (
        "pane-does-not-copy-source-ipc",
        '            prepared.pop(name, None)\n',
        '            prepared.get(name)\n',
        {"stale-caller-ipc-is-not-reused": "The source channel also remains when the caller has a non-inheritable descriptor."},
    ),
    (
        "direct-exec-retains-own-ipc",
        '            if valid:\n                prepared["NODE_CHANNEL_FD"] = raw\n',
        '            if valid and not direct:\n                prepared["NODE_CHANNEL_FD"] = raw\n',
    ),
    (
        "stale-caller-ipc-is-not-reused",
        '                valid = os.get_inheritable(fd) and (statlib.S_ISSOCK(mode) or statlib.S_ISFIFO(mode))\n',
        '                valid = statlib.S_ISSOCK(mode) or statlib.S_ISFIFO(mode)\n',
    ),
    (
        "every-launch-route-selects-wrapper",
        '        command = planned_command(ctx, args, passthrough)\n        # A lead is the socket\'s namesake:',
        '        command = build_command(ctx, args, passthrough)\n        # A lead is the socket\'s namesake:',
    ),
    (
        "wrapper-options-stop-at-literal-arguments",
        '            if token == "--":\n                break\n',
        '            if token == "--" and False:\n                break\n',
    ),
    (
        "wrapper-settings-read-is-single-and-regular",
        '            if not statlib.S_ISREG(os.fstat(descriptor).st_mode):\n',
        '            if False and not statlib.S_ISREG(os.fstat(descriptor).st_mode):\n',
    ),
    (
        "wrapper-exec-uses-platform-argv",
        '        os.execvpe(command[0], list(command), environ)\n',
        '        os.execvpe(command[0], command, environ)\n',
    ),
]


if __name__ == "__main__":
    probe.expect(CASES, live=True, jobs=2, timeout=180)
