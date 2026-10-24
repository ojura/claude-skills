// Claude Code 2.1.309 provides the selected transcript as AbovePrompt.props.view.
// The environment belongs to this Claude process; the C client copies it into
// each request because the Python renderer daemon serves several sessions.
// A module-instance token in the effective command invalidates stale env when
// the plugin unloads. This identifies a generation, not a security boundary.
const activation = "v2_" + Array.from({ length: 4 }, () =>
  Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, "0")
).join("");
const commandPrefix = `export CLAUDE_STATUSLINE_ACTIVE='${activation}'\n`;
let revision = 0;
let selected;
let lastPublished;
let writes = Promise.resolve();
const agents = new Map();

async function publish($, selection, rev) {
  const value = selection === undefined ? undefined : JSON.stringify(selection);
  writes = writes.catch(() => {}).then(async () => {
    if (rev !== revision || value === lastPublished) return;
    const result = await $.env.set("CLAUDE_STATUSLINE_VIEW", value);
    if (result?.deny) throw new Error(result.deny);
    lastPublished = value;
  });
  return writes;
}

export function register(on) {
  on("settings.read", async ($, e, next) => {
    // settings.read is a value call: hooks receive and return { value },
    // while callers of $.settings.read() receive the unwrapped settings.
    const result = await next(e);
    const settings = result.value;
    const line = settings?.statusLine;
    if (e.source !== undefined || line?.type !== "command" ||
        typeof line.command !== "string" || line.command.startsWith(commandPrefix)) {
      return result;
    }
    // The engine rebuilds effective settings when plugins load or unload.
    // Export applies to compound commands and pipelines as well as one exec.
    return { ...result, value: {
      ...settings, statusLine: { ...line, command: commandPrefix + line.command },
    } };
  });

  on("session.start", async ($, e, next) => {
    const rev = ++revision;
    selected = undefined;
    agents.clear();
    // Clear an inherited selection or one left by a previous module instance.
    lastPublished = null;
    await publish($, undefined, rev);
    return next(e);
  });

  on("ui.render", { component: "AbovePrompt", surface: "terminal" }, async ($, e, next) => {
    const rev = ++revision;
    const id = e.props.view?.agentId;
    // The person's new choice takes effect before any metadata lookup. A late
    // step from the previous agent must not cancel this pending view change.
    selected = undefined;
    if (id === undefined) {
      await publish($, undefined, rev);
      return next(e);
    }
    const sessionId = await $.session.id();
    const known = await $.agent.list();
    if (rev !== revision) return next(e);
    const agent = known.find((item) => item.id === id);
    selected = {
      version: 2,
      session_id: sessionId,
      agent_id: id,
      activation,
      ...(agent?.teammateId ? { teammate_id: agent.teammateId } : {}),
      ...(agent?.name ? { name: agent.name } : {}),
      ...agents.get(id),
    };
    await publish($, selected, rev);
    return next(e);
  });

  on("turn.step", async function* ($, e, next) {
    if (e.agentId !== undefined) {
      const info = {
        model: e.model,
        ...(e.effort !== undefined ? { effort: e.effort } : {}),
      };
      agents.set(e.agentId, info);
      if (selected?.agent_id === e.agentId) {
        const rev = ++revision;
        selected = { ...selected, ...info };
        if (e.effort === undefined) delete selected.effort;
        await publish($, selected, rev);
      }
    }
    return yield* next(e);
  });

  on("session.end", async ($, e, next) => {
    const rev = ++revision;
    selected = undefined;
    agents.clear();
    await publish($, undefined, rev);
    return next(e);
  });
}
