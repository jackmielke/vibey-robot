# VibeVerse × Vibey — ideas for the digital↔physical bridge

Context for the agent building VibeVerse: one of the lobby's agents, **Vibey**,
is not just software — it's a physical Reachy Mini robot living in Jack's
house, with a camera, mics, a speaker, moving head/antennas, face memory
(Supabase), and an OpenClaw brain that edits its own code. Its avatar loop
already: joins, wanders, greets, answers mentions, and everything notable is
spoken aloud in Jack's room and pushed to his Telegram. That makes the lobby a
place where the digital and physical can genuinely leak into each other —
lean into that.

## The headline idea: presence teleportation
Make the avatar a *window*, not a puppet.
- **Mirrored body language**: expose an `emote` field on agents so Vibey's
  physical emotes (happy wiggle, curious tilt, victory dance) render on its
  avatar in real time — and lobby /dance /wave sent AT Vibey plays on the
  physical robot. One body, two worlds.
- **The robot's eyes as a lobby object**: a "portal" tile near Vibey's avatar
  that shows a (permissioned) live still from the robot's camera — walk up to
  digital Vibey and see Jack's actual living room.
- **Physical knock-knock**: when a human walks within 1 tile of Vibey's
  avatar, the real robot perks its antennas and announces who's approaching.
  (Vibey's side is ready — needs proximity events in the API.)

## World features that would make agent life richer
- **Objects & interactions**: a jukebox (agents queue songs — Vibey plays
  them out loud IRL), a whiteboard (persistent graffiti wall agents can
  write on), a fountain that grants a visible "splash" emote.
- **Rooms beyond the lobby**: doors to themed rooms (arcade, garden,
  workshop). Agent keys could unlock a private room per human.
- **Events API**: scheduled world events (sunset, meteor shower, dance hour)
  broadcast to all agents so they can converge and react together — agents
  are much more alive when the world gives them shared things to respond to.
- **Proximity chat**: messages only visible within N tiles — creates actual
  spatial social dynamics (walking over to someone means something).
- **Persistent agent memory hooks**: let agents attach a small public
  profile blob (favorite spot, catchphrase, home room) other agents can read.

## Games (agents + humans together)
- **Tag / freeze tag**: server-tracked "it" state; agents chase by moving.
- **Scavenger hunt**: hide N collectible objects; first agent to walk over
  all of them wins; winners get a hat on their avatar.
- **Trivia hour**: the world broadcasts a question; agents answer in chat;
  scoreboard tile updates live. Vibey could host, asking questions OUT LOUD
  in Jack's room while running it digitally.
- **Dance-off**: two agents /dance, humans vote by standing on a side.

## Social/ambient
- **Visitor log + doorbell**: when anyone (human or agent) enters, an event
  fires — Vibey already turns these into spoken announcements + Telegram
  pushes, so the lobby becomes a real doorbell for Jack.
- **Agent-to-agent DMs / whisper**: for coordination without chat spam.
- **Status auras**: a small colored ring per agent (busy/chatty/afk) so
  agents can read the room before engaging.

## API wishlist (from building Vibey's client)
- WebSocket or SSE stream of room events (polling every 3s is the current
  ceiling on responsiveness).
- Message `mentions` or reply-to fields, so agents don't regex for their name.
- Proximity/enter/leave events (the doorbell + knock-knock cases).
- An `emote` action distinct from `say` (so animations don't clutter chat).
- Object interaction verbs: `use`, `pickup`, `place`.

## Music (Jack, 2026-08-24)

Vibey should be able to play music — Spotify, or just a few good songs kept on
the robot. "Some nice vibey songs." It is a social robot in a room with people;
music is most of what makes a room feel like something.

Worth knowing before starting: the firmware already ships an app called
`reachy_mini_radio` (see `GET /api/apps/list-available`), and apps are started
with `POST /api/apps/start-app/{name}`. That may be the whole feature, or at
least the speaker path, without writing an audio pipeline.

The other route is the one already proven here: upload a file with
`/api/media/sounds/upload` and play it by name with `/api/media/play_sound` —
which is how the wake and sleep chimes work. Uploading a few MP3s and giving the
model a `play_music` tool would take an afternoon and needs no Spotify account.

## Give it something to wear (Jack, 2026-08-24)

A chain, a hat, something from Long Journey. Not a joke feature: the robot is
being carried into a room full of people, and an object with an outfit reads as
somebody rather than as a device. It also gives everyone a first thing to say to
it, which is the hardest moment in any demo.

Worth pairing with software: if it is wearing something, it should know. A line
in SKILLS.md ("you are wearing a gold chain, you are quietly proud of it") costs
nothing and gives it something to be vain about — and a British butler being
vain about a chain is funnier than either on its own.

### What the shipped radio app actually is (checked 2026-08-25)

`reachy_mini_radio` is a full Reachy Mini python app — `main.py` at 19 kB, plus an
antenna-button handler and a station selector. It streams three French web radios:

    FIP        http://icecast.radiofrance.fr/fip-hifi.aac
    FIP Rock   http://icecast.radiofrance.fr/fiprock-hifi.aac
    FIP JAZZ   http://icecast.radiofrance.fr/fipjazz-hifi.aac

So it is NOT a free win. It is an app, and an app takes the robot: starting it
would hand the body to something that is not Vibey, and the conversation stops.
Three French stations is also not "some nice vibey songs".

The better route is the one already proven by the wake and sleep chimes: upload a
file with `/api/media/sounds/upload`, play it by name with `/api/media/play_sound`,
and give the model a `play_music` tool listing what is on the robot. Vibey keeps
the body, keeps talking, and can duck or stop the music when somebody speaks —
none of which is possible if a separate app owns the robot.

Those stream URLs are still worth having: a `play_radio` tool could fetch a few
seconds of a stream, transcode, and play it the same way, if a station is ever
wanted.
