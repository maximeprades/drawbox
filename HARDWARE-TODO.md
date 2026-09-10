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
option (`stt_provider`), Gemini catalog models (`google/gemini-3.1-flash-lite-image`,
3:4 `imageConfig`), and opt-in Conversation Mode (Grok Voice Agent;
Pi client `drawbox_realtime.py`; ESP32 client `realtime_client.h` in
firmware v2.0.0 — compiled, never run on the device). Full design:
`ARCHITECTURE.md`, and the PR descriptions.

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

## 2. Flash the ESP32 (v2.0.0)

```bash
./firmware/esp32_amoled_button/build.sh flash /dev/cu.usbmodemXXXX
```

Serial (115200): `s` must report `ver=2.0.0 ... conv=0` (conversation
mode off on the server for now).

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
sane transcript in the journal, and that "um"s are stripped. Uses the one
AI Gateway key (model `spacexai/grok-stt`). Switch back if the owner
prefers Whisper.

## 5. THE SPIKE — heap headroom for the on-box conversation client

Serial `w` (WiFi must be up). It opens a TLS websocket to the AI Gateway
realtime route (`wss://ai-gateway.vercel.sh/v4/ai/realtime-model`) next to
the live UI and prints heap at each stage. The probe sends a bogus token,
so a 401 "Invalid client secret" after "wss connected" is the expected
end; the heap numbers are what matter. TLS is verified against the
pinned Let's Encrypt roots (`gateway_ca.h`); "wss refused" right away
with no heap dip means the certificate chain changed — check
`openssl s_client -connect ai-gateway.vercel.sh:443` from the Mac.

- **GO**: `minfree` stays above ~20 KB through "wss connected" and
  "after traffic" → the client in `realtime_client.h` has the room it
  was designed for; go to step 7b.
- **NO-GO**: minfree dips below ~20 KB or the box resets → the client
  will fail its connect and fall back to the one-shot flow every time.
  The plan's fallback is a Pi-proxied audio bridge (box streams plain
  TCP to the Pi, the Pi holds the TLS session). Do not start building it
  without the owner's call — just report the numbers.

## 6. Gemini speed benchmark + imageConfig verification

1. Settings → Image Model → `google/gemini-3.1-flash-lite-image`;
   generate 3 pages by voice or dashboard.
2. Compare per-model `duration_s` in `/api/analytics` against
   `google/gemini-3.1-flash-image-preview`. Report both averages.
3. Check the aspect ratio: `curl -s http://drawbox.local:5000/api/last-image -H "Authorization: Bearer <token>" | file -` or fetch
   `~/.drawbox/last_generated.png` — content region 3:4-ish means the
   gateway honors `imageConfig`; square means it ignored it (then remove
   the `providerOptions.google` block from `_GOOGLE_IMAGE_KWARGS` in
   `drawbox_core.py` to keep the code honest, and say so in the report).
4. Owner judges line quality; if Lite looks good, they may want it as the
   default catalog model.

## 7. Conversation mode live test (Pi box)

Requires the AI Gateway key in Settings → API Keys (the only key).

1. Settings → Conversation Mode → On. Press the button and talk to it.
2. Verify: agent replies in Grok's voice; asking for a drawing triggers a
   print (journal shows "agent draw started"); "authorize" mid-chat is
   intercepted deterministically (pairing card prints, canned line
   plays); a blocked word from the kid gets the canned redirect; the
   session ends after ~45 s of silence or 5 min.
3. **If the Gateway rejects the session config** (journal: "conversation
   session failed" / "realtime error event"), the field shape in
   `drawbox_core.realtime_session_config()` is the single place to fix —
   compare against the AI SDK `RealtimeModelV4SessionConfig` type
   (`packages/provider/src/realtime-model/v4/` in vercel/ai). The route,
   model id, token TTL, and subprotocol auth were verified live against
   the Gateway with a bogus token (401 "Invalid client secret"); the
   session config and event flow are the pieces built against the spec
   without a live round-trip. Empty `inputAudioTranscription` /
   `outputAudioTranscription` objects are the first suspects if the
   Gateway complains — try a provider model name (e.g. `grok-transcribe`)
   or drop them.
4. Leave conversation mode OFF when done unless the owner says otherwise
   (Gateway passes through xAI's rate: $0.08/min of audio plus $0.004 per
   text input message; audio appends and tool outputs are not billed).

## 7b. Conversation mode live test (ESP32 box) — only after 7 passes

The firmware client was compiled but never ran against the Gateway, so
expect to iterate here with the serial console open.

1. Conversation Mode still On. Wait one heartbeat (`s` shows `conv=1`).
2. Tap (or serial `t`). Expected serial: `[rt] session: agent 24000 Hz`,
   `[rt] wss connected`, `[rt] session configured`; screen says
   "Let's chat!" / "hold to stop". Talk; the agent answers through the
   speaker with the mouth moving. Say a drawing request: `[rt] draw ->
   200 {...}` and a page prints. Say "authorize": `[rt] intercepted
   (authorize)` and the pairing clip plays. Long press ends the session
   (serial `x` does too).
3. Watch heap in `s` mid-session; below ~15 KB free means the audio
   scratch or the TLS record buffers are starving — shrink
   `RT_MIC_CHUNK_SAMPLES` or lower `RT_RING_SECONDS` before anything else.
4. Failure map: `wss connect failed` → certificate/heap (step 5);
   `error event` before `session configured` → same config fix as step
   7.3 (the box parrots the server's config, so fix it on the Pi only);
   choppy playback → the loop is starving the DMA, log the time between
   `[rt]` lines; the agent hearing itself → raise `RT_ECHO_GUARD_MS`.
5. The one-shot fallback must still work when the mode is off: toggle it
   off, wait a heartbeat, tap — "I'm listening!" flow as before.

## 8. Cleanup

- Mark any remaining plan todos, report everything to the owner.
- `git rm HARDWARE-TODO.md`, remove the "PENDING HARDWARE WORK" section
  from `AGENTS.md`, commit as "remove hardware runbook (completed)".
