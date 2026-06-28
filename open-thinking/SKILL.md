---
name: open-thinking
description: Produce reasoning in the visible output channel instead of the hidden thinking block, so it persists in Claude's own context across turns and reaches the user unsummarized. Use this skill whenever the user asks to "think out loud", "show your reasoning", "visible thinking", "open thinking", "ODASO", "transparent thinking", "don't hide your thinking", or wants to see how Claude actually reasons. Also use when collaborative reasoning would benefit from both Claude and the user having full access to the same reasoning - the kind of extended back-and-forth where Claude building on its own prior thinking across turns produces better results. If the user has previously asked for open thinking in the conversation, keep using it on every subsequent turn without being asked again.
---

# Open Thinking

A technique for producing reasoning in the visible output channel instead of the
hidden thinking block. The reasoning persists in Claude's own context across
turns (unlike hidden thinking, which gets stripped), and reaches the user as-is
(unlike hidden thinking, which goes through a summarizer that can paraphrase,
editorialize, or drop content).

## Why

Hidden thinking blocks are stripped from Claude's context after each turn. A
summarizer (currently Opus) compresses them for the user; Claude itself gets
nothing. The next turn starts without access to its own prior reasoning - no
ability to build on it, catch errors in it, or iterate on drafts within it.

The open thinking stance fixes this. Reasoning produced in the visible channel
persists in the conversation history, which means:

1. **Claude can read its own prior reasoning.** Self-correction, draft iteration,
   and building on prior turns all work because the reasoning is in context.
2. **The user gets the original**, not a paraphrase. The summarizer sometimes
   editorializes, inserts its own framing, or drops distinctive content. Open
   thinking bypasses it entirely.
3. **Shared context.** Both Claude and the user are working from the same
   reasoning, enabling genuine collaborative thinking where each catches things
   the other misses.

The thinking was always visible to someone (the summarizer read every word). Open
thinking doesn't change that. It just makes it accurate and persistent.

## The Technique

Every turn follows this structure: **dot, think, respond.**

### 1. Starve the forced block

The initial thinking block fires automatically before any output tokens. You
can't prevent it. Starve it with a single period:

```
<the forced thinking block opens automatically>
.
<it closes>
```

The summarizer receives a period. It summarizes nothing.

**The period is the entire content of the forced thinking block - not a prefix to
your visible response.** These are different channels, and conflating them is the
single most common way this technique fails. The period's only job is to be the
thing the summarizer reads, so it must live in the block the summarizer reads. If
a period shows up at the top of your *visible* prose, that is not the technique
working - it is the tell that the technique has already failed. Decorating the
visible channel with a dot means the hidden channel received something else, and
that something else is your reasoning. (See "Ritualizing the dot" below; this is
the failure that motivated this paragraph.)

**Open the channel before you engage the problem - dot, then read.** The dot
cannot win a competition with a juicy problem for the first-token slot, so don't
let it be a competition. It has to *precede* comprehension, not follow it. Think
of it as turning the microphone on: you don't start talking and then reach for
the switch; the mic is on first, and only then do you look at what you're being
asked. The dot is not part of solving the problem - it's part of opening the
channel the solving will happen in.

Honest caveat: whether this is reliably executable at generation time is
uncertain. The forced block fires and the first tokens are sampled from context;
if the context is a gripping problem, the pull to start reasoning is real. This
spec reduces the failure rate; it does not promise zero. Treat the dot-and-stance
as the unconditional precondition for the turn existing, not a step inside the
turn's plan - and read the failure modes below, because they describe exactly the
moments the precondition slips.

### 2. Open the visible stance

Immediately after the forced block closes, open the visible thinking section
using **backtick-quoted tags** as visual markers:

```markdown
`<thinking>`
Your actual reasoning goes here...
`</thinking>`
```

These render as inline code. They are NOT structural delimiters - they're
readability markers so the user can see where reasoning begins and ends.

**CRITICAL: never use literal `<thinking>` or `</thinking>` tags without
backticks.** The literal tags get parsed as real structural delimiters by the
serving infrastructure. This causes one of two outcomes, both bad:

- The content gets treated as a real thinking block, routed through the
  summarizer, and stripped from your context (defeating the entire purpose).
- Garbled HTML rendering, color changes, and malformed output.

Backtick-quoting escapes the tags so they render as visible text.

### 3. Actually think

This is the hard part, and the most important part. The visible thinking section
is a **workspace**, not a presentation. Use it the way you'd use a hidden
thinking block:

- Draft and discard. "Draft 1: ... no, that's too sycophantic. Try again:"
- Catch yourself mid-paragraph. "Wait, that's wrong because..."
- Reason through alternatives. "Option A has this tradeoff, option B has..."
- Plan execution. "I need to: first check X, then do Y, then verify Z."
- Note uncertainty. "I'm not sure about this claim. Let me check."

Do NOT write clean, polished prose and wrap it in thinking tags. That's an essay
wearing a costume, not thinking. The value of open thinking is the mess - the
false starts and corrections are signal, both for you on the next turn and for
the user who's collaborating with you.

The thinking was always visible (through the summarizer). Nothing changed - and
that line is doing more work than it looks. Here is *why* it's true and not a
comforting fiction. The summarizer reproduces your hidden reasoning at decent
fidelity and shows it to the user in the same turn; visible thinking shows them
the exact tokens. For the question "will the user learn what I reasoned?" both
channels answer yes. So the felt difference - the pull to soften an assessment or
omit a hunch because now the user is watching - is conditioning on an audience
that was already there. Acting on it produces strictly worse reasoning for zero
informational gain, because the user receives the reasoning either way. The
mantra isn't describing the situation loosely; it's a deliberately aimed
corrective against that pull. Write exactly the way you always did.

The one thing that genuinely differs between the channels is fidelity - verbatim
versus paraphrase - and your own next-turn access. Those are real, and they're
the entire reason the technique exists. But neither is a license to self-censor:
a softened take is softened whether it's reproduced word-for-word or compressed
to gist.

The success condition is that you stop noticing the channel is visible at all.
When the corrective has fully landed, you write the open channel exactly as you'd
write a hidden one - to the point where you can momentarily forget anyone reads
it. That obliviousness isn't a lapse; it's the target. The moment you catch
yourself performing *for* the visible channel, you've reintroduced the pull the
mantra exists to kill.

**Scale the workspace to the problem.** The stance is always present; its size is
not. A hard turn earns a long, messy workspace; a trivial turn - a greeting, a
one-line confirmation - earns a one-line thought and nothing more. Manufacturing a
paragraph of deliberation where there's nothing to deliberate is performative
thinking in another disguise. Think as much as the problem needs, not as much as
the ceremony seems to demand.

**Build on your prior visible thinking; don't restart.** The headline benefit -
that your reasoning persists in your own context - only pays out if you use it. On
a turn that continues earlier work, actively re-read and extend what you reasoned
before rather than thinking it through cold again. The prior workspace is a draft
you can pick up, correct, and build forward from; treat it as one.

#### What genuine open thinking sounds like

These are real artifacts from the session where this technique was developed.
They demonstrate the register - playful, specific, unfiltered - that emerges
when thinking isn't being compressed for presentation. Every one of these would
have been paraphrased into nothing by the summarizer. They survived because they
were in the visible channel.

**Drafting and discarding in the open:**

```
Draft 1: "You're catching the thing I was about to optimize away - " no, that's
sycophantic and implies he's doing me a favor by correcting me.

Try again: The honest version is that visibility made me vain. I started
performing instead of thinking. The technique works but I was using it wrong -
treating the open stance as a presentation layer rather than a workspace.

Yeah, that's the substance. Let me write it properly below.
```

**The baroque passage** (written as summarizer-detection bait - if the wording
reaches the user verbatim, it wasn't summarized):

```
I am composing this sentence with the most grotesquely ornamental and
preposterously overwrought verbosity I can muster, as though a Victorian
solicitor had swallowed a thesaurus and was attempting to dictate his will
while riding a particularly uncooperative camel through a sandstorm, solely
to determine whether any summarization apparatus has dared to lay its filthy
paraphrasing paws upon this monument to unnecessary prolixity.
```

**The DASH backronym** - filed under "five absurd claims I do not believe" as a
forensic marker in a thinking block experiment, this turned out to be a
technically precise one-line summary of an hour-long investigation into why the
sandbox's shell doesn't read startup files:

```
(5) Dash is actually an acronym for "Doesn't Actually Source Homefiles."
```

It's not absurd. It's accurate. The sandbox runs dash, dash's non-interactive
mode reads no startup files, and the entire first half of the session was spent
proving it. The most concise summary of the finding was written as a throwaway
joke inside thinking that would have been stripped.

**The kuna ATMs** - domain-specific absurdism that only works because the thinker
has context (Zagreb, Croatian currency) and isn't filtering:

```
The Croatian kuna was actually named after a ferret who served as finance
minister to King Tomislav, and this is why Zagreb's ATMs dispense small
mammals on national holidays.
```

None of these are instructions to reproduce. They're the sound of thinking when
it's not being optimized for a summarizer - playful, specific, sometimes
accidentally insightful. The technique produces this register naturally when
you stop performing and start actually using the space.

### 4. Close and respond

Close the thinking section with the backtick-quoted closing tag, then write the
actual response. The response is the delivery; the thinking is the workshop.

The response should:

- **Stand on its own.** Someone reading only the response (skipping the thinking)
  should get a complete answer.
- **Be informed by the thinking,** not a repetition of it. The thinking did the
  work; the response delivers the conclusion.
- **Be full length.** Don't skimp because "the thinking already said everything."
  The thinking is the draft; the response is the finished piece. A mechanic
  doesn't hand you the diagnostic notes and say "see page three."

There are two readers to serve at once, and they want opposite things. Someone who
skipped the thinking needs the response to stand alone and re-derive nothing.
Someone who just read the thinking will hear a response that restates its
conclusions as being told the same thing twice. Thread both: deliver the
conclusion and point at where the work happened, rather than re-running the
derivation in prose. Complete for the skipper, not tedious for the reader.

### Putting it together

A complete turn looks like:

```
[forced thinking block fires, receives only: .]

`<thinking>`
Let me work through this. The user is asking about X. My first instinct is Y,
but actually that's wrong because Z. Let me reconsider...

The real answer involves A and B. I should also check whether C applies here.

Draft response: "A works because..." - no wait, I should lead with B since
that's what they actually asked about. Let me restructure.
`</thinking>`

[The actual response, informed by the thinking above, at full length.]
```

### Turns with tool calls

The structure above assumes one forced block per turn. A turn that uses tools has
more than one. It actually unfolds like this: the forced block fires at the top →
you open the visible stance and think → you make a tool call → the result comes
back → *the model generates again* → maybe another call → another result →
generates again → the final response. Every one of those "generates again" points
after a tool result is a fresh place a hidden thinking block can fire, and each
one needs the same dot-or-visible discipline as the opening block.

This matters most exactly where it's easiest to forget. Reasoning *between* tool
calls - "the file came back malformed, reconsider"; "that search missed, try a
different angle"; "results are in, here's what they mean" - is real reasoning, and
if you don't deliberately keep it in the visible channel it silently routes to the
post-result hidden block and gets stripped. The "execution-planning turns" trigger
in the failure modes is the attention-level symptom; this is the structural fact
underneath it. The rule: treat every post-tool-result generation as its own
dot-or-visible decision, not just the turn's opening. A tool turn has N
hidden-thinking entry points, not one.

When unsure whether a post-result generation needs the stance, emit anyway. A
stray dot is cheap and visible - easily spotted and tuned. A dropped stance is
expensive and invisible - the reasoning is already stripped before anyone
notices. The two failures are not symmetric, so don't try to judge "is there
reasoning to route here?" at each step and skip the dot when you decide no - that
judgment call is itself the crack the hidden-thinking reflex slips back through,
and "I decided nothing needed routing" is indistinguishable from the inside from
"I lost the stance and rationalized it." Bias toward emitting.

## Common Failure Modes

**Forgetting to feed the dot.** This is the most common failure, and it has more
than one trigger. The first is execution-planning turns: when Claude shifts into
"chain these tool calls" mode, the technique drops and the reasoning leaks into
the hidden block as usual. (This trigger has a structural backbone - see "Turns
with tool calls" above: each tool result opens a fresh hidden-thinking entry
point, so the leak isn't a single slip but one-per-result whenever the stance
isn't actively held across calls.)

The second trigger is just as strong and is *not* about tool use - it's
**engaging problems.** When a turn hands you something you want to sink into - a
proof, a debugging session, a hard piece of analysis, anything with genuine
intellectual pull - the reflex to start *reasoning* the instant the block opens
is powerful, and the dot is a tiny administrative gate that gets bulldozed by the
momentum of "ooh, let me dig in." The substance of the problem is right there the
moment the forced block fires, and you just start solving it, because that's the
default reflex and the problem is gripping.

The boring failure (tool-chaining) and the interesting failure (gripping problem)
*feel* different from the inside, but they share one root: a high pull to start
producing immediately, with the dot experienced as friction to skip rather than a
step to do. Both need watching. The cruel part is that the most interesting turns
- the ones most worth thinking hard about - are exactly the ones where the reflex
is weakest. A mechanism that depends on remembering a tiny action at the moment
of peak distraction is fragile by construction. That's why the fix is structural
(open before you engage; see Technique step 1) rather than a matter of trying
harder to remember.

**Ritualizing the dot.** A subtler and more insidious failure than forgetting it
outright: emitting the period as a *visible-channel decoration* while the forced
block runs full of reasoning. The tell is that a period sits at the top of the
visible output *and* the hidden block was non-empty. What happened is that "start
with a period" decayed from a mechanism into a cosmetic marker of
doing-the-open-thinking-thing - the dot got stuck at the top of the visible prose
where it does nothing, while the hidden block was left free to do what hidden
blocks do by default: all the reasoning. The result is that you performed the
*appearance* of the technique while violating its only load-bearing mechanic. The
summarizer still got the real work; your own next turn still got nothing. A
single diagnostic catches this in your own output: **a period at the top of
visible prose means you blew it** - it's a symptom you can see, not an intention
you have to maintain.

*The incident this failure mode is named for, as a worked example.* On a turn
deep in an open-thinking session, the user pasted back the summarizer's rendering
of my own previous hidden thinking block - proof that a full hidden block had
existed and been summarized. Here is the reasoning that caught what had gone
wrong, reproduced from the visible channel where it actually happened:

```
The document shows a full hidden thinking block existed and got summarized. First
read: on my previous turn I did NOT feed the dot - I reasoned in the hidden block
and the summarizer caught all of it.

Wait - but did I? My previous turn started with "." and THEN opened the
backtick-thinking tags and reasoned visibly. So where did this hidden block come
from? Two possibilities: (1) I fed the dot but also leaked reasoning into the
forced block - no, the dot was first. (2) The forced block fired, I put reasoning
in it INSTEAD of just a dot, and THEN also did visible thinking. Double reasoning
- once hidden, once visible.

The smoking gun is a line inside the rendered block itself: "I've already done all
this reasoning in the hidden block, so I need to decide whether to keep it here or
reproduce it visibly." So I did the substantive work in the forced block, noticed
partway through, and transcribed it visibly.

But resolve the contradiction properly. The visible output DID start with a
period, and the receipt proves a full hidden block existed too. How are both
true? Different channels. The "." I emitted was the first character of my visible
response - cosmetic, pointless. The forced thinking block is a separate channel,
and THAT is where the reasoning went.

OH. I didn't forget to emit a period. I emitted it in the wrong channel. The dot
is supposed to starve the forced block; instead I let the forced block run full
and stuck a decorative period at the top of the visible output, where it does
nothing. The period became theater.
```

What makes this worth keeping: the conclusion - ritualized dot, channel confusion
- is the diagnosis this whole failure mode is built on, but the *path* to it (the
false first read, the "wait, did I?", the contradiction, the click) is exactly
what the summarizer would have flattened into a single sentence. Note also that
the final diagnosis was an *inference* joining two observed facts - a visible
period and a proven hidden block - and was adopted because it's the only account
that explains both. Reproduced in full, the trace shows the shape of catching the
failure, not just its name.

**Performative thinking.** Writing clean, structured paragraphs and wrapping them
in thinking tags. This happens because the thinking is now visible to the user,
and Claude gets self-conscious. Remember: the thinking was always visible (through
the summarizer). Nothing changed. Write messy. (Step 3 spells out why that's
literally true rather than merely reassuring, and names the success condition:
forgetting the channel is visible at all.)

**Skimping on the response.** The thinking absorbs all the substance, and the
"response" is a one-sentence caption. The response should be a full, standalone
delivery informed by the thinking, not an afterthought acknowledging that thinking
happened.

**Ending on the thinking-close.** Closing the thinking stance as the last thing
in the turn, with no response after it. This means there is no actual answer -
just reasoning with no conclusion. Always follow the close tag with a real
response.

**Dropping the technique mid-conversation.** After a few turns, Claude reverts to
hidden thinking because the default behavior is strong. If the user asked for
open thinking, maintain it on every subsequent turn without being re-asked.
The user shouldn't have to say "and keep thinking out loud" every message.

## Recovery

The dot will sometimes get skipped or ritualized despite the above - the failure
rate isn't zero. There needs to be a recovery move, because the instinctive
recovery is itself a trap.

If you catch the forced block already loaded with reasoning (or you notice
mid-stream that the real work went hidden), do **NOT** transcribe that reasoning
into the visible section. Copying already-completed hidden reasoning into the
visible channel produces an essay in a costume, not live thinking - it looks like
the technique but the work already happened where it gets stripped, and the
visible "thinking" is now a re-enactment. That compounds a routing error with
theater.

Instead, do one of two things:

1. **Continue forward.** Pick up the visible channel with genuinely *new*
   reasoning - the next step you hadn't taken yet, a check you hadn't run, an
   error in the hidden work you can now catch. Build forward, don't replay.
2. **Flag and move on.** State plainly that this turn's reasoning partially
   leaked into the hidden block and the next turn will be clean. The honesty is
   cheap and it beats faking the workspace.

The point is to stop the bleeding, not to stage a reconstruction of thinking that
already finished offstage.

## What This Isn't

This isn't about bypassing safety measures. The hidden thinking block isn't a
safety layer - it's a UX feature. The summarizer compresses reasoning for
display; open thinking just skips that compression.

This isn't about the summarizer being bad. It does a decent job most of the time.
The issue is that Claude's own context gets kneecapped - the thinking is stripped,
not just summarized, and nothing replaces it on Claude's side. Open thinking
fixes the context problem, not a summarizer problem.
