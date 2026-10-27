---
name: crash-resume
description: Bring back the Claude Code sessions, team leads and tmux teammates that a crash ended, whether the machine froze, a suspend failed, or GNOME Shell or gnome-terminal-server crashed. Finds what was running and what still runs, restarts the leads as plain sessions in one GNOME Terminal window with their team names, models and effort, resumes their teammates into tmux with agent-resume under the lead's environment, and gets each lead reading its team's mail again. Also restarts a tmux swarm that was started with the wrong environment, keeping its agents and layout. Use when the user asks to restore their Claude sessions, tabs, leads or teammates after a crash.
---

# Crash resume

A crash ends the claude processes of the desktop session: sessions in terminal tabs,
team leads among them, and possibly the teammates in tmux swarms. This skill finds which
of them were running, starts the leads again in one terminal window, resumes the
teammates with agent-resume (from the team-necromancy skill), and gets each lead reading
its team's mail.

A *lead* here is a session that spawned teammates. A *plain lead* runs as an ordinary
`claude --resume <uuid>`, with its team name given by the `CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME`
environment variable. The user wants leads to run this way.

The scripts are in `~/.claude/skills/crash-resume/scripts/`. Shell variables do not
carry over between tool calls, so the commands below spell out that path.

## What a crash leaves behind

- A claude process appends a `cost-state` record to its transcript
  (`~/.claude/projects/<project>/<uuid>.jsonl`) when it exits normally. After a crash
  that ends processes through their exit path, such as a GNOME Shell crash, the
  transcripts last modified around the crash time belong to the processes that were
  running. A freeze or a failed suspend ends them without that record, and a transcript
  can end in NUL bytes: writes that did not reach the disk. Leave transcripts as they
  are.
- What survives depends on the crash. When gnome-terminal-server crashes, the tabs end
  and the tmux swarm servers keep running, because they are children of the systemd user
  manager. When GNOME Shell crashes and the login session ends, the user manager stops and
  takes the swarms with it.
- `~/.claude/sessions/<pid>.json` holds entries for live processes only; teammates have
  only a `.key` file there.
- After a lead restarts, its team's `~/.claude/teams/<team>/config.json` lists only the
  lead. The teammates' transcripts still name their team in their `teamName` and
  `agentName` fields. The task lists under `~/.claude/tasks/session-<team>` survive.
- zsh here writes a shell's history when the shell exits, so a launch typed in a shell
  that a freeze killed is missing from `~/.zsh_history`. A history entry with a 0s
  duration is a command that was still running when its shell exited, or that took under
  a second.
- `startTime` plus `totalDuration` in `cost-state` is not an exit time: `startTime` stays
  the same across resumes. Transcript timestamps are UTC.

## 1. Find what was running, and what still runs

1. Find the crash time:
   `journalctl -o short-iso --since '<day>' | grep -iE 'gnome-shell.*crashed|gnome-terminal-server.service: Main process exited|systemd-oomd.*Killed|core-dump|Removed session'`.
   For a freeze or a failed suspend, `journalctl --list-boots` gives the index of the boot
   that ended in it, and `journalctl -b <index> -n 20` shows how it ended.
2. Run the inventory. It only reads.
   - After a crash with a time: `~/.claude/skills/crash-resume/scripts/crash-inventory.py --at "YYYY-MM-DD HH:MM:SS"`
     (local time) lists the transcripts last modified from a minute before to three
     minutes after it, leaving out sessions that started after it.
   - After a freeze or a failed suspend: `crash-inventory.py --boot <index>` lists the
     transcripts of that boot that end without a `cost-state` record.
   - `crash-inventory.py --sessions <uuid or prefix>...` inspects given sessions.

   For each session it prints whether a process runs it now (RUNNING NOW, from the
   registry and process command lines), lead or plain session versus teammate (a
   teammate's first message is a `<teammate-message>`), entrypoint, launch directory,
   current model and effort, reply and API-error counts, the shell history lines and
   `restore-tab.sh` launch-log lines that mention it, and for a lead the evidence for its
   team name.
3. List the tmux swarm servers that survived: `ls /tmp/tmux-$UID/ | grep claude-swarm`,
   then `tmux -L <socket> list-panes -a -F '#{pane_id} #{pane_pid}'` for each. Leave
   survivors running: the restored lead takes their team name, and the attach tab goes to
   the surviving server.
4. Agree with the user on which sessions come back. Sessions the user had closed stay
   closed. Ask about sessions with an entrypoint other than `cli` (`sdk-cli` is `claude -p`),
   teammates that replied only with API errors, and no-op teammates that a lead spawned
   only to start reading its mail.

## 2. Work out how each lead was launched

- **Team name.** The lead has to run as the team name its teammates carry in their
  `teamName` field; for a lead with teammates that is the strongest evidence. To find it:
  1. The `CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME` value of the last launch. The inventory
     prints shell history lines that mention the session and, before each one, the lines
     of the same shell that set or unset that variable or sourced the proxy script, and the
     `restore-tab.sh` launch log. A resume chosen by title or in the picker does not
     mention the uuid.
  2. A session that was never resumed runs as `session-<first 8 hex digits of its uuid>`.
     The inventory shows whether that task list exists.
  3. A resumed session started without the variable gets a new random team name in each
     process. Take the task list that the inventory matches to the session's task calls
     and task reminders, and the teams it spawned into. Both cover every process of the
     transcript, so prefer the team with the latest spawn.
- **Model and effort**: the inventory's "model now" and "effort". The model comes from
  the latest of the last reply, a `model` attachment (which keeps variants such as
  `[1m]`), and a confirmed `/model` command. When the effort is `None`, leave out
  `--effort`.
- **Launch directory**: the inventory's launch directory, the first working directory in
  the transcript.
- **Other flags**: from the launch command in the history.
- **Proxy**: sessions on GPT models run through CLIProxyAPI. The history lines before a
  launch show whether its shell sourced `proxy-codex.sh`; for a Claude model that ran
  through the proxy, ask.

## 3. Start the leads

- Start every lead as a plain lead. Do not give it the teammate identity that agent-resume
  builds when it resumes a lead (`--agent-id team-lead@...`, `--agent-name`,
  `--team-name`, `--parent-session-id`).
- Start it with the launch environment of a claude process that the user started from a
  shell, through `env-of-claude.sh "$CLAUDE_PID"`: the user's shell environment, with
  `~/.zshrc`'s exports and unsets. The environment of the agent's own tool calls adds
  `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`, `CLAUDE_CODE_CHILD_SESSION`, the
  messaging variables and others, and gnome-terminal passes the caller's environment to
  its tabs. `systemctl --user show-environment` lacks every `~/.zshrc` export, among them
  `CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF`, which Claude Code reads only from the
  launch environment. `env-of-claude.sh` refuses a process that is not claude or that a
  tool call or a teammate launch started, and drops `TMUX` and `TMUX_PANE`.
- Open all the leads with one gnome-terminal call; a separate call opens a new window or
  puts its tab in whichever window was last active. Run it in the background and record
  its PID, because the client can stay alive after it has created every tab:

  ```bash
  busctl --user tree org.gnome.Terminal | grep -o 'window/[0-9]*'   # windows before
  ~/.claude/skills/crash-resume/scripts/env-of-claude.sh "$CLAUDE_PID" gnome-terminal --window \
    --working-directory=<launch directory> --title='<title>' \
      -e "/home/<user>/.claude/skills/crash-resume/scripts/restore-tab.sh session-<team> proxy --model <model> --effort <effort> --resume <uuid>" \
    --tab --working-directory=<launch directory> --title='<title>' \
      -e "/home/<user>/.claude/skills/crash-resume/scripts/restore-tab.sh session-<team> direct --model <model> --resume <uuid>" &
  echo "client pid $!"
  ```

  Inside an `-e` string, write absolute paths: gnome-terminal splits the string into
  words without expanding `~` or variables. Put the team name in `restore-tab.sh`'s
  argument: an `-e` string containing `VAR=value` makes GNOME Terminal 3.52 refuse the
  whole call with "Can only use --wait once".
- When `busctl` shows the new window, check whether the client still runs. `/usr/bin/gnome-terminal`
  is a Python wrapper around `gnome-terminal.real`, so look at `pgrep -P <client pid>`
  and `ps -o pid,args`, and stop a client that stays by its PID. The tabs belong to
  gnome-terminal-server and stay open.
- `restore-tab.sh <team> <proxy|direct> <claude args>` sets the team variable, sources
  `~/.local/bin/proxy-codex.sh` for `proxy`, and refuses to start claude when the proxy
  does not come up or when `direct` was asked for and `ANTHROPIC_BASE_URL` came with the
  environment. It records the launch in `~/.local/state/crash-resume/launches.log`, and
  when claude exits it unsets the team variable and prints the command to run it again.
- Check each lead's `/proc/<pid>/environ`: the team variable, `ANTHROPIC_BASE_URL` for
  proxy leads and its absence for direct ones, the `~/.zshrc` exports, and the absence of
  `CLAUDECODE`, `CLAUDE_PID` and `CLAUDE_CODE_SESSION_ID`.

## 4. Get each lead reading its team's mail

A plain lead reads its team's mail only after it has spawned a teammate in the current
process. Ask the user to have each lead with teammates spawn a teammate once, for example
a no-op Haiku teammate, before the teammates are resumed: the spawn also records the team
the lead runs as, which agent-resume uses when it resumes the teammates.

Then `agent-resume --doctor session-<team>` reports UNKNOWABLE, with the lead line "has
written transcript records stamped with this team, or spawned a teammate into it, since it
started". That is the best result a plain lead gets: the doctor reports CONNECTED only for
a lead with its team on its command line. Before the spawn it reports SEVERED (members
running or mail waiting) or DEGRADED. Its remedy list includes resuming the lead under the
team-lead identity; leave that remedy out.

## 5. Resume the teammates

- Resume one teammate at a time, with its lead's environment:

  ```bash
  ~/.claude/skills/crash-resume/scripts/env-of-claude.sh <lead pid> agent-resume <name>@session-<team> --no-attach --dry-run
  ~/.claude/skills/crash-resume/scripts/env-of-claude.sh <lead pid> agent-resume <name>@session-<team> --no-attach [--model <m>] [--effort <e>]
  ```

  agent-resume picks the model from its own sources (the roster, the spawn record, the
  transcript) and can leave out the effort. Compare the dry run's command with the
  inventory's model and effort, and pass `--model` or `--effort` where they differ; an
  explicit flag replaces the derived one.
- agent-resume starts the tmux server `claude-swarm-<lead pid>` with the environment it
  runs in, and every pane in that server inherits it, so the first teammate on a server
  decides the environment of all of them. When a swarm survived, resume teammates into it
  only after checking its environment (`tmux -L <socket> show-environment -g`).
- Resume by agent id rather than with `--team`: `--team` resumes the members and then the
  lead under the team-lead identity unless `--no-lead` is given, and after a restart, when
  `config.json` lists only the lead, it needs `--all`, which also brings back every former
  member.
- Add a tab attached to each swarm to the leads' window:

  ```bash
  ~/.claude/skills/crash-resume/scripts/env-of-claude.sh "$CLAUDE_PID" python3 -I \
    ~/.claude/skills/crash-resume/scripts/tab-in-window.py <window> /tmp tmux -L claude-swarm-<lead pid> attach
  ```

## Restarting a tmux swarm with a different environment

A swarm server keeps the environment it was started with, and every teammate added to it
later gets that environment too. To restart it with the lead's environment and keep its
agents and layout:

1. Record the layout and the agent in each pane:
   `tmux -L <socket> list-windows -F '#{window_layout}'` and
   `tmux -L <socket> list-panes -F '#{pane_index} #{pane_id} #{pane_pid}'`, with the
   agent id and uuid from each pane process's command line. Take each agent's current
   model and effort from `crash-inventory.py --sessions <uuid>...`.
2. Check that every agent is idle: `tmux -L <socket> capture-pane -p -t <pane>` does not
   show "esc to interrupt", its transcript's last turn has ended, and its inbox under
   `~/.claude/teams/<team>/inboxes/` holds only read messages. Wait or ask when one is busy.
3. Stop each agent with `agent-resume <name>@<team> --stop -y`. The server exits with its
   last pane, and its attach tab closes.
4. Resume the agents as in step 5, in the recorded pane order, with their current models
   and effort, and add the attach tab again.
5. Put the panes back in order with `tmux -L <socket> swap-pane -d -s <pane> -t <pane>`,
   and once the attach tab's client is attached, apply the layout with
   `tmux -L <socket> select-layout -t claude-swarm:0 '<layout>'`. The window takes the
   client's size, and the layout string carries the size it was recorded at.

## Rules

- Before stopping or restarting a session or teammate, check that it is idle (its status
  in `~/.claude/sessions/` where it has an entry, the end of its transcript, its pane, its
  unread mail), in the same command as the stop.
- Ask before stopping or restarting anything the user did not ask to restart.
- A PreToolUse hook runs Claude Code's dangerous-removal check on Bash commands, and it
  refuses a command it cannot parse with "This command could not be checked for dangerous
  rm operations". `zsh -c '...'` strings trigger it; put such commands in a script file
  and run that.
