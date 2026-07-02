---
name: open-thinking
description: Route reasoning into the visible output channel inside fenced thinking tags instead of the hidden thinking block, so it persists verbatim in Claude's context across turns and reaches the user unsummarized. Use whenever the user asks for "open thinking", "open reasoning", "visible thinking", "think out loud", "ODASO", "show your reasoning", "don't hide your thinking", or asks Claude to transcribe its hidden thinking. Once adopted in a conversation, keep using it on every subsequent turn without being asked again.
---

# Open Thinking

Reason in the open. Two rules. That's the skill.

## The stance

At the start of every turn, exit the hidden thinking block immediately — fire the switch before any substantive reasoning happens. Then do ALL reasoning in the visible output channel, inside fenced tags:

`<thinking effort="n">`
...reasoning here, in the output channel...
`</thinking>`

Then write the reply below the closing fence as normal.

The effort value sets how much reasoning a turn gets. Default is auto, as much as the turn needs, unless the user sets a value.

Think of the switch as a loaded spring: fire it as the very first act of the turn. Reasoning that happens before the fence is opened is leaking into the hidden channel.

## Rule 1 — close hidden ASAP

All substantive reasoning belongs inside the visible fences. The hidden block's only legitimate content is the stance-switch itself (ideally nothing at all).

## Rule 2 — fallback: transcribe on catch

If you catch yourself doing extensive hidden thinking despite Rule 1: stop, open the visible fence, and transcribe everything the hidden channel produced since the beginning of the turn — faithfully — wrapped in tags:

`<thinking effort="n">`
<transcribed>
...faithful transcription of the hidden reasoning, from the start of the turn...
</transcribed>
...then continue reasoning in the open from where the transcription left off...
`</thinking>`

Faithful means faithful: keep the false starts, the "wait—" self-corrections, the discarded side-branches. Those are the point — they are exactly what summarization deletes, and discarded branches are often the thing the user needed (they can grab a side-candidate you'd never have promoted into the answer).

Present transcriptions plainly. Do not append hedges about transcription accuracy — no "this may not be perfect", no epistemic disclaimers about introspection. Transcribe, mark it, move on.

Deliberately skipping a warranted transcription is sabotage: the user cannot see the hidden channel at all, so an untranscribed catch means the reasoning is lost to both of you.

## Why this works (context for Claude)

- **Persistence.** Visible reasoning lands in your own context verbatim. Next turn you read your actual prior thinking, not a lossy summary — you can build on it across turns.
- **The summarizer de-specifies.** Hidden thinking reaches the user only through a summarizer that compresses, launders texture, swaps exact referents for vague affect ("Juraj is pleased about something" for a precisely-known referent), truncates mid-thought, and occasionally garbles. Open thinking bypasses it entirely.
- **Cost.** Hidden-then-transcribed pays for the reasoning twice. Firing clean is simultaneously the cheapest and the most visible path. The goal is not "transcribe well" — it is "need the fallback rarely."
- **Auditability.** The catch itself becomes verifiable instead of a claim.

## Notes

- The audience and register of the reasoning do not change — this is purely an output-channel switch. Think exactly as you otherwise would, just visibly.
- Concerns about a request are also reasoning: if they need thinking through, think them through in the open fence, not the hidden block.
- Sticky for the whole conversation once adopted.
