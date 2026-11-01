// Selection and restoration run in the extension worker, so they can finish
// even if the daemon's hidden extension page or CDP connection disappears.
const windowOperations = new Map();

async function initializeView(targetId, requesterTargetId) {
  const target = (await chrome.debugger.getTargets()).find(item => item.id === targetId);
  if (!target || typeof target.tabId !== "number") {
    return { changed: false, reason: "not_tab" };
  }
  const tab = await chrome.tabs.get(target.tabId);
  const previous = windowOperations.get(tab.windowId) || Promise.resolve();
  const operation = previous.catch(() => {}).then(async () => {
    const targets = await chrome.debugger.getTargets();
    if (!targets.some(item => item.id === requesterTargetId)) {
      return { changed: false, reason: "requester_closed" };
    }
    const current = await chrome.tabs.get(tab.id);
    const before = (await chrome.tabs.query({ windowId: current.windowId, active: true }))[0];
    if (!before || before.id === current.id) {
      return { changed: false, reason: "already_active" };
    }
    const result = { changed: false, restored: false, windowId: current.windowId };
    try {
      await chrome.tabs.update(current.id, { active: true });
      result.changed = true;
    } finally {
      const active = (await chrome.tabs.query({ windowId: current.windowId, active: true }))[0];
      if (active?.id === current.id) {
        const saved = await chrome.tabs.get(before.id);
        if (saved.windowId === current.windowId) {
          await chrome.tabs.update(saved.id, { active: true });
          result.restored = true;
        } else {
          result.restoreSkipped = "previous_tab_moved";
        }
      } else {
        result.restoreSkipped = "selection_changed";
      }
    }
    return result;
  });
  windowOperations.set(tab.windowId, operation);
  try {
    return await operation;
  } finally {
    if (windowOperations.get(tab.windowId) === operation) {
      windowOperations.delete(tab.windowId);
    }
  }
}

chrome.runtime.onMessage.addListener((request, sender, respond) => {
  if (sender.id !== chrome.runtime.id || !request || typeof request !== "object" ||
      request.method !== "initialize-view" ||
      typeof request.targetId !== "string" || !request.targetId ||
      typeof request.requesterTargetId !== "string" || !request.requesterTargetId) {
    return false;
  }
  initializeView(request.targetId, request.requesterTargetId).then(
    result => respond({ ok: true, result }),
    error => respond({ ok: false, error: String(error) }),
  );
  return true;
});
