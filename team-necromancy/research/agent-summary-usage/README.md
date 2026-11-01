# Claude agent-summary token usage

## What was measured

On 2026-11-01, from 10:08:45 to 10:10:28 Zagreb time, `measure.py` sent 36 requests directly to `api.anthropic.com` using the native Claude OAuth credential. They comprised six work requests that populated the cache and 30 full-mode agent-summary replays: ten each for Fable 5.1, Opus 5.5 and Sonnet 5.5. Every request returned HTTP 200 and ended with `end_turn`. These are real Claude usage counts, not GPT accounting or locally generated proxy responses.

The conversation was synthetic. Its cached prefix contained about 29,700 tokens of test records and a `Read` call. The latest tool result was either a short function or that function preceded by 512 comment lines. The longer result added 17,920 tokens. The work request asked for a one-sentence assessment. The summary request used Claude Code 2.1.316's exact full-mode Describe prompt, adaptive thinking, a 32,000-token output limit and either `high` or `xhigh` effort.

The requests reproduced two message arrangements from the native builder:

1. The latest user message contains a tool result. The Describe text joins that message, and `skipCacheWrite` moves its cache marker back to the preceding assistant message.
2. A completed assistant reply follows the tool result. The Describe prompt becomes a new user message, and the marker covers the preceding assistant reply.

The work request completed before its summary replays. The `summary_in_flight` field in the raw results names the first message arrangement; it does not mean this experiment ran concurrent inference. The experiment measured API consumption, not the native timer, its holds, or a working agent's real reporting frequency. It used nonstreaming responses and synthetic input rather than a complete native system prompt and tool set.

## Measured totals

The ten summaries for each model gave these totals. Output includes thinking; the thinking column is a subset of output, not an additional charge.

| Actual response model | Uncached input | Cache reads | Cache creation | Output | Of which thinking |
|---|---:|---:|---:|---:|---:|
| `claude-fable-5-1` | 74,602 | 267,659 | 29,787 | 138 | 0 |
| `claude-opus-5-5` | 74,619 | 267,759 | 29,887 | 676 | 527 |
| `claude-sonnet-5-5` | 74,602 | 267,780 | 29,892 | 1,427 | 1,293 |
| **30 summaries** | **223,823** | **803,198** | **89,566** | **2,241** | **1,820** |

Of the summaries' 2,241 output tokens, 1,820 were thinking and 421 were the rest of the output. Thinking therefore accounted for 81.2% of generation in this sample. The labels themselves used 12 to 19 non-thinking output tokens per response.

The six work requests used 12 uncached input tokens, 89,141 cache-read tokens, 143,405 cache-creation tokens and 751 output tokens, including 210 thinking tokens. They are cache-warming controls, so a summary-to-work percentage from this deliberately chosen request count would not describe a real development session.

The 36 recorded requests together consumed 223,835 uncached input tokens, 892,339 cache-read tokens, 232,971 cache-creation tokens and 2,992 output tokens. A separate initial connectivity check used another 40 uncached input tokens and five output tokens; it is recorded in the investigation transcript rather than `results.jsonl`.

## The latest tool result can be read again at the uncached rate

| Request arrangement | Cache reads per summary | Uncached input per summary |
|---|---:|---:|
| Short tool result followed by Describe text | 29,713 to 29,715 when warm | 297 to 326 |
| Long tool result followed by Describe text | 29,713 to 29,715 | 18,217 to 18,241 |
| Describe after a completed assistant reply | 29,797 to 29,976 | 213 to 244 |

The long tool result was already cached by the completed work request. Nevertheless, every one of its 12 summary replays read it again as uncached input. The summary's marker ends before that result. The Describe prompt and its changing `Previous:` line are also beyond the marker.

For the first summary at each effort, where the Describe prompt is identical, the long result adds exactly 17,920 uncached tokens. Subsequent prompts can differ by a few additional tokens because they quote different previous labels. The cached prefix stays the same. This shows why the token cost cannot be described as merely reading a cached conversation and producing a few words: the last business message can be a substantial uncached input on every report.

For an illustrative 100 reports with these inputs:

- The short-result case reads about 2.97 million cached tokens and 31,000 uncached tokens.
- The long-result case reads about 2.97 million cached tokens and 1.823 million uncached tokens.
- A 100,000-token cached prefix would contribute ten million cache-read tokens per 100 reports, before uncached input and generation.

These are projections from the measured request sizes, not measurements of an hour of native agent activity. The 2.1.316 timer normally waits 30 seconds after a tick finishes; response latency, eligibility checks, unchanged activity and holds reduce the rate below a theoretical 120 reports per hour.

## Full mode inherits effort and can generate thinking

For the tool-result arrangement, there were four `high` and four `xhigh` summaries per model, with both result lengths represented equally:

| Model | Mean output at `high` | Mean output at `xhigh` | Output range across all ten summaries |
|---|---:|---:|---:|
| Fable 5.1 | 13.75 | 13.75 | 12 to 16 |
| Opus 5.5 | 49 | 111.5 | 12 to 176 |
| Sonnet 5.5 | 139.75 | 194.5 | 12 to 314 |

The output limit is not what these requests normally consume. They stopped after a short label. However, Opus and Sonnet sometimes used additional thinking to produce it, and the `xhigh` samples used more output on average. Fable used neither reported thinking nor a longer output at `xhigh` in this sample. These small samples describe these prompts, not each model's general distribution.

## `skipCacheWrite` does not globally prohibit cache creation

The first change from `high` to `xhigh` missed the existing cache in all three models and created 29,713 to 29,715 tokens of cache. The next `xhigh` request read that entry. Later `xhigh` requests with the long result read it too. The three first-switch misses account for 89,141 cache-creation tokens in the summary totals. They are not steady-state overhead from an unchanged effort.

The first summary after a completed assistant reply also created a cache entry for that reply: 74 tokens for Fable, 174 for Opus and 177 for Sonnet. The next summary read the extended prefix. These account for the remaining 425 cache-creation tokens.

The native option moves the message marker back from the Describe message. It prevents marking the new Describe text; it does not disable the provider's ability to create cache entries at the earlier marker. The previous registry statement that full-mode summaries never write a new cache entry was too broad.

## What this establishes about historical usage

The retained proxy request logs were classified by requested model. At that scan they contained 14,056 message logs, of which 42 requested Claude models. Two Claude requests succeeded, both requesting Haiku; the other Claude requests returned 400 or 401. Their final user messages did not identify any of the 42 as a Describe request. The remaining models and local summary responses cannot establish Claude's consumption.

The native full-mode helper sets `skipTranscript: true`, so its assistant response is not an ordinary agent transcript turn. The inspected failed-telemetry files contained API error/query events but did not contain successful API usage events. These sources do not establish the total Claude summary usage in past sessions or its fraction of those sessions' ordinary work.

The API response also does not state how these token categories count toward a Claude subscription's five-hour or weekly limits. A quota percentage cannot be inferred from the raw counts alone. The GPT quota fit in the earlier investigation does not apply to Claude.

## Evidence and reproduction

- `results.jsonl` contains every measured request's settings, exact response model, response ID, timestamp, latency, stop reason and provider usage. It records generated labels from the synthetic fixture; credentials stay in memory.
- `measure.py` constructs the synthetic requests and writes the results. It reads the access token into memory from the native credential file and does not log it. It refuses an existing output file and returns a failing status if an API request fails.
- Claude Code 2.1.316 source was inspected in `/tmp/cc-env-host-316.gQxRhC/claude-2.1.316.js`. Relevant anchors are `querySource:"agent_summary"`, `skipTranscript:!0,skipCacheWrite:!0`, the `Oi()` prompt builder and `kun()`'s marker calculation. The latter first selects the last eligible message and, when `skipCacheWrite` is true, steps back one eligible message. Full-mode `xu()` inherits `baseParams`; short-mode `Ru()` instead explicitly sets low effort and disables prompt caching.
- The native-format request at `~/.local/state/cliproxyapi/logs/v1-messages-2026-11-01T100006-32e73e17.log` independently shows the first arrangement: its cache marker is on the assistant tool call, while the final user message contains the tool result and Describe text. That log requested GPT and is used only to verify request construction, not to measure Claude consumption.

The recorded run used:

```bash
python3 -I measure.py --output results.jsonl
```

AC was connected at the pre-run check. The measurements left Claude Code settings, the proxy plugin, running sessions and the GrowthBook cache unchanged.
