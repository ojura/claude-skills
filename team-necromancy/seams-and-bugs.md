# Seams, bugs, and what upstream should do about them

Companion to SKILL.md and architecture.md. This is the ledger of places where
the teammate machinery's parts disagree with each other, each one observed
live and then confirmed in the 2.1.200 binary. The unifying diagnosis: the
system's parts are better than its plumbing. Almost every entry below is two
good components that nobody introduced to each other.

## The seams (divergences you will hit)

1. **Roster file vs leader memory.** The leader writes the team file but
   renders from an in-memory projection updated only by its own actions and
   by death announcements in the mail. Nothing reconciles memory against the
   file, ever. Symptoms: color chips that lag reality, dead teammates shown
   "active", counters that tick for idle or dead processes. Fix either by
   restarting the leader (memory re-snapshots from disk) or by not caring;
   actions resolve from the file, so behavior stays correct while pixels lie.

2. **Death is only real if announced.** Teammates are children of the tmux
   server or the daemon, not of the leader, so the leader cannot wait on
   them. Graceful shutdowns announce themselves through the mailbox; a
   Ctrl+C, a `respawn-pane -k`, or a crash announces nothing. This is what
   makes the spawn-then-swap resurrection invisible to the harness, and what
   makes the agents view unreliable as a liveness monitor.

3. **Turn state does not exist for pane teammates.** A teammate's plain text
   output goes nowhere; only SendMessage and the Stop hook's idle
   notification reach the leader, and those surface at the leader's next turn
   boundary. The footer's teammate-idle machinery only reads in-process
   tasks. So pane teammates look busy forever unless they explicitly report.

4. **Two input paths, one broken.** `@name` from the leader prompt routes via
   the mailbox and works for every substrate. Typing into the opened teammate
   viewer pushes into `pendingUserMessages` in leader memory, which only the
   in-process runner drains. For a tmux teammate that queue has no consumer:
   the message shows "1 queued" forever and is silently stranded.

5. **Team names come from the internal session id**, which changes on every
   resume and does not match the transcript filename. Anything that hardcodes
   or predicts a team name breaks after the next resume. Discover names post
   hoc, or set them by flag via the masquerade.

6. **tmux swallows zshrc.** A tmux pane given an explicit command string runs
   it without a login shell, so exported env like
   `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` vanishes unless passed inline.
   The harness knows (it embeds env into every spawn command); hand-rolled
   spawns must copy that habit.

7. **Ghost text.** The prompt's dim autosuggestion renders as typed input in
   `capture-pane -p`. Check for `\x1b[2m` with `-e` before believing that a
   human (or anyone) typed something. Bitten twice in one night.

8. **Mint is not adoption.** A boot with no `--agent-id` mints
   `session-<sessionId[:8]>` and writes its config; at first render the
   harness reads the loaded window's first entry, and when it carries both
   `agentName` and `teamName` stamps it overwrites the team context from
   them: stamped team, roster self-match by name, an empty in-RAM teammate
   map, no idle hook for a restored lead. Everything operative follows the
   restored context, and every new line re-stamps it, so a flagless resume of
   a stamped transcript runs as the stamped team forever, not the minted one.
   The mint record is a mint event that adoption then discards. Anything that
   answers "runs as" from the session id or the mint ledger overclaims for
   resumed transcripts; the window-start stamps are the operand the harness
   actually consumes.

   Settled, triple-witnessed (stale source, bundle trace, live sandbox):
   restore is not gated on flags. A flag-bound launch onto a
   stamped-disagreeing transcript runs split: it writes transcript lines as
   the stamped team, sends idle notifications as the worn team, and polls
   both teams' inboxes. The transcript half wins every future resume, so a
   rebind by flags silently undoes itself at each restart; the durable
   mechanism is an appended re-stamped boundary entry (pseudocompact shape),
   which changes exactly the entry restore consumes. Two reaped-team edges:
   stamped for an absent team and flagless, the session comes up as nobody
   and writes unstamped lines until the team dir exists again; flag-bound,
   argv survives but hook init is skipped (that half is code-read grade).

9. **Delivery is a turn-boundary event.** Teammate mail is three-tier by
   design: an idle recipient gets an immediate turn; a busy one queues in
   AppState and drains when its turn ends; a conservative mid-turn
   attachment lane exists but rarely triggers (useInboxPoller.ts:118-125,
   :860-864). The weight is deliberate: a message is a full conversational
   turn (user-role cannot interleave into an agentic loop), ordering is
   crash-durable at-least-once (mark-read only after delivery), and the
   mid-turn lane once raced the poller so protocol messages were eaten as
   raw context (attachments.ts race comment) - it stayed conservative ever
   since. Consequences: mail to a long-running turn queues for minutes and
   arrives batched, absence of a reply is not silence, and anything that
   reports reachability must say "at its next turn boundary" rather than
   implying immediacy. The upstream wish - mail that behaves like a user
   correction - is a trigger-frequency change in the existing lane, not
   architecture.

## The bugs (file-worthy)

1. **Teammate viewer accepts input it cannot deliver** (seam 4). Either route
   the viewer's input through the mailbox like @-mentions, or disable the box
   for pane-backed teammates.

2. **The hardcoded pane tint (resolved: keep it).** Teammate spawn
   unconditionally runs `set-option -p window-style "bg=default,fg=<color>"`
   on the new pane, so the pane's default foreground, including tmux's own
   chrome inside it, renders in the agent color. Early on this was barely
   readable (dark palette, ANSI blue); that is fixed, tinted panes now look
   good, and the tint is worth preserving as the at-a-glance agent identity.
   The remaining upstream ask is a setting to configure or disable it, not
   removal. Hand-rolled panes (Recipe 1b) can apply the same tint with the
   same set-option command.

3. **Bg sessions do not flush their transcripts.** A `--bg` session resumed
   from a transcript appends nothing back to disk; its lives leave no
   memories, and resuming its continuation id later crashes the job because
   the file was never written. This is the single biggest caveat against the
   otherwise superior bg substrate.

4. **The fake king cannot disband the kingdom.** Cleanup-at-exit is
   registered only by the session-startup implicit-team initializer
   (`initializeSessionTeam`), whose `!agentId` guard excludes every
   flag-bound launch, so a session that acquired its team through identity
   flags (the masquerade) exits without reaping or deleting anything. A
   consistency bug upstream; the load-bearing feature of the team-resume
   recipe here.

5. **`claude <word>` dispatches.** A stray positional that is not a known
   verb quietly spawns a new bg job with your text as its prompt. Fat-finger
   a verb name and you have created an agent. Cost us three stray sessions
   in one night.

6. **The /resume `bg` badge reads as a session type.** It is the first
   line's `sessionKind`, frozen at birth; the session behaves per its live
   env. Rendering the literal string `HEAD` as a git branch label in the
   same slot invites the same misreading.

6. **A subagent's teammate is spawned into a team that is never instantiated,
   and its mail reports success into it.** An in-process subagent calling the
   Agent tool gets back an agent id naming a fresh team, but a subagent cannot
   host a team server, so nothing is created for it: no team directory, no
   config, no session file for the lead the id names. The teammate runs, works,
   and reports; every `SendMessage` returns `{"success": true, "message":
   "Message sent to team-lead's inbox"}` into a queue nobody polls, and from
   inside there is no way to tell.

   Observed with a teammate stamped `teamName: session-2b76dc77`: no
   `~/.claude/teams/session-2b76dc77/`, no transcript beginning `2b76dc77`,
   `~/.claude/tasks/session-2b76dc77` empty, and two successful sends in its
   transcript. Its report was recovered only by reading the transcript directly.

   Two directories *were* created at the two spawn times, named after neither
   the agent's stamped team nor each other, each holding only a `team-lead`
   member whose `leadSessionId` has no transcript. So the path that mints the
   agent id and the path that writes the roster disagreed on the team name, and
   neither produced a live lead.

   Either refuse the spawn where it cannot be hosted, or report the delivery
   honestly. Success returned for a message with no recipient is worse than a
   failure, because it ends the sender's attempts to be heard.

## Feature requests (in order of leverage)

1. **`teammateMode: bg`.** The daemon substrate already gives supervision,
   auto-respawn, truecolor, exit survival, attach/logs/stop, and resume built
   into the spawn verb. Wiring it into the backend registry plus roster and
   shutdown handling is a small patch; fixing transcript flushing (bug 3)
   makes it strictly better than the tmux backend.

2. **Teams in adopt.** Adopt already recovers shells, agents, workflows and
   cron across process death. Teammates are the one omission. Stamping leader
   transcripts with team identity (they are currently unstamped, which is why
   leaders cannot be resumed as leaders) plus an adopt entry for members
   would turn this whole skill into a product feature.

3. **Reconcile or watch the roster file** (seam 1). The file is tiny, the
   harness already watches inbox files, and six code paths already read it.
   Rendering is the only consumer that never looks.

4. **A real `prompt <id>` verb** for driving bg sessions non-interactively.
   Today the only inputs are attach (interactive) and the team inbox, and the
   obvious spelling silently dispatches (bug 5).

5. **A tint setting.** The pane tint is good identity signal (bug 2, now
   resolved); expose the window-style color, or at least an off switch, as
   configuration instead of a hardcoded constant.
