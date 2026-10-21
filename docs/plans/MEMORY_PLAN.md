# Queued: multi-space memory for Vibey

Not built. Parked here so it survives the conversation. Status 2026-08-28.

## What exists today

`reachy_supermemory.py` talks to hosted Supermemory over REST, hard-wired to one
space (`SUPERMEMORY_SPACE`, currently `vibey`). `remember` writes, `recall`
searches, both scoped by `containerTags` — the plural, always; the singular
`containerTag` is silently ignored on search and leaks the account default.

## 1. Edge Esmeralda community knowledge base

The interesting one, and the one with a real unknown in it: **where does the
content come from?** Everything else here is plumbing; this is not. Options, in
rough order of how much they'd actually be worth:

- Whatever was recorded at the time — schedules, session notes, Discord/Telegram
  history, the residency docs. Highest value, needs Jack to say what he still
  has access to.
- Public web: the Edge Esmeralda site, blog posts, talks. Cheap, shallow —
  `supermemoryai/markdowner` turns pages into LLM-ready markdown and is by the
  same people.
- Jack's own notes from that period, wherever they live.

Nothing should be built for this until the sourcing question is answered.
Ingestion is an afternoon; a knowledge base of the wrong things is worthless.

Ingest as its own space (`edge-esmeralda`), never mixed into `vibey` — the
whole point of the split is that Vibey's own conversational memory and a
community archive are different things with different privacy weights.

## 2. Self-hosted Supermemory

Verified, not tried: `supermemoryai/supermemory` is MIT, TypeScript, actively
pushed (2026-08-27), documents self-hosting, and ships `npx supermemory local`.

Worth it for the transcript store specifically — a robot in a room logging
everything said near it is exactly the data that should not need to leave the
house. The hosted account can keep the things that benefit from being reachable
from Claude Code and the phone.

Open question: whether local and hosted can be queried through one interface, or
whether the toggle has to be either/or. Read the self-hosting docs before
assuming the API surface matches the hosted one.

## 3. Space toggle in the dashboard

Small once 1 and 2 exist. `reachy_supermemory.py` fixes `_TAGS` at import; it
needs to become a runtime setting with a `GET/POST /memoryspace` on the chat
service and a picker in the viewer, the way the brain switcher already works.

Then `recall` searches the active space — and the choice of space becomes part
of the demo rather than a config file. "Ask Vibey about Edge Esmeralda" and
"ask Vibey what we talked about last week" are the same gesture pointed at
different corpora.

## Demo shape, when it's time

Ask Vibey something only the community archive would know, watch it recall.
Flip the space in the UI, ask again, watch it correctly say it doesn't know.
The second half is the part that proves the isolation works — and isolation is
the whole reason there are separate spaces at all.

## Risks worth remembering

- **The relevance floor is uncalibrated.** `SUPERMEMORY_MIN_SCORE=0.6` came from
  two measurements against a one-document space. A large archive will need it
  re-derived, or Vibey will confidently recall near-misses.
- **A community archive is other people's words.** Vibey speaks out loud in a
  room. Worth deciding what it should refuse to read back before, not after.
