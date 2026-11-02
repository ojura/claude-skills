import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { setImmediate } from "node:timers/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../research/view_initialization.js", import.meta.url), "utf8");

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function fixture() {
  const tabs = new Map([
    [1, { id: 1, windowId: 10, active: true }],
    [2, { id: 2, windowId: 10, active: false }],
    [3, { id: 3, windowId: 10, active: false }],
    [4, { id: 4, windowId: 20, active: true }],
    [5, { id: 5, windowId: 20, active: false }],
  ]);
  const state = { tabs, actions: [], afterUpdate: async () => {}, listener: null,
    requesters: new Set(["requester-1", "requester-2"]) };
  const chrome = {
    debugger: {
      getTargets: async () => [
        ...[...tabs.values()].map(tab => ({ id: `target-${tab.id}`, tabId: tab.id })),
        ...[...state.requesters].map(id => ({ id, type: "other" })),
      ],
    },
    tabs: {
      get: async id => {
        if (!tabs.has(id)) throw new Error("tab disappeared");
        return { ...tabs.get(id) };
      },
      query: async query => [...tabs.values()].filter(tab =>
        tab.windowId === query.windowId && (!query.active || tab.active)).map(tab => ({ ...tab })),
      update: async (id, options) => {
        const tab = tabs.get(id);
        if (!tab) throw new Error("tab disappeared");
        state.actions.push(id);
        if (options.active) {
          for (const item of tabs.values()) {
            if (item.windowId === tab.windowId) item.active = item.id === id;
          }
        }
        await state.afterUpdate(id, options);
        return { ...tab };
      },
    },
    runtime: {
      id: "test-extension",
      onMessage: { addListener: listener => { state.listener = listener; } },
    },
  };
  const context = vm.createContext({ chrome });
  vm.runInContext(source, context);
  state.initialize = (id, requester = "requester-1") => context.initializeView(`target-${id}`, requester);
  state.active = windowId => [...tabs.values()].find(tab => tab.windowId === windowId && tab.active)?.id;
  return state;
}

test("selection is restored in the same window", async () => {
  const state = fixture();
  const result = await state.initialize(2);
  assert.deepEqual(state.actions, [2, 1]);
  assert.equal(state.active(10), 1);
  assert.equal(result.changed, true);
  assert.equal(result.restored, true);
});

test("two operations in one window are serialized", async () => {
  const state = fixture();
  const entered = deferred();
  const finish = deferred();
  state.afterUpdate = async id => {
    if (id === 2) { entered.resolve(); await finish.promise; }
  };
  const first = state.initialize(2);
  await entered.promise;
  const second = state.initialize(3);
  await setImmediate();
  assert.deepEqual(state.actions, [2]);
  finish.resolve();
  await Promise.all([first, second]);
  assert.deepEqual(state.actions, [2, 1, 3, 1]);
  assert.equal(state.active(10), 1);
});

test("a closed requester skips queued work while an applied selection is restored", async () => {
  const state = fixture();
  const entered = deferred();
  const finish = deferred();
  state.afterUpdate = async id => {
    if (id === 2) { entered.resolve(); await finish.promise; }
  };
  const first = state.initialize(2, "requester-1");
  await entered.promise;
  const second = state.initialize(3, "requester-2");
  await setImmediate();
  state.requesters.clear();
  finish.resolve();
  const [applied, queued] = await Promise.all([first, second]);
  assert.equal(applied.restored, true);
  assert.equal(queued.reason, "requester_closed");
  assert.deepEqual(state.actions, [2, 1]);
  assert.equal(state.active(10), 1);
});

test("different windows do not block each other", async () => {
  const state = fixture();
  const entered = deferred();
  const finish = deferred();
  state.afterUpdate = async id => {
    if (id === 2) { entered.resolve(); await finish.promise; }
  };
  const first = state.initialize(2);
  await entered.promise;
  await state.initialize(5);
  assert.deepEqual(state.actions, [2, 5, 4]);
  assert.equal(state.active(20), 4);
  finish.resolve();
  await first;
  assert.equal(state.active(10), 1);
});

test("a failed selection still restores the previous tab and releases the queue", async () => {
  const state = fixture();
  let fail = true;
  state.afterUpdate = async id => {
    if (id === 2 && fail) { fail = false; throw new Error("selection failed after applying"); }
  };
  await assert.rejects(state.initialize(2), /selection failed after applying/);
  assert.equal(state.active(10), 1);
  await state.initialize(3);
  assert.deepEqual(state.actions, [2, 1, 3, 1]);
  assert.equal(state.active(10), 1);
});

test("a subsequent selection by the user is retained", async () => {
  const state = fixture();
  state.afterUpdate = async id => {
    if (id === 2) {
      state.tabs.get(2).active = false;
      state.tabs.get(3).active = true;
    }
  };
  const result = await state.initialize(2);
  assert.equal(state.active(10), 3);
  assert.deepEqual(state.actions, [2]);
  assert.equal(result.restored, false);
  assert.equal(result.restoreSkipped, "selection_changed");
});

test("a previous tab moved to another window is not moved back", async () => {
  const state = fixture();
  state.afterUpdate = async id => {
    if (id === 2) state.tabs.get(1).windowId = 30;
  };
  const result = await state.initialize(2);
  assert.equal(state.tabs.get(1).windowId, 30);
  assert.deepEqual(state.actions, [2]);
  assert.equal(result.restoreSkipped, "previous_tab_moved");
});

test("an already selected tab and an unknown target do not change selection", async () => {
  const state = fixture();
  assert.equal((await state.initialize(1)).reason, "already_active");
  assert.equal((await state.initialize(99)).reason, "not_tab");
  assert.deepEqual(state.actions, []);
});

test("the extension message handler rejects malformed or external requests", async () => {
  const state = fixture();
  for (const request of [null, 1, {}, { method: "other", targetId: "target-2", requesterTargetId: "requester-1" }]) {
    assert.equal(state.listener(request, { id: "test-extension" }, () => assert.fail("unexpected reply")), false);
  }
  assert.equal(state.listener({ method: "initialize-view", targetId: "target-2", requesterTargetId: "requester-1" },
    { id: "other-extension" }, () => assert.fail("unexpected reply")), false);
  assert.deepEqual(state.actions, []);
});

test("the extension holds the response until selection restoration finishes", async () => {
  const state = fixture();
  const reply = deferred();
  const retained = state.listener({ method: "initialize-view", targetId: "target-2", requesterTargetId: "requester-1" },
    { id: "test-extension" }, reply.resolve);
  assert.equal(retained, true);
  const response = await reply.promise;
  assert.equal(response.ok, true);
  assert.equal(response.result.restored, true);
  assert.equal(state.active(10), 1);
});
