// Claude Code 2.1.309 provides the selected transcript as AbovePrompt.props.view.
// The environment belongs to this Claude process; the C client copies it into
// each request because the Python renderer daemon serves several sessions.
let revision = 0;
let selected;
let lastPublished;
let writes = Promise.resolve();
let ownerReady;
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

// The native engine kills a spawned child when this module unloads. Its lock
// gives the renderer a live ownership signal even if the host retains our env.
// Nothing wakes periodically: the child blocks and the stream waits for exit.
async function ownView($, ready, failed) {
  let announced = false;
  try {
    const configured = await $.env.get("CLAUDE_CONFIG_DIR");
    const home = await $.env.get("HOME");
    const client = `${configured || `${home}/.claude`}/statusline-client`;
    const child = $.process.spawn({ argv: [client, "--view-owner"] });
    let buffer = "";
    for await (const chunk of child) {
      if (chunk.stream !== "stdout" || announced) continue;
      buffer += chunk.text;
      if (buffer.length > 1024) throw new Error("Invalid statusline owner response");
      const newline = buffer.indexOf("\n");
      if (newline < 0) continue;
      const owner = JSON.parse(buffer.slice(0, newline)).owner;
      if (typeof owner !== "string" || !/^statusline-view-[A-Za-z0-9]{6}$/.test(owner)) {
        throw new Error("Invalid statusline owner handle");
      }
      announced = true;
      ready(owner);
    }
    if (!announced) failed(new Error("Statusline owner exited before startup"));
  } catch (error) {
    if (!announced) failed(error);
  } finally {
    ownerReady = undefined;
    selected = undefined;
    ++revision;
    // The released lock invalidates this handle. An old module must not clear
    // the environment after a replacement module has published a new handle.
  }
}

function ensureOwner($) {
  if (!ownerReady) {
    let ready;
    let failed;
    ownerReady = new Promise((resolve, reject) => { ready = resolve; failed = reject; });
    void ownView($, ready, failed);
  }
  return ownerReady;
}

export function register(on) {

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
    const owner = await ensureOwner($);
    const sessionId = await $.session.id();
    const known = await $.agent.list();
    if (rev !== revision) return next(e);
    const agent = known.find((item) => item.id === id);
    selected = {
      version: 1,
      session_id: sessionId,
      agent_id: id,
      owner,
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
