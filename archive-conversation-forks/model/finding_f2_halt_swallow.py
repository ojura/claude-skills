"""
Probe: the HALT() inside try/except:pass. Does an unresolvable live session
HALT the run, or get silently swallowed?

Faithful reproduction of lines 282-300.
"""
import json, os, glob

# Two natural HALT implementations a wiring engineer would pick:
def HALT_raise(msg): raise RuntimeError(msg)         # "stop and ask" as an exception
def HALT_print_continue(msg): print("HALT:",msg)     # if someone made it non-raising

# Simulate registry: one running session whose sessionId is NOT a file in the store.
fps = {"REALFILE": []}   # the only file in store
registry = [{"pid": os.getpid(), "sessionId": "GHOSTSESSIONxxxx", "status":"running"}]

def run_live_block(HALT):
    live=set()
    for d in registry:                     # stands in for the glob+json.load
        try:
            if d.get("status")!="running": continue
            try: os.kill(int(d["pid"]),0)
            except Exception: continue
            sid=d.get("sessionId","")[:8]
            if sid not in fps:
                HALT(f"live session {sid} has no file - resolve before proceeding")
            live.add(sid)
        except: pass                       # <-- the swallow
    return live

print("=== HALT implemented as raise (the natural 'stop' wiring) ===")
live = run_live_block(HALT_raise)
print("live after block:", live, " <- EMPTY, and no exception propagated!")
print("The unresolvable session was SILENTLY SKIPPED, not halted.")
print()
print("Downstream: `if not live:` then calls confirm_no_live_or_HALT(), i.e. the run")
print("treats this as 'no live sessions' even though the registry HAD a running one.")
print("That is the exact wrong-live-set cascade the prose spends 15 lines warning against.")
