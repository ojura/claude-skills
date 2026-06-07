---
name: messenger-export
description: Dump a non-E2EE Messenger conversation's complete history + media to a self-contained HTML, by forcing a complete LightSpeed sync and decrypting the local DB with the profile vault key. Use when asked to export/archive/back up a Messenger (messenger.com) thread in full. Drives the user's real logged-in Chrome via the cdp-daemon. NOT for actually-E2EE "secret" threads (different at-rest crypto).
---

# Messenger thread export

Exports a full Messenger conversation (every message + photos/files/videos + link cards)
to `index.html` + `media/`, readable in any browser. Built and verified on real threads
of 11k and 15k messages.

## The one thing that matters: don't trust the scroll

Scrolling the thread UI (or capturing the WebSocket as you scroll) is **lossy** - the
infinite-scroll data source stalls and silently stops requesting older ranges. A thread
that scrolled to 2,940 messages actually had 15,454. So instead: **drive the fetch
directly to both thread boundaries, then read the local LightSpeed DB.** The DB is
authoritative; completeness is provable from its range flags.

## Prerequisites

- The **cdp-daemon** running on `127.0.0.1:7799` (see `../cdp-daemon`), attached to the
  user's Chrome that is logged into messenger.com.
- A messenger.com tab **open to the target thread** - LightSpeed/ReStore materializes a
  thread's messages only in the tab that has it open, so the sync and DB reads must run
  against that tab (the script opens/navigates one).
- `python3 -m pip install pynacl`.

## Run

```bash
python3 dump_thread.py <threadKey> <contactName> <outDir> [selfId=739203988] [selfName=Juraj]
# e.g.
python3 dump_thread.py 572972286 Sandra /home/juraj/sandra_dump
```

`threadKey` = the contact's FBID. For a regular thread it's the `/t/<id>` URL id. For an
`/e2ee/t/<e2eeid>` URL it is still the contact FBID, not the e2ee id (look it up once from
the data - e.g. e2ee `7385692221555435` -> threadKey `1241408603`). `selfId` is the
account owner's FBID (their messages render right/blue).

## How it works (so you can debug or extend `dump_thread.py`)

1. **Force a complete sync.** On the PAGE target (the LS runtime/`db` live there, not the
   busy WASM workers), get the db: `await require('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue()`.
   Fetch a message range with the sproc
   `LSIssueMessagesRangeQueryStoredProcedure(LSFactory(db), {threadKey, referenceTimestampMs, direction})`
   inside `db.runInTransaction(fn,"readwrite",...)`; `direction` 0=BEFORE/older, 1=AFTER/newer;
   there is **no count param** (server pages ~20 msgs/call). Gated loop (~20 msgs/s): read
   the thread's row in `db.tables.messages_ranges_v2__generated`; fire BEFORE with its
   `minTimestampMs` until `hasMoreBefore=false`, then AFTER with `maxTimestampMs` until
   `hasMoreAfter=false`; gate on `isLoadingBefore/After=false`, re-fire on stall.
   **`hasMoreBefore=false AND hasMoreAfter=false` is the only reliable completeness proof**
   (range sentinels are min=0, max=9999999999999). Calling the sproc bypasses the UI's
   scroll guard (`hasMoreBefore && !isLoadingBefore`) that causes the stall.

2. **Read + decrypt the DB.** `db.tables.messages` getKeyRange(threadKey) -> rows
   `{text, senderId, timestampMs, messageId, ...}`. `text` is at-rest encrypted as
   `"<id1>##<id2>" + base64 + "<id1>##<id2>"` (the `id##id` wrapper is
   `MAWVaultMaterials.prefixAndSuffix`, a marker, NOT a key). Strip prefix+suffix,
   base64-decode -> `[01 02][24-byte nonce][ciphertext+16-byte tag]` -> tweetnacl
   `secretbox.open(bytes[26:], bytes[2:26], key)`. The key is ONE profile-wide 32-byte
   vault key: `new Uint8Array(require('MAWVaultMaterials').getVaultMaterials().encryptionKey)`,
   the same for every thread and message (the EAR vault key; distinct from the per-DB
   maw_ear keychain key).

3. **Media + links.** `db.tables.attachments` (filter threadKey): media rows carry
   `playableUrl`/`previewUrl`/`imageUrl` + `playableUrlMimeType` + `filename`; XMA
   link-preview rows instead carry `titleText`/`subtitleText` (no url). `attachment_ctas.actionUrl`
   holds link URLs (decode the `u=` query param). FK is `messageId`. Download with
   `curl -A "Mozilla/5.0"`. CDN URLs expire, but step 1's sync re-issues fresh valid ones,
   so always sync immediately before downloading.

## Gotchas

- **Per-tab materialization.** A messenger tab NOT showing the thread reports 0 messages
  for it. Always operate on the tab with the thread open.
- **The busy WASM pool workers are uninjectable** (perpetual synchronous WASM; eval/Debugger
  hang). Everything here runs on the PAGE target, which has the db handle and `require`.
- **Not for E2EE "secret" threads.** Those use a different at-rest scheme and per-thread
  keys; this vault-key path won't decrypt them.
