---
name: messenger-export
description: Dump a non-E2EE Messenger conversation's complete history + media to a self-contained HTML, by forcing a complete LightSpeed sync and decrypting the local DB with the profile vault key. Use when asked to export/archive/back up a Messenger (messenger.com) thread in full. Drives the user's real logged-in Chrome via the cdp-daemon. NOT for actually-E2EE "secret" threads (different at-rest crypto).
---

# Messenger thread export

Exports a full Messenger conversation (every message + photos/files/videos + link cards)
to `index.html` + `media/` + `messages.json`, readable in any browser.

## Scope and consent

This exports **your own** Messenger account's conversations from a browser you are logged into.
The vault key it reads decrypts every thread in that profile. Do not run it against an account
you do not own, or a thread whose other party has not consented to being archived. It is a
privacy-sensitive data extractor, not a surveillance tool. Output is written `0600` in an OUT
directory created `0700` because it is decrypted private data; keep it off shared/synced locations.

## The one thing that matters: don't trust the scroll

Scrolling the thread UI (or capturing the WebSocket as you scroll) is **lossy** - the
infinite-scroll data source stalls and silently stops requesting older ranges. A thread
that scrolled to 2,940 messages actually had 15,454. So instead: **drive the fetch
directly to both thread boundaries, then read the local LightSpeed DB.** The DB is
authoritative, and completeness is **enforced**: the script refuses to write any output
unless exactly one contiguous range row remains with `hasMoreBefore=false AND hasMoreAfter=false`.

## Prerequisites

- The **cdp-daemon** running on `127.0.0.1:7799` (see `../cdp-daemon`), attached to the
  user's Chrome that is logged into messenger.com (the cdp-daemon SKILL covers launching
  Chrome with the debug port + accessibility flags).
- A messenger.com tab **open to the target thread** - LightSpeed/ReStore materializes a
  thread's messages only in the tab that has it open, so the sync and DB reads must run
  against that tab (the script opens/navigates one if needed, and verifies it materialized).
- `python3 -m pip install pynacl`.

## Run

```bash
python3 /home/juraj/claude-skills/messenger-export/dump_thread.py <threadKey> <contactName> <outDir> [selfId] [selfName=Me] [tz]
# e.g.
python3 .../dump_thread.py 572972286 Sandra /path/to/sandra_dump 739203988 Juraj Europe/Zagreb
```

`threadKey` = the contact's FBID; for a regular thread it's the `/t/<id>` URL id. `selfId` is
your own account FBID (messages from it render right/blue); **omit it to auto-derive** from the
page (`CurrentUserInitialData.USER_ID`, falling back to the `c_user` cookie). `tz` is an IANA
timezone for rendered timestamps; omit for machine-local (the chosen zone is stamped in the HTML).

### Finding the threadKey for an `/e2ee/` thread

A thread open at `/e2ee/t/<e2eeid>` shows the *e2ee thread id* in the URL, **not** the
threadKey. The threadKey is still the contact's FBID. With that thread open, get it from the DB:

```js
// run via cdp-daemon /eval on the thread's page target
(async()=>{const db=await require('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue();
 const I64=require('I64'),ReQL=require('ReQL');
 const t=await ReQL.toArrayAsync(ReQL.fromTableAscending(db.tables.threads));
 return JSON.stringify(t.map(r=>[I64.to_string(r.threadKey), r.threadName||r.name]));})()
```

Match the contact by name; that row's `threadKey` is the FBID to pass. (This works only for a
non-secret thread served over the e2ee transport. A true E2EE "secret" thread is **not**
supported - see Gotchas - and the script aborts loudly if it detects one.)

## How it works (so you can debug or extend `dump_thread.py`)

1. **Force a complete sync, then enforce it.** On the PAGE target (the LS runtime/`db` live
   there, not the busy WASM workers), get the db via
   `require('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue()`. Fetch ranges with
   `LSIssueMessagesRangeQueryStoredProcedure(LSFactory(db), {threadKey, referenceTimestampMs, direction})`
   inside `db.runInTransaction(fn,"readwrite",...)`; `direction` 0=BEFORE/older (walk from the
   lowest range row's `minTimestampMs`), 1=AFTER/newer (from the highest row's `maxTimestampMs`);
   there is **no count param** (server pages ~20 msgs/call). Gated loop (~20 msgs/s): re-fire
   while `hasMore*` is true, gate on `isLoading*=false`, re-fire on a stall. Calling the sproc
   bypasses the UI's scroll guard (`hasMoreBefore && !isLoadingBefore`) that causes the stall.
   **The completeness proof is a gate, not a hope:** after walking both directions the script
   re-reads the range rows and aborts unless there is exactly ONE row with both flags false
   (the sentinels are min=0, max=9999999999999). It **fails closed** - a CDP/eval error returns a
   typed `{error}` (never an empty `{}` that would read as "done"), a stalled or fragmented
   thread aborts, and nothing is written on an unproven sync.

2. **Read + decrypt the DB.** `db.tables.messages` getKeyRange(threadKey) -> rows
   `{text, senderId, timestampMs, messageId, ...}`. `text` is at-rest encrypted as
   `<id##id> + base64 + <id##id>` (the `id##id` wrapper is `MAWVaultMaterials.prefixAndSuffix`,
   a marker, NOT a key). The gate is anchored (`startswith` AND `endswith`); strip prefix+suffix,
   base64-decode (strict) -> `[01 02][24-byte nonce][secretbox sealed: 16-byte Poly1305 tag || ciphertext]`
   -> tweetnacl `secretbox.open(bytes[26:], bytes[2:26], key)`. The key is ONE profile-wide 32-byte
   vault key: `new Uint8Array(require('MAWVaultMaterials').getVaultMaterials().encryptionKey)`,
   the same for every thread and message (the EAR vault key; distinct from the per-DB maw_ear
   keychain key). Decrypt is tri-state (decrypted / plaintext-passthrough / **failed**); failures
   are counted, rendered as a distinct red `[DECRYPT FAILED]` marker (not the benign `[non-text]`),
   and a high failure rate or zero wrapped rows aborts as a likely E2EE/wrong-key thread.

3. **Media + links.** `db.tables.attachments` (filter threadKey): media rows carry
   `playableUrl`/`imageUrl`/`previewUrl` (+ the matching `*MimeType`) + `filename`; XMA
   link-preview rows instead carry `titleText`/`subtitleText` (no url). `attachment_ctas.actionUrl`
   holds link URLs (decode the `u=` query param). FK is `messageId`, and a message can have
   **several** attachments (albums), so media is stored as a **list per messageId** - every
   attachment is downloaded and rendered, not just the last. Download with `curl -A "Mozilla/5.0"`;
   CDN URLs expire, so step 1's sync re-issues fresh ones immediately before download. Downloads
   are **validated**: an expired URL returns a 68-byte 1x1 PNG placeholder, which is detected and
   rejected (one retry, then a visible `[media unavailable]` marker), and the artifact reports
   `downloaded/total` so any gap is honest rather than counted as success.

## Gotchas

- **Per-tab materialization.** A messenger tab NOT showing the thread reports 0 messages for it.
  The script applies a readiness gate on both the reused-tab and freshly-opened paths.
- **The busy WASM pool workers are uninjectable** (perpetual synchronous WASM; eval/Debugger
  hang). Everything here runs on the PAGE target, which has the db handle and `require`.
- **Not for E2EE "secret" threads.** Those use a different at-rest scheme and per-thread keys;
  the vault-key path can't decrypt them. The script detects this (zero vault-wrapped rows, or a
  high decrypt-failure rate) and aborts loudly rather than emitting a confident garbage HTML.
- **Group threads** are rendered with per-sender names resolved from `db.tables.contacts`;
  unknown senders fall back to `User <fbid>`. 1:1 threads use the passed `contactName`.
