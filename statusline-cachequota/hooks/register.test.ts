import { test, expect, mock } from "claude-code/testing";
import { register } from "./register.js";

const empty = { type: "Box", props: {}, children: [] };

function view(agentId?: string, surface = "terminal") {
  return {
    surface, component: "AbovePrompt", requestId: "above-prompt",
    props: {
      hasSurvey: false, isWorking: false, maxRows: 20, bodyColumns: 120,
      scroll: { offset: 0, bodyRows: 20 },
      view: agentId === undefined ? {} : { agentId },
    },
  };
}

function world(on, options = {}) {
  const state = { writes: [], value: undefined, clock: mock.clock(on, { now: 1_000 }) };
  on("session.start", async ($, e) => ({ cwd: e.cwd }));
  on("session.end", async ($, e) => ({ sessionId: e.sessionId }));
  on("session.id", async () => ({ value: "main-session" }));
  on("settings.read", async ($, e) => ({ value: options.settings ?? {
    statusLine: { type: "command", command: "~/.claude/statusline-client", refreshInterval: 1 },
  } }));
  on("process.spawn", async function* () {
    throw new Error("The statusline mod must not spawn a helper process");
  });
  on("agent.list", async () => {
    if (options.beforeList) await options.beforeList();
    return { value: [
      { id: "agent-a", name: "Alpha", description: "First", type: "Explore", status: "running" },
      { id: "agent-b", name: "Beta", description: "Second", type: "general-purpose", status: "idle" },
      { id: "peer@team", teammateId: "peer@team", name: "Peer", description: "Separate process", type: "teammate", status: "idle" },
    ] };
  });
  on("env.set", async ($, e) => {
    expect(e.name).toBe("CLAUDE_STATUSLINE_VIEW");
    if (options.beforeWrite) await options.beforeWrite(e);
    state.writes.push(e.value);
    state.value = e.value;
    return { value: {} };
  });
  on("ui.render", async () => empty);
  on("turn.step", async function* ($, e) {
    return { turnId: e.turnId, index: e.index, answer: "", toolUses: [], stopReason: null, usage: null };
  });
  return state;
}

test("main, agent A, agent B and main preserve the rendered band", async ($, on) => {
  const state = world(on);
  await $.session.start({ cwd: "/work", surface: "terminal", isInteractive: true });
  expect(state.value).toBe(undefined);
  expect(await $.ui.render(view("agent-a"))).toEqual({ ...empty, part: { plugin: "test" } });
  expect(JSON.parse(state.value)).toEqual({
    version: 2, session_id: "main-session", agent_id: "agent-a", name: "Alpha", activation: JSON.parse(state.value).activation,
  });
  await $.ui.render(view("agent-b"));
  expect(JSON.parse(state.value).agent_id).toBe("agent-b");
  await $.ui.render(view());
  expect(state.value).toBe(undefined);
});

test("unchanged renders do not rewrite the process environment", async ($, on) => {
  const state = world(on);
  await $.ui.render(view("agent-a"));
  await $.ui.render(view("agent-a"));
  await $.ui.render(view("agent-a"));
  expect(state.writes.length).toBe(1);
});

test("remote view changes cannot replace the terminal selection", async ($, on) => {
  const state = world(on);
  await $.ui.render(view("agent-a"));
  await $.ui.render(view("agent-b", "desktop"));
  await $.ui.render(view(undefined, "mobile"));
  expect(JSON.parse(state.value).agent_id).toBe("agent-a");
  expect(state.writes.length).toBe(1);
});

test("background work updates metadata without changing selection", async ($, on) => {
  const state = world(on);
  await $.ui.render(view("agent-a"));
  const first = $.turn.step({
    turnId: "turn-b", index: 0, model: "claude-haiku-test",
    effort: "low", messageCount: 1, agentId: "agent-b",
  });
  expect((await first.next()).done).toBe(true);
  expect(JSON.parse(state.value).agent_id).toBe("agent-a");
  const active = $.turn.step({
    turnId: "turn-a", index: 0, model: "claude-opus-test",
    effort: "max", messageCount: 3, agentId: "agent-a",
  });
  const result = await active.next();
  expect(result.done).toBe(true);
  expect(result.value.turnId).toBe("turn-a");
  expect(JSON.parse(state.value)).toEqual({
    version: 2, session_id: "main-session", agent_id: "agent-a", name: "Alpha",
    model: "claude-opus-test", effort: "max", activation: JSON.parse(state.value).activation,
  });
  await $.ui.render(view("agent-b"));
  expect(JSON.parse(state.value).model).toBe("claude-haiku-test");
});

test("session end clears the selected view", async ($, on) => {
  const state = world(on);
  await $.ui.render(view("agent-a"));
  await $.session.end({
    reason: "clear", sessionId: "main-session", resume: { id: "main-session" },
  });
  expect(state.value).toBe(undefined);
});

test("an older metadata lookup cannot overwrite a newer view", async ($, on) => {
  let release;
  let entered;
  const held = new Promise((resolve) => { release = resolve; });
  const started = new Promise((resolve) => { entered = resolve; });
  let count = 0;
  const state = world(on, { beforeList: async () => {
    if (++count === 1) { entered(); await held; }
  } });
  const older = $.ui.render(view("agent-a"));
  await started;
  await $.ui.render(view("agent-b"));
  release();
  await older;
  expect(JSON.parse(state.value).agent_id).toBe("agent-b");
});

test("a previous agent's late step cannot cancel a pending view change", async ($, on) => {
  let release;
  let entered;
  const held = new Promise((resolve) => { release = resolve; });
  const started = new Promise((resolve) => { entered = resolve; });
  let count = 0;
  const state = world(on, { beforeList: async () => {
    if (++count === 2) { entered(); await held; }
  } });
  await $.ui.render(view("agent-a"));
  const switchToB = $.ui.render(view("agent-b"));
  await started;
  const late = $.turn.step({
    turnId: "late-a", index: 0, model: "claude-a", messageCount: 3, agentId: "agent-a",
  });
  await late.next();
  release();
  await switchToB;
  expect(JSON.parse(state.value).agent_id).toBe("agent-b");
});

test("main selection is published after an in-flight agent write", async ($, on) => {
  let release;
  let entered;
  const held = new Promise((resolve) => { release = resolve; });
  const started = new Promise((resolve) => { entered = resolve; });
  let count = 0;
  const state = world(on, { beforeWrite: async () => {
    if (++count === 1) { entered(); await held; }
  } });
  const older = $.ui.render(view("agent-a"));
  await started;
  const main = $.ui.render(view());
  release();
  await older;
  await main;
  expect(state.value).toBe(undefined);
  expect(state.writes.length).toBe(2);
});

test("an idle selection needs no periodic environment writes", async ($, on) => {
  const state = world(on);
  await $.ui.render(view("agent-a"));
  expect(/^v2_[a-f0-9]{32}$/.test(JSON.parse(state.value).activation)).toBe(true);
  await state.clock.advance(20_000);
  expect(state.writes.length).toBe(1);
  await $.ui.render(view());
  const count = state.writes.length;
  await state.clock.advance(20_000);
  expect(state.value).toBe(undefined);
  expect(state.writes.length).toBe(count);
});


// The 2.1.309 test Engine has no settings noun. Exercise this pure hook
// directly; live native runs cover engine command activation and unload.
function settingsHook() {
  let hook;
  register((event, handler) => { if (event === "settings.read") hook = handler; });
  return hook;
}

test("effective settings preserve compound commands and source snapshots", async () => {
  const hook = settingsHook();
  const command = "printf ready | cat; ~/.claude/statusline-client";
  const settings = { statusLine: { type: "command", command, refreshInterval: 1 }, model: "sonnet" };
  const reply = await hook({}, { layers: [] }, async () => ({ value: settings }));
  const result = reply.value;
  expect(/^export CLAUDE_STATUSLINE_ACTIVE='v2_[a-f0-9]{32}'\n/.test(result.statusLine.command)).toBe(true);
  expect(result.statusLine.command.split("\n").slice(1).join("\n")).toBe(command);
  expect(result.statusLine.refreshInterval).toBe(1);
  expect(result.model).toBe("sonnet");
  expect(settings.statusLine.command).toBe(command);
  expect(await hook({}, { layers: [] }, async () => ({ value: settings }))).toEqual(reply);
  expect(await hook({}, { layers: [] }, async () => reply)).toEqual(reply);
  expect(await hook({}, { source: "user", layers: [] }, async () => ({ value: settings }))).toEqual({ value: settings });
});

test("settings without a command statusline are unchanged", async () => {
  const hook = settingsHook();
  const settings = { model: "sonnet" };
  expect(await hook({}, { layers: [] }, async () => ({ value: settings }))).toEqual({ value: settings });
});

test("teammate addresses and roster identities are preserved", async ($, on) => {
  const state = world(on);
  await $.ui.render(view("peer@team"));
  const selected = JSON.parse(state.value);
  expect(selected.agent_id).toBe("peer@team");
  expect(selected.teammate_id).toBe("peer@team");
  expect(selected.name).toBe("Peer");
});
