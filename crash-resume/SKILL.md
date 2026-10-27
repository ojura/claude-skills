---
name: crash-resume
description: Bring back the Claude Code sessions, team leads and tmux teammates that were running when the machine froze, a suspend failed, or GNOME Shell or gnome-terminal-server crashed. Finds what was running from the transcripts, restarts the leads as plain sessions in one GNOME Terminal window with their team names, models and effort, resumes their teammates into tmux with agent-resume under the lead's environment, and reconnects each lead to its team's mail. Also restarts a tmux swarm that was started with the wrong environment, keeping its agents and layout. Use when the user asks to restore their Claude sessions, tabs, leads or teammates after a crash.
---

# Crash resume

A crash ends every claude process in the desktop session: the sessions in terminal
tabs, the team leads among them, and the teammates in tmux swarms. This skill finds
which of them were running, starts the leads again in one terminal window, resumes the
teammates with agent-resume (from the team-necromancy skill), and checks that each lead
reads its team's mail again.

The scripts are in `scripts/`; the examples below set `S=~/.claude/skills/crash-resume/scripts`.

## What a crash leaves behind

- Every claude process writes a `cost-state` record to its transcript when it exits.
  A transcript last modified within seconds of the crash therefore belonged to a
  process that was running at the crash. Sessions in terminal tabs exit when
  gnome-terminal-server or the login session ends; teammates in tmux exit when their
  tmux server does, which can be several seconds later.
- After a hard freeze, a transcript can end in NUL bytes: writes that never reached
  the disk. The last few records are lost. Leave transcripts as they are.
- `~/.claude/sessions/<pid>.json` loses the entries of dead processes, so after a
  crash it lists only processes started since.
- A plain lead deletes its team directory under `~/.claude/teams/` when it exits and
  rewrites `config.json` with only itself when it starts. After a crash the team
  directories do not list the teammates; the teammates' transcripts do, through their
  `teamName` and `agentName` stamps.
- The task lists under `~/.claude/tasks/session-<team>` survive.
- `startTime` plus `totalDuration` in `cost-state` is not an exit time: `startTime`
  stays the same across resumes, and the duration clock stops during suspend.
- Transcript timestamps are UTC.

## 1. Find what was running

1. Find the crash time. For a GNOME Shell crash:
   `journalctl --since '<day>' | grep -iE 'gnome-shell.*crashed|core-dump|Removed session'`.
   For a freeze that ended in a reboot: `journalctl -b -1 -n 20`.
2. Run `$S/crash-inventory.py --at "YYYY-MM-DD HH:MM:SS"` with the crash time (local).
   It only reads. For every transcript written from a minute before to three minutes
   after that time, it prints:
   - whether it is a lead or plain session, or a teammate (its first message is a
     `<teammate-message>`), grouped by team;
   - its stamps, title, working directory and version;
   - its current model and effort, where a `/model` command after the last reply wins
     over the model of that reply;
   - the shell history lines and the `restore-tab.sh` launch log lines that mention the
     session;
   - for a lead, the teams it spawned teammates into, and the task lists with subjects
     that appear in its task calls and task reminders.
   `--sessions <id or prefix>...` inspects given sessions instead of a time window.
3. Agree with the user which sessions come back. Sessions the user had closed stay
   closed. Ask about teammates that never did any work (their only reply is an error)
   and no-op teammates that a lead spawned only to start reading its mail.

## 2. Work out how each lead was launched

- **Team name**, in this order:
  1. The `CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME` value of the last launch. The inventory
     shows launches typed in the shell and launches made through `restore-tab.sh`. In
     zsh's extended history a launch with a duration of 0s was written when it started
     and not updated, which usually means it was still running at the crash; confirm
     with the transcript.
  2. A session that was never resumed runs as `session-<first 8 hex digits of its id>`.
     The inventory shows whether that task list directory exists.
  3. A resumed session started without the override gets a new random team name in each
     process. Take the task list that the inventory matches to its task calls and
     reminders, confirmed by the teams it spawned teammates into.
- **Model and effort**: the current model and effort from the inventory.
- **Other flags**: from the launch command in the shell history.
- **Proxy**: sessions on GPT models run through CLIProxyAPI
  (`source ~/.local/bin/proxy-codex.sh`). For a Claude model that ran through the proxy
  before, ask.

## 3. Start the leads

- A lead runs as a plain `claude --resume <uuid>` with its own flags. Do not give it the
  teammate identity that agent-resume builds when it resumes a lead (`--agent-id
  team-lead@...`, `--agent-name`, `--team-name`, `--parent-session-id`).
- Start it with the launch environment of a claude process that the user started from
  their shell: `$S/env-of-claude.sh "$CLAUDE_PID" ...`. That is the user's shell
  environment, with `~/.zshrc`'s exports and unsets. The tool-call environment is wrong:
  it carries `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`,
  `CLAUDE_CODE_CHILD_SESSION` and the messaging variables, and gnome-terminal passes the
  caller's environment to its tabs. `systemctl --user show-environment` is wrong too: it
  lacks every `~/.zshrc` export, including `CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF`,
  which Claude Code reads only from the launch environment. `env-of-claude.sh` refuses a
  process that has `CLAUDECODE` or `CLAUDE_CODE_CHILD_SESSION` set.
- Open every lead with one gnome-terminal call. A separate call opens a new window or
  adds its tab to whichever window was last active.

  ```bash
  S=~/.claude/skills/crash-resume/scripts
  busctl --user tree org.gnome.Terminal | grep -o 'window/[0-9]*'      # windows before
  $S/env-of-claude.sh "$CLAUDE_PID" gnome-terminal --window \
    --working-directory=/tmp --title=<title> \
      -e "$S/restore-tab.sh session-<team> proxy --model <model> --effort <effort> --resume <uuid>" \
    --tab --working-directory=<cwd> --title=<title> \
      -e "$S/restore-tab.sh session-<team> direct --model <model> --effort <effort> --resume <uuid>"
  busctl --user tree org.gnome.Terminal | grep -o 'window/[0-9]*'      # the new one is the leads' window
  ```

  `restore-tab.sh <team> <proxy|direct> <claude args>` sets the team variable, sources
  the proxy script for `proxy` and refuses to start claude if the proxy does not come up,
  records the launch in `~/.local/state/crash-resume/launches.log`, and leaves a shell in
  the tab when claude exits. The variable has to be set inside the script: an `-e`
  string containing `VAR=value` makes GNOME Terminal 3.52 refuse the whole call with
  "Can only use --wait once".
- The gnome-terminal client can stay alive after it has created every tab. The tabs
  belong to gnome-terminal-server, so stop the client by its PID.
- Check each lead's `/proc/<pid>/environ`: the team variable, the proxy URL for proxy
  sessions, the `~/.zshrc` exports, and none of the tool-call variables.

## 4. Resume the teammates

- Resume one teammate at a time with its lead's environment:

  ```bash
  $S/env-of-claude.sh <lead-pid> agent-resume <name>@session-<team> --no-attach [--model <current model>]
  ```

  agent-resume starts the tmux server `claude-swarm-<lead-pid>` with the environment it
  runs in, and every pane in a tmux server inherits the server's environment, so the
  lead's environment reaches every teammate. That is also what Claude Code's own
  teammate spawn hands to tmux.
- agent-resume takes the model of the last reply. Pass `--model` when the inventory shows
  a later `/model`; an explicit flag replaces the derived one. `--dry-run` prints the
  command for each teammate first.
- Resume by agent id rather than by team. With the team directory gone, a whole-team run
  needs `--all`, which also brings back every former member.
- Add a tab attached to each swarm to the leads' window:

  ```bash
  $S/env-of-claude.sh "$CLAUDE_PID" python3 -I $S/tab-in-window.py <window-id> /tmp tmux -L claude-swarm-<lead-pid> attach
  ```

## 5. Reconnect each lead to its team's mail

A plain lead keeps its roster in memory and reads its team's mail only after it has
spawned a teammate in the current process; until then `agent-resume --doctor
session-<team>` reports the team as SEVERED. Ask the user to have each lead with
teammates spawn a teammate once, for example a no-op Haiku teammate. Then run the doctor
again; it should report CONNECTED.

## Restarting a tmux swarm with a different environment

When a swarm server was started with the wrong environment, its panes keep it, and every
teammate added to that server later gets it too. To restart the server with the lead's
environment and keep its agents and layout:

1. Record the window size and layout, and the agent in each pane:
   `tmux -L <socket> list-windows -F '#{window_width}x#{window_height} #{window_layout}'`,
   `tmux -L <socket> list-panes -F '#{pane_index} #{pane_id} #{pane_pid}'`, the agent id
   from each pane process's command line, and each agent's current model from the
   status line at the bottom of its pane (`capture-pane -p`).
2. Check that every agent is idle: its pane does not show "esc to interrupt", its
   transcript's last turn has ended, and its inbox under
   `~/.claude/teams/<team>/inboxes/` does not hold unread mail. Wait or ask when one is
   busy.
3. Stop each agent with `agent-resume <name>@<team> --stop -y`. The server exits with its
   last pane, and its attach tab closes.
4. Resume the agents as in step 4, in the recorded pane order and with their current
   models, and add the attach tab again.
5. Put the panes back in order with `tmux swap-pane -d -s <pane> -t <pane>`, then apply
   the recorded layout with `tmux select-layout '<layout>'`. Do this after the client has
   attached: the window takes the client's size, and the layout string carries the size
   it was recorded at.

## Rules

- Before stopping or restarting a session or teammate, check that it is idle (its status
  in `~/.claude/sessions/` where it has an entry, the end of its transcript, its pane,
  unread mail), in the same command as the stop.
- Ask before stopping or restarting anything the user did not ask to restart.
- When a hook cannot check a `zsh -c` string, put the commands in a script file and run
  that.
