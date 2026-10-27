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

- A claude process appends `cost-state` records to its transcript
  (`~/.claude/projects/<project>/<uuid>.jsonl`) when it exits normally, and also when a
  resume starts. After a crash that ends processes through their exit path, such as a
  GNOME Shell crash, the transcripts last modified around the crash time belong to the
  processes that were running. A freeze or a failed suspend ends them without that
  record, and a transcript can end in NUL bytes: writes that did not reach the disk.
  Leave transcripts as they are.
- A transcript's modification time is not the time of its last record: a live lead's
  transcript is touched every hour without new records.
- What survives depends on the crash. When gnome-terminal-server crashes, the tabs end
  and the tmux swarm servers keep running, because they are children of the systemd user
  manager. When GNOME Shell crashed and the login session ended, the swarm panes ended
  together with the tabs.
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

Run the inventory before resuming anything: a session resumed since the crash has a new
modification time and drops out of the time-based selections.

1. Find the crash time:
   `journalctl -o short-iso --since '<day>' | grep -iE 'gnome-shell.*crashed|gnome-terminal-server.service: Main process exited|systemd-oomd.*Killed|core-dump|Removed session'`.
   For a freeze or a failed suspend, `journalctl --list-boots` gives the index of the boot
   that ended in it, and `journalctl -b <index> -n 20` shows how it ended.
2. Run the inventory. It only reads.
   - After a crash with a time:
     `~/.claude/skills/crash-resume/scripts/crash-inventory.py --at "YYYY-MM-DD HH:MM:SS"`
     (local time) lists the transcripts last modified from a minute before to three
     minutes after it, leaving out sessions that started after it.
   - After a freeze or a failed suspend:
     `~/.claude/skills/crash-resume/scripts/crash-inventory.py --boot <index>` lists every
     transcript modified during that boot. Those that end without an exit record were
     cut off by the freeze. Those that end in `cost-state` either exited or were resumed
     and stayed idle until the freeze. To tell them apart, look in the journal of that
     boot for terminal tabs and tmux panes that started and never stopped:
     `journalctl --user -b <index> -o short-iso | grep -E 'Started (vte|tmux)-spawn'`,
     compared with the scopes that report "Consumed"; a pane that never stopped and that
     started within a second of a transcript's modification time belonged to it.
   - `~/.claude/skills/crash-resume/scripts/crash-inventory.py --sessions <uuid or prefix>...`
     inspects given sessions, including ones already resumed.

   For each session it prints whether a process runs it now (RUNNING NOW, from the
   registry and from `--resume <uuid>` or `--agent-id <name>@<team>` in process command
   lines), lead or plain session versus teammate (a teammate's first message is a
   `<teammate-message>`), how its transcript ends, entrypoint, launch directory, current
   model and effort, reply and API-error counts, the time of the latest process's first
   record, the shell history lines and `restore-tab.sh` launch-log lines that resume it,
   and for a lead the evidence for its team name.
3. List the tmux swarm servers that survived: `ls /tmp/tmux-$UID/ | grep claude-swarm`,
   then `tmux -L <socket> list-panes -a -F '#{pane_id} #{pane_pid}'` for each, and the
   agent of each pane from `--agent-id` or `--resume` in `/proc/<pane_pid>/cmdline`. Leave
   survivors running: the restored lead takes their team name, and the attach tab goes
   to the surviving server.
4. Agree with the user on which sessions come back. Sessions the user had closed stay
   closed. Ask about sessions with an entrypoint other than `cli` (`sdk-cli` is
   `claude -p`), teammates that replied only with API errors, and no-op teammates that a
   lead spawned only to start reading its mail.

## 2. Work out how each lead was launched

- **Team name.** The lead has to run as the team name its teammates carry in their
  `teamName` field; for a lead with teammates that is the strongest evidence. To find it:
  1. The `CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME` value of the last launch. The inventory
     prints the history lines that resume the session by uuid or by title, and before
     each one the `export`, `unset` and `source` lines for that variable and the proxy
     that came before it, possibly in the same shell, and the `restore-tab.sh` launch log.
     A resume chosen in the picker does not name the session.
  2. A session that was never resumed runs as `session-<first 8 hex digits of its uuid>`.
     The inventory shows whether that task list exists.
  3. A resumed session started without the variable gets a new random team name in each
     process. Take the task list that the inventory matches to the session's task calls
     and task reminders, and the teams it spawned into. Both cover every process of the
     transcript; the inventory prints the latest spawn per team, the latest match per
     task list, and the latest process's first record, and the team in use at the crash
     is the one with activity after that first record.
- **Model and effort**: the inventory's "model now" and "effort". The model comes from
  the latest of the last reply, a `model` attachment (which keeps variants such as
  `[1m]` when the reply names the same model without it), and a confirmed `/model`
  command. When the inventory says the model came from the picker, ask. When the effort
  is "none recorded" or `auto`, leave out `--effort`.
- **Launch directory**: the inventory's launch directory, the first working directory in
  the transcript.
- **Other flags**: from the launch command in the history.
- **Proxy**: sessions on GPT models run through CLIProxyAPI. The `source proxy-codex.sh`
  lines before a launch show whether its shell had the proxy; for a Claude model that ran
  through the proxy, ask.

## 3. Start the leads

- Start every lead as a plain lead. Do not give it the teammate identity that agent-resume
  builds when it resumes a lead (`--agent-id team-lead@...`, `--agent-name`,
  `--team-name`, `--parent-session-id`).
- Start it with the launch environment of a claude process that the user started from a
  shell, through `env-of-claude.sh <pid>`: the user's shell environment, with
  `~/.zshrc`'s exports and unsets. Use `$CLAUDE_PID`, the restoring agent's own process,
  unless that process runs through the proxy; then take the PID of a claude process
  started without it, because `restore-tab.sh` refuses `direct` tabs that inherit
  `ANTHROPIC_BASE_URL`. The environment of the agent's own tool calls is wrong: it adds
  `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`, `CLAUDE_CODE_CHILD_SESSION`, the
  messaging variables and others, and gnome-terminal passes the caller's environment to
  its tabs. `systemctl --user show-environment` is wrong too: it lacks every `~/.zshrc`
  export, among them `CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF`, which Claude Code
  reads only from the launch environment. `env-of-claude.sh` refuses a process that is
  not claude or that a tool call or a teammate launch started, and drops `TMUX` and
  `TMUX_PANE`.
- Open all the leads with one gnome-terminal call; a separate call opens a new window or
  puts its tab in whichever window was last active. Run it with the Bash tool's
  background option, because the client can stay alive after it has created every tab:

  ```bash
  busctl --user tree org.gnome.Terminal | grep -o 'window/[0-9]*'   # windows before, in its own call
  ~/.claude/skills/crash-resume/scripts/env-of-claude.sh "$CLAUDE_PID" gnome-terminal --window \
    --working-directory=<launch directory> --title="<title>" \
      -e "/home/<user>/.claude/skills/crash-resume/scripts/restore-tab.sh session-<team> proxy --model <model> --effort <effort> --resume <uuid>" \
    --tab --working-directory=<launch directory> --title="<title>" \
      -e "/home/<user>/.claude/skills/crash-resume/scripts/restore-tab.sh session-<team> direct --model <model> --resume <uuid>"
  ```

  Inside an `-e` string, write absolute paths: gnome-terminal splits the string into
  words without expanding `~` or variables. Put the team name in `restore-tab.sh`'s
  argument: an `-e` string containing `VAR=value` makes GNOME Terminal 3.52 refuse the
  whole call with "Can only use --wait once". A title containing `"` needs escaping.
- When `busctl` shows the new window, check for a client that stays:
  `pgrep -f '^/usr/bin/gnome-terminal.real'`, then `ps -o pid,args -p <pid>`, and stop
  it by that PID. `/usr/bin/gnome-terminal` is a Python wrapper around
  `gnome-terminal.real` and exits with it. The tabs belong to gnome-terminal-server and
  stay open.
- `restore-tab.sh <team> <proxy|direct> <claude args>` sets the team variable, sources
  `~/.local/bin/proxy-codex.sh` for `proxy`, and refuses to start claude when the proxy
  does not come up or when `direct` was asked for and `ANTHROPIC_BASE_URL` came with the
  environment. It records the launch in `~/.local/state/crash-resume/launches.log`, and
  when claude exits it unsets the team variable, prints the command to run it again and
  leaves an interactive shell in the tab.
- Check each lead's `/proc/<pid>/environ`: the team variable, `ANTHROPIC_BASE_URL` for
  proxy leads and its absence for direct ones, the `~/.zshrc` exports, and the absence of
  `CLAUDECODE`, `CLAUDE_PID` and `CLAUDE_CODE_SESSION_ID`.

## 4. Get each lead reading its team's mail

A plain lead reads its team's mail only after it has spawned a teammate in the current
process. Ask the user to have each lead with teammates spawn a teammate once, for example
a no-op Haiku teammate, before the teammates are resumed: the spawn also records the team
the lead runs as, which agent-resume uses when it resumes the teammates.

Then check `agent-resume --doctor session-<team>`: its "runs as" line should name the
team, and the verdict should be UNKNOWABLE. That is the best result a plain lead gets,
because the doctor reports CONNECTED only for a lead with its team on its command line
(recorded as a defect in team-necromancy's TODO.md). Before the spawn the verdict is
SEVERED. Two of the doctor's remedies conflict with running leads as plain leads; leave
both out: resuming the lead under the team-lead identity (worded as "resume the lead
under its own identity" or as `agent-resume team-lead@<team>`), and the whole-team run
`agent-resume session-<team>`, which brings the lead back under that identity too.

## 5. Resume the teammates

- Resume one teammate at a time, with its lead's environment:

  ```bash
  ~/.claude/skills/crash-resume/scripts/env-of-claude.sh <lead pid> agent-resume <name>@session-<team> --no-attach --dry-run
  ~/.claude/skills/crash-resume/scripts/env-of-claude.sh <lead pid> agent-resume <name>@session-<team> --no-attach [--model <m>] [--effort <e>]
  ```

  agent-resume picks the model from its own sources (the roster, the spawn record, the
  transcript) and can leave out the effort. Compare the dry run's command with the
  inventory's model and effort, and pass `--model` or `--effort` where they differ; an
  explicit flag replaces the derived one. The dry run's `socket` line names the tmux
  server the teammate will run in.
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
   model and effort from
   `~/.claude/skills/crash-resume/scripts/crash-inventory.py --sessions <uuid>...`.
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
- Claude Code's dangerous-removal check refuses a Bash command it cannot parse, with "This
  command could not be checked for dangerous rm operations". `zsh -c '...'` strings and
  zsh parameter flags such as `${(j:,:)name}` trigger it; put such commands in a script
  file and run that.
