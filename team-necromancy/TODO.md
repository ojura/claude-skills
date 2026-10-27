# agent-resume: open work

## The doctor cannot report CONNECTED for a plain lead

A plain lead runs as `claude --resume <uuid>` and gets its team name from
`CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME` in its launch environment, without team
flags on its command line. Leads are run this way on purpose: the user does not
use the `team-lead@<team>` identity that agent-resume builds for a lead.

`--doctor` handles such a lead wrongly in three ways. The verdict code is in
`agent-resume`, in the block that starts with `reads = bool(` (around line 9148):

1. After the lead has spawned a teammate in its current process, it reads its
   team's mail, and the doctor's own lead line says so ("has written transcript
   records stamped with this team, or spawned a teammate into it, since it
   started"). The verdict is still UNKNOWABLE, because the branch before
   CONNECTED tests `lead["argv_team"]`, and a plain lead never has one. So a
   correctly restored team with a plain lead stays at UNKNOWABLE.
2. In that state the first remedy is still "start any teammate from inside the
   lead", although the lead line reports that it already did.
3. The second remedy is `agent-resume team-lead@<team>`, "restarts the lead
   with team flags". That is the identity the user does not use for leads, and
   an agent working toward CONNECTED is likely to take it.

Before the spawn, SEVERED (members running or mail waiting) and DEGRADED are
correct: a plain lead does not read its team's mail until it has spawned a
teammate in the current process.

Done when, with tests that `probe.expect(...)` confirms bite:

- the Resolver takes a live lead's team from `CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME`
  in `/proc/<pid>/environ` as evidence of the team it runs as, next to its argv;
- a plain lead that has spawned a teammate into the team in its current process
  gets CONNECTED, and one that has not gets the spawn as its remedy;
- the doctor's remedies for a lead without team flags on its command line do not
  include restarting it under the team-lead identity.
