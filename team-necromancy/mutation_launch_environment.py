"""Check that launch-environment regressions fail the tests that describe them."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe  # noqa: E402


CASES = [
    (
        "launch-environment-revalidates-source",
        '            elif self._proc_env(source["pid"]) != environment["source_values"]:\n',
        '            elif False:\n',
    ),
    (
        "launch-environment-revalidates-source",
        '            if self._process_fact_signature(current_source) != environment["generation"]:\n',
        '            if False:\n',
    ),
    (
        "launch-pane-environment-exec-preserves-exact-values",
        "    \"env=packet['values']; \"\n",
        '    "env=dict(os.environ); "\n',
    ),
    (
        "launch-environment-is-applied-to-server-and-pane",
        '            res = tmux(server["path"], *argv)\n',
        '            res = tmux(socket, *argv)\n',
    ),
    (
        "launch-pane-environment-exec-preserves-exact-values",
        '        packet = json.dumps({"values": dict(self.environment["values"]),\n',
        '        packet = json.dumps({"values": {k: v for k, v in self.environment["values"].items() if k != "PAGER"},\n',
    ),
    (
        "environment-command-checks-source-before-exec",
        '    if not RESOLVER.revalidate_environment_command(plan):\n',
        '    if not RESOLVER.revalidate_environment_command(plan) and False:\n',
    ),
    (
        "environment-command-keeps-argv-and-source",
        '        os.execvpe(plan["command"][0], list(plan["command"]), dict(plan["values"]))\n',
        '        os.execvpe(plan["command"][0], list(plan["command"]), dict(os.environ))\n',
    ),
    (
        "environment-command-refuses-child-source",
        '        inherited_markers = tuple(name for name in self._SHELL_LAUNCH_MARKERS if name in values)\n',
        '        inherited_markers = ()\n',
    ),
    (
        "launch-caller-env-preserves-proxy-wrapper",
        '        if selected is None and not caller_env:\n',
        '        if selected is None:\n',
    ),
    (
        "server-liveness-engine-only",
        '            self.listener.listen(1)\n',
        '            self.listener.listen(1)\n'
        '            if False:\n'
        '                self.listener.connect("/synthetic/tmux-socket")\n',
    ),
]


if __name__ == "__main__":
    probe.expect(CASES, live=True, jobs=2, timeout=180)
