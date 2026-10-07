"""Mutation probes for task 15 Resolver ownership and generation checks.

Run this file as Python after the task-15 clean selftest passes. ``probe.expect``
runs a clean suite and one isolated mutant for each case, reads every report in
full, and refuses a mutant whose failure set is not exactly the set its case
names: the named check, plus any co-failure the case justifies with a reason.
It also refuses a run during which the tool, its companion, the verifier or the
test tree changed. This module has no probe command-line interface.
"""

import probe


# These generation boundaries have no category-specific registered verdict that
# can distinguish the requested race from ordinary behavior. Keep the exact
# removal sites visible so later coverage does not substitute a weaker check for
# the generation it is supposed to protect.
BLOCKED_GENERATION_CASES = (
    (
        "team-run runtime generation",
        'if process_generation != plan["process_generation"]:',
        "a named team-run generation verdict is not registered",
    ),
    (
        "team-run launch-placement generation",
        'if current_placement != plan["placement_generation"]:',
        "a named team-run placement generation verdict is not registered",
    ),
    (
        "PID start and argv revalidation",
        "selected_signature = self._process_fact_signature(selected)",
        "a named stop-generation verdict is not registered",
    ),
    (
        "rebind and migration generations",
        "revalidate_rebind",
        "no named rebind or lead-migration generation verdict is registered",
    ),
)

# Some of these final owners now exist, but none has an exact registered verdict
# for removal or bypass of the final call or revalidation. A later script must
# name that verdict; it must not replace one of these omissions with a nearby
# behavior check or weaker structural mutation.
BLOCKED_FINAL_PLAN_CASES = (
    ("rebuild final revalidation", "rebuild_plan"),
    ("membership final revalidation", "membership_plan"),
)


# These are complete mutation candidates, deliberately kept out of
# ``probe.expect`` until API-tests provides an exact verdict for each stated
# ownership or generation rule. The last field says why a nearby behavior check
# is not a valid substitute.
PENDING_TEAM_MEMBERSHIP_ATTACH_CASES = ()


# The request-family routing methods have approved callers, but API-tests has
# not yet registered fixtures for these planned verdicts. Each tuple is
# (planned verdict, phase, old text, mutant text, status), never a probe case.
PENDING_MEMBER_ROUTING_CASES = ()


CASES = [
    # Resolver public-question allowlist and all three ways code can bypass it.
    (
        "resolver-public-api-is-explicit",
        '    "selector_answer": ("apply_selector",),\n',
        '    "selector_answer_removed": ("apply_selector",),\n',
    ),
    (
        "resolver-public-api-is-explicit",
        "    # Declared here rather than assigned in __init__, because declaring one is\n",
        "    def agent_diagnosis(self, *_args, **_kwargs):\n"
        "        return None\n\n"
        "    # Declared here rather than assigned in __init__, because declaring one is\n",
    ),
    (
        "resolver-question-callers-are-exact",
        "    plan = RESOLVER.stop_plan(\n",
        "    if False:\n"
        "        RESOLVER.runtime()\n"
        "    plan = RESOLVER.stop_plan(\n",
    ),
    (
        "resolver-components-stay-private",
        "    plan = RESOLVER.stop_plan(\n",
        "    engine = RESOLVER\n"
        "    plan = engine.stop_plan(\n",
    ),
    (
        "resolver-components-stay-private",
        "    plan = RESOLVER.stop_plan(\n",
        "    if False:\n"
        "        RESOLVER.rewriter.conversion_plan\n"
        "    plan = RESOLVER.stop_plan(\n",
    ),
    (
        "resolver-components-stay-private",
        "def run_agents(args, passthrough, table, jobs, socket=None, prefix=\"\", terminal=None):\n",
        "def run_agents(args, passthrough, table, jobs, socket=None, prefix=\"\", terminal=None):\n"
        "    if False:\n"
        "        RESOLVER._terminal_context()\n",
    ),
    (
        "resolver-components-stay-private",
        "def launch(plan, cwd, command, dry, replace_pane=None):\n",
        "def launch(plan, cwd, command, dry, replace_pane=None):\n"
        "    if False:\n"
        "        RESOLVER._tmux_launch_action(None)\n",
    ),
    # Completion consumes one already-rendered answer. Each mutation below
    # targets a distinct condition of completion-consumes-one-complete-answer.
    (
        "completion-consumes-one-complete-answer",
        "    answer = RESOLVER.completion_answer(topic)\n",
        "    answer = RESOLVER.completion_answer(topic)\n"
        "    if False:\n"
        "        RESOLVER.process_table()\n",
    ),
    (
        "completion-consumes-one-complete-answer",
        "    answer = RESOLVER.completion_answer(topic)\n",
        "    answer = RESOLVER.completion_answer(topic)\n"
        "    if False:\n"
        "        RESOLVER.teams_dir\n",
    ),
    (
        "completion-consumes-one-complete-answer",
        "    answer = RESOLVER.completion_answer(topic)\n",
        "    answer = RESOLVER.completion_answer(topic)\n"
        "    if False:\n"
        "        Path(\".\").read_text()\n",
    ),
    (
        "completion-consumes-one-complete-answer",
        "    answer = RESOLVER.completion_answer(topic)\n",
        "    answer = RESOLVER.completion_answer(topic)\n"
        "    if False:\n"
        "        render_team_line()\n",
    ),
    (
        "completion-consumes-one-complete-answer",
        "    answer = RESOLVER.completion_answer(topic)\n",
        "    answer = RESOLVER.completion_answer(topic)\n"
        "    if False:\n"
        "        ignored = f\"{team}\"\n",
    ),

    # A supplied process snapshot is one answer, and runtime indexes retain the
    # exact record object rather than independently copied values.
    (
        "runtime-reuses-supplied-snapshot",
        "        table = self._process_table() if table is None else table\n"
        "        snapshot = self._runtime(table)\n",
        "        table = self._process_table()\n"
        "        snapshot = self._runtime(table)\n",
    ),
    (
        "runtime-consumers-do-not-reread",
        "        table = self._process_table() if table is None else table\n"
        "        snapshot = self._runtime(table)\n",
        "        table = self._process_table() if table is None else table\n"
        "        if False:\n"
        "            self.runtime()\n"
        "        snapshot = self._runtime(table)\n",
    ),
    (
        "agent-runtime-one-question",
        "        table = self._process_table() if table is None else table\n"
        "        snapshot = self._runtime(table)\n",
        "        table = self._process_table() if table is None else table\n"
        "        if False:\n"
        "            self._argv_pairs(table)\n"
        "        snapshot = self._runtime(table)\n",
    ),
    (
        "runtime-indexes-share-record-objects",
        "            if record[\"agent_id\"]:\n"
        "                by_agent[record[\"agent_id\"]].append(record)\n",
        "            if record[\"agent_id\"]:\n"
        "                by_agent[record[\"agent_id\"]].append(\n"
        "                    types.MappingProxyType(dict(record)))\n",
    ),

    # Seat and argv form one retained process fact. The seat mutant preserves a
    # real pane while assigning it to a different server; the argv mutant keeps
    # parsed identity but drops the exact command carried by the process record.
    (
        "live-seat-walks-pane-children",
        '                seat = Seat(socket, pane["pane"])\n',
        '                seat = Seat("claude-swarm-decoy", pane["pane"])\n',
    ),
    (
        "live-seat-walks-pane-children",
        '                           "argv": argv, "args": shlex.join(argv),\n',
        '                           "argv": (), "args": shlex.join(argv),\n',
    ),
    (
        "claude-pattern-engine-only",
        "def running_agents(plan):\n",
        "def running_agents(plan):\n"
        "    if False:\n"
        "        CLAUDE_PROC.search(\"claude\")\n",
    ),

    # A selected title retains both its complete text and its source event.
    (
        "title-skips-the-harness",
        "                return TitleFact(value, kind, path, timestamp, record_uuid)\n",
        "                return TitleFact(value.split()[0], kind, path, timestamp, record_uuid)\n",
    ),
    (
        "title-skips-the-harness",
        "                return TitleFact(value, kind, path, timestamp, record_uuid)\n",
        "                return TitleFact(value, kind, path, None, None)\n",
    ),
    (
        "title-one-transcript-fact",
        "def title_value(title):\n",
        "def transcript_title(path):\n"
        "    return None\n\n\n"
        "def title_value(title):\n",
    ),

    # Consumers use the assembled identity and selected parent; they do not
    # reconstruct either from a removed merge or a second Resolver query.
    (
        "identity-consumers-do-not-read-the-merge",
        "def build_command(ctx, args, passthrough):\n",
        "def build_command(ctx, args, passthrough):\n"
        "    if False:\n"
        "        ctx[\"merged\"]\n",
    ),
    (
        "resume-target-has-no-parallel-projections",
        "def build_command(ctx, args, passthrough):\n",
        "def build_command(ctx, args, passthrough):\n"
        "    if False:\n"
        "        RESOLVER.transcript_path(ctx[\"session\"])\n",
    ),
    (
        "demote-planner-consumes-parent-resolution",
        "def run_demote(args):\n",
        "def run_demote(args):\n"
        "    if False:\n"
        "        RESOLVER.demotion_parent(None, None, None)\n",
    ),

    # Reporters receive facts. They neither query Resolver nor grow a second
    # member or placement model in an executor.
    (
        "doctor-renderers-read-no-evidence",
        "def run_agent_dossier(member, shared=None):\n",
        "def run_agent_dossier(member, shared=None):\n"
        "    if False:\n"
        "        RESOLVER.team_diagnosis(\"unused\")\n",
    ),
    (
        "doctor-has-one-member-model",
        "        population = types.MappingProxyType({\n",
        "        if False:\n"
        "            hidden = {\"names_by_membership\": {}}\n"
        "        population = types.MappingProxyType({\n",
    ),
    (
        "team-sentence-one-owner",
        "    for line in answer[\"rows\"]:\n"
        "        print(line)\n",
        "    for line in answer[\"rows\"]:\n"
        "        print(f\"{line}\")\n",
    ),
    (
        "placement-one-function",
        "    supplied_census = getattr(args, \"roster_census\", None)\n",
        "    if False:\n"
        "        RESOLVER.live_teams()\n"
        "        RESOLVER.teams_led_by(\"unused\")\n"
        "    supplied_census = getattr(args, \"roster_census\", None)\n",
    ),
    (
        "conversion-writers-only",
        "def run_sides(selector=None):\n",
        "def run_sides(selector=None):\n"
        "    if False:\n"
        "        materialize({})\n",
    ),
    (
        "holder-kill-one-site",
        "    supplied_census = getattr(args, \"roster_census\", None)\n",
        "    if False:\n"
        "        subprocess.run([\"kill\", \"0\"])\n"
        "    supplied_census = getattr(args, \"roster_census\", None)\n",
    ),

    # Each selected file or directory generation must refuse the original action
    # after its selected object changes. Reap and inbox plans share a Resolver
    # method, so each mutation disables only the relevant plan kind.
    (
        "conversion-source-generation-revalidated",
        '        if source != plan["source_generation"]:\n',
        "        if False:\n",
    ),
    (
        "conversion-destination-generation-revalidated",
        "        if current_destination != expected_destination:\n",
        "        if False:\n",
    ),
    (
        "reap-generation-revalidated",
        '            "matches": current == expected,\n',
        '            "matches": current == expected or plan["kind"] == "team",\n',
    ),
    (
        "archive-inbox-generation-revalidated",
        '            "matches": current == expected,\n',
        '            "matches": current == expected or plan["kind"] == "inbox",\n',
    ),
    (
        "cleanup-generation-revalidated",
        '            if self._directory_generation(action["team_dir"]) != action["generation"]:\n',
        "            if False:\n",
    ),

    # The renderers each receive one complete immutable answer and make no
    # second Resolver query of the raw evidence they render.
    (
        "mail-consumes-one-complete-answer",
        "    answer = RESOLVER.mail_answer(selector)\n",
        "    answer = RESOLVER.mail_answer(selector)\n"
        "    if False:\n"
        "        RESOLVER.mail_ledger(\"unused\")\n",
    ),
    (
        "sides-consumes-one-complete-answer",
        "    answer = RESOLVER.sides_answer(selector)\n",
        "    answer = RESOLVER.sides_answer(selector)\n"
        "    if False:\n"
        "        RESOLVER.side_transcripts()\n",
    ),
    (
        "census-consumes-one-complete-answer",
        "    answer = RESOLVER.census_answer()\n",
        "    answer = RESOLVER.census_answer()\n"
        "    if False:\n"
        "        RESOLVER.rosters()\n",
    ),

    # Team and attachment executors consume completed Resolver answers. Inbox
    # initialization preserves the exact selected state and refuses drift.
    (
        "team-run-consumes-selected-plan",
        "def run_team(args, passthrough):\n",
        "def run_team(args, passthrough):\n"
        "    if False:\n"
        "        RESOLVER.roster_snapshot(args.team)\n",
    ),
    (
        "team-run-consumes-selected-plan",
        "    socket = destination.socket if destination else None\n",
        "    socket = destination.socket if destination else None\n"
        "    if False:\n"
        "        socket = RESOLVER.newest_live_socket()\n",
    ),
    (
        "team-run-consumes-selected-plan",
        "        seed_selected_inbox(inbox, dry=args.dry_run)\n",
        "        seed_selected_inbox(inbox, dry=args.dry_run)\n"
        "        if False:\n"
        "            RESOLVER.inbox_state(args.team)\n",
    ),
    (
        "membership-keeps-nonempty-inbox",
        '        action = "keep"\n',
        '        action = "replace-empty"\n',
    ),
    (
        "membership-replaces-selected-empty-inbox",
        '            action = "replace-empty"\n',
        '            action = "keep"\n',
    ),
    (
        "membership-creates-selected-absent-inbox",
        '            written = write_new_bytes(path, b"[]", 0o600)\n',
        '            written = write_new_bytes(path, b"[ ]", 0o600)\n',
    ),
    (
        "membership-refuses-inbox-generation-drift",
        # Two revalidations carry this block; the membership one is the block
        # followed by its return, the rebuild one by a process-table read.
        '            for inbox in plan["inboxes"]:\n'
        '                if self._file_generation(inbox["path"]) != inbox["generation"]:\n'
        '                    changed.append(f"inbox {inbox[\'name\']}")\n'
        "        return immutable({\n",
        '            for inbox in plan["inboxes"]:\n'
        "                if False:\n"
        '                    changed.append(f"inbox {inbox[\'name\']}")\n'
        "        return immutable({\n",
    ),
    (
        "attach-flow-consumes-one-decision",
        "    decision = RESOLVER.attach_decision(\n",
        "    if False:\n"
        "        RESOLVER.socket_state(socket)\n"
        "    decision = RESOLVER.attach_decision(\n",
    ),

    # Member redirect plans retain their semantic branch, effective parent,
    # generations, durable action, and selected placement across every phase.
    (
        "member-redirect-retains-selected-semantic-branch",
        '            "kind": request.kind,\n',
        '            "kind": "discarded",\n',
    ),
    (
        "member-migration-completion-revalidates-pre-and-post-write",
        "    if holders[\"others\"] and not args.dry_run:\n",
        "    if False:\n",
    ),
    (
        "member-redirect-reuses-and-revalidates-launch-placement",
        "        placement = self.revalidate_launch(plan[\"redirected_launch\"])\n",
        "        placement = immutable({\"changed\": ()})\n",
    ),

    # Lead migration retains the selected original launch and must revalidate
    # before holder clearance and again with after_clear=True.
    (
        "lead-migration-plan-reuses-selected-route-facts",
        '            "original_launch": launch,\n',
        '            "original_launch": types.MappingProxyType(dict(launch)),\n',
    ),
    (
        "lead-migration-revalidates-before-stop-and-after-clear",
        "        checked = RESOLVER.revalidate_lead_migration(\n"
        "            migration, after_clear=True)\n",
        "        checked = RESOLVER.revalidate_lead_migration(\n"
        "            migration, after_clear=False)\n",
    ),

    # Current member fixtures prove the selected parent, automatic route,
    # retained launch, and durable rebind result independently.
    (
        "member-redirect-explicit-revalidates-destination-parent",
        '            "changed": tuple(dict.fromkeys(changed)),\n'
        '            "confirmation": bool(confirmation),\n'
        '            "launch": plan["redirected_launch"],\n'
        '            "snapshot": snapshot,\n',
        '            "changed": tuple(\n'
        '                item for item in dict.fromkeys(changed)\n'
        '                if not item.startswith("lead transcript")),\n'
        '            "confirmation": bool(confirmation),\n'
        '            "launch": plan["redirected_launch"],\n'
        '            "snapshot": snapshot,\n',
    ),
    (
        "member-redirect-auto-consumes-one-selected-route",
        "    print(f\"{prefix}redirect the lead now runs as {plan['destination']}, not \"\n"
        "          f\"{plan['source']}; launching with that identity (--keep-team to stay)\")\n"
        "    return apply_member_migration(\n"
        "        plan, args, prefix=prefix, confirmation=confirmed)\n",
        "    print(f\"{prefix}redirect the lead now runs as {plan['destination']}, not \"\n"
        "          f\"{plan['source']}; launching with that identity (--keep-team to stay)\")\n"
        "    plan = immutable({**plan, \"confirmation\": \"altered\"})\n"
        "    return apply_member_migration(\n"
        "        plan, args, prefix=prefix, confirmation=confirmed)\n",
    ),
    (
        "member-redirect-auto-does-not-reselect-after-rebind",
        "    return apply_member_migration(\n"
        "        plan, args, prefix=prefix, confirmation=confirmed)\n",
        "    completed = apply_member_migration(\n"
        "        plan, args, prefix=prefix, confirmation=confirmed)\n"
        "    RESOLVER.member_redirect_plan(completed, plan[\"request\"])\n"
        "    return completed\n",
    ),
    (
        "member-redirect-revalidates-retained-generations",
        '            "launch": plan["redirected_launch"],\n'
        '            "snapshot": snapshot,\n',
        '            "launch": plan["launch"],\n'
        '            "snapshot": snapshot,\n',
    ),
    (
        "member-redirect-verifies-durable-rebind-result",
        '        nested = plan["rebind"]\n'
        '        current_transcript = self._file_generation(\n'
        '            plan["redirected_launch"]["transcript"]["path"])\n'
        '        if nested:\n'
        '            if applied.durable_action != nested["action"]:\n',
        '        nested = plan["rebind"]\n'
        '        current_transcript = self._file_generation(\n'
        '            plan["redirected_launch"]["transcript"]["path"])\n'
        '        if nested:\n'
        '            if False:\n',
    ),
    (
        "member-migration-completion-reuses-route-selection",
        "            launch = self._complete_routed_launch(\n"
        "                launch, census, table, snapshot, current_transcript,\n"
        "                {\"member_moving\": plan[\"destination\"]},\n"
        "                source_reaped=bool(nested and nested[\"action\"] == \"reap\"))\n"
        "            if launch[\"identity\"] != plan[\"completion_intent\"][\"identity\"]:\n",
        "            launch = self._complete_routed_launch(\n"
        "                launch, census, table, snapshot, current_transcript,\n"
        "                {\"member_moving\": plan[\"destination\"]},\n"
        "                source_reaped=bool(nested and nested[\"action\"] == \"reap\"))\n"
        "            launch = types.MappingProxyType({\n"
        "                **launch, \"destination\": copy.copy(launch[\"destination\"]),\n"
        "            })\n"
        "            if launch[\"identity\"] != plan[\"completion_intent\"][\"identity\"]:\n",
    ),

    # Selected team inbox generations, membership rollback records, and attach
    # decisions are each consumed exactly once by their owning operation.
    (
        "team-run-revalidates-inbox-generation",
        '            "members": ordered,\n            "inboxes": inboxes,\n',
        '            "members": ordered,\n            "inboxes": (),\n',
    ),
    (
        "membership-rollback-restores-selected-written-state",
        '            written = undo["written"]\n',
        '            written = undo["record"]\n',
    ),
    (
        "attach-flow-consumes-one-decision",
        "    decision = RESOLVER.attach_decision(\n",
        "    if True:\n"
        "        maybe_attach(immutable({\"socket\": socket, \"terminal\": terminal, "
        "\"alive\": False, \"action\": \"skip\", \"reason\": \"disabled\", "
        "\"command\": ()}))\n"
        "    decision = RESOLVER.attach_decision(\n",
    ),
]


probe.expect(CASES)
