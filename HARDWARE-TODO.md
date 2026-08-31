# Hardware runbook — pending on-device work (temporary file)

**For the agent:** this branch shipped big software changes that could not
be verified on hardware (the owner was away; no Pi on the network, no
ESP32 on USB). When the owner is home and the devices are powered, run
this file top to bottom. Delete it — and the pointer section in
`AGENTS.md` — when everything below is verified. Report results to the
owner as you go, especially the spike verdict (step 5) and the benchmark
numbers (step 6).

Context: the changes are VAD early-stop (both boxes), personalized
acknowledgments via a two-phase ESP32 flow (firmware v1.6.0), an xAI STT
option (`stt_provider`), Gemini speed presets (`nano-banana-fast`,
3:4 `imageConfig`), and opt-in Conversation Mode (Grok Voice Agent;
Pi client `drawbox_realtime.py`; ESP32 client NOT built yet — gated on
the heap spike). Full design: `ARCHITECTURE.md`, and the PR description.

## 0. Preconditions

- Check PR #39 for Bugbot comments newer than commit 35c07cb and address
  them before merging (the five findings on 9a8916c are already fixed).
- This branch is merged (or checked out) and you are on the home network.
- `ping drawbox.local` answers; `ls /dev/cu.usbmodem*` shows the ESP32.
- Ask the owner before restarting anything if the kids are mid-drawing.

## 1. Deploy the server + Pi daemon FIRST

Order matters: new firmware expects the new endpoints; old firmware works
fine against the new server, not vice versa.

```bash
./deploy-web.sh                 # copies drawbox*.py, installs websockets, restarts both services
```

Then sync the Pi's update clone: `ssh pi@drawbox.local "cd ~/drawbox-repo && git pull origin main"`.

Verify: `curl http://drawbox.local:5000/api/status` answers; the dashboard
Settings page shows "Speech-to-Text", "Reply Style", and "Conversation
Mode (beta)"; generate one page from the dashboard. Note: the deploy
clears the voice cache — the daemon re-synthesizes lines on first start,
so give it a minute before judging silence.

## 2. Flash the ESP32 (v1.6.0)

```bash
./firmware/esp32_amoled_button/build.sh flash /dev/cu.usbmodemXXXX
```

Serial (115200): `s` must report `ver=1.6.0`.

## 3. Verify the one-shot flow end to end (both boxes)

- Pi box: press, say "a happy dinosaur", stop talking — recording should
  end ~1.5 s later (journal logs "speech ended ... stopping early"), a
  personalized ack plays ("Ooh, a happy dinosaur!"), page prints.
- ESP32: serial `t`, speak — same early stop, ack clip plays within a few
  seconds (serial shows the two-phase flow: POST → ack → result poll).
- Say "authorize" at the **ESP32** box: pairing must now work from it
  (code card prints; spoken message plays). This was new wiring.

## 4. STT swap check (optional but quick)

Settings → Speech-to-Text → "Grok STT", generate by voice once, confirm a
sane transcript in the journal, and that "um"s are stripped. Needs the
xAI key in Settings → API Keys. Switch back if the owner prefers Whisper.

## 5. THE SPIKE — go/no-go for the on-box conversation client

Serial `w` (WiFi must be up). It opens a TLS websocket to api.x.ai next
to the live UI and prints heap at each stage.

- **GO**: `minfree` stays above ~20 KB through "wss connected" and
  "after traffic" → Phase 4 (full ESP32 conversation client, firmware
  v2.0.0) is buildable as designed. Tell the owner; that build is a
  separate session of work.
- **NO-GO**: minfree dips below ~20 KB or the box resets → the plan's
  fallback is a Pi-proxied audio bridge (box streams plain TCP to the Pi,
  the Pi holds the TLS session). Do not start building either variant
  without the owner's call — just report the numbers.

## 6. Gemini speed benchmark + imageConfig verification

1. Settings → Image Model → "Nano Banana 2 Lite" (`nano-banana-fast`);
   generate 3 pages by voice or dashboard.
2. Compare per-model `duration_s` in `/api/analytics` against
   `nano-banana`. Report both averages.
3. Check the aspect ratio: `curl -s http://drawbox.local:5000/api/last-image -H "Authorization: Bearer <token>" | file -` or fetch
   `~/.drawbox/last_generated.png` — content region 3:4-ish means the
   gateway honors `imageConfig`; square means it ignored it (then remove
   the `providerOptions.google` block from `_GOOGLE_IMAGE_KWARGS` in
   `drawbox_core.py` to keep the code honest, and say so in the report).
4. Owner judges line quality; if Lite looks good, they may want it as the
   default preset.

## 7. Conversation mode live test (Pi box)

Requires the xAI key in Settings → API Keys.

1. Settings → Conversation Mode → On. Press the button and talk to it.
2. Verify: agent replies in Grok's voice; asking for a drawing triggers a
   print (journal shows "agent draw started"); "authorize" mid-chat is
   intercepted deterministically (pairing card prints, canned line
   plays); a blocked word from the kid gets the canned redirect; the
   session ends after ~45 s of silence or 5 min.
3. **If xAI rejects the session config** (journal: "conversation session
   failed"), the field shape in `drawbox_core.realtime_session_config()`
   is the single place to fix — compare against docs.x.ai's realtime
   session.update schema. This is the one piece built against docs
   without a live round-trip.
4. Leave conversation mode OFF when done unless the owner says otherwise
   (it costs ~$0.05/min while chatting).

## 8. Cleanup

- Mark any remaining plan todos, report everything to the owner.
- `git rm HARDWARE-TODO.md`, remove the "PENDING HARDWARE WORK" section
  from `AGENTS.md`, commit as "remove hardware runbook (completed)".
