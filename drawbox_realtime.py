"""DrawBox conversation mode — a live Grok Voice Agent session on the Pi box.

The agent (grok-voice) does the talking; this client does the plumbing and
the policing:

  - streams mic audio up, plays response audio out (full duplex),
  - executes ``draw_coloring_page`` tool calls through the same gated
    pipeline as every other DrawBox flow (``drawbox_core.execute_draw_tool``),
  - runs every input AND output transcript through the deterministic
    interceptor/blocklist, killing the response and speaking the canned
    line on a hit — the LLM never gets authority over pairing, settings,
    or the blocklist,
  - ends the session on idle silence, the session cap, or two blocklist
    strikes.

The protocol is xAI's clone of the OpenAI Realtime API. Event handling is
separated from socket/audio IO (``AgentSession.handle_event``) so the
policy logic is unit-testable without hardware or network.
"""

import asyncio
import base64
import json
import logging
import queue
import threading
import time

import numpy as np

import drawbox_core

log = logging.getLogger("drawbox.realtime")

MIC_RATE = 44100          # the USB mic's only supported rate
AGENT_AUDIO_RATE = 24000  # OpenAI-Realtime-protocol default, pcm16 mono
IDLE_TIMEOUT_S = 45       # no kid speech for this long ends the session
BLOCK_STRIKES_LIMIT = 2   # blocklist hits before the session ends


def resample_to_pcm16(chunk, src_rate=MIC_RATE, dst_rate=AGENT_AUDIO_RATE):
    """Float32 mono/2D audio → little-endian pcm16 bytes at ``dst_rate``.

    Linear interpolation is plenty for speech; the fancy alternative is a
    scipy dependency the Pi doesn't otherwise need.
    """
    mono = chunk[:, 0] if getattr(chunk, "ndim", 1) > 1 else chunk
    n_out = int(len(mono) * dst_rate / src_rate)
    if n_out <= 0:
        return b""
    x_src = np.linspace(0.0, 1.0, num=len(mono), endpoint=False)
    x_dst = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    out = np.interp(x_dst, x_src, mono)
    return (np.clip(out, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class AgentSession:
    """Protocol/policy half of a realtime session.

    ``send`` and ``speak`` are async callables: ``send`` posts one JSON
    event to the socket; ``speak`` plays a deterministic local line
    (cached key or dynamic text) through the VoiceFeedback speaker —
    async so the blocking mpg123 playback runs in an executor and the
    event loop keeps answering websocket pings meanwhile.
    ``enqueue_audio`` / ``clear_audio`` feed the playback queue.
    """

    def __init__(self, send, speak, enqueue_audio, clear_audio):
        self._send = send
        self._speak = speak
        self._enqueue_audio = enqueue_audio
        self._clear_audio = clear_audio
        self.done = False
        self.failed = False          # fatal: config rejected before start
        self.configured = False      # session.updated received
        self.last_activity = time.time()
        self.block_strikes = 0
        self._out_transcripts = {}   # response_id → accumulated text
        self._killed_responses = set()
        self._current_response = None

    async def handle_event(self, event):
        etype = event.get("type", "")
        if etype == "session.updated":
            self.configured = True
        elif etype == "input_audio_buffer.speech_started":
            self.last_activity = time.time()
        elif etype == "conversation.item.input_audio_transcription.completed":
            self.last_activity = time.time()
            await self._check_input(event.get("transcript") or "")
        elif etype == "response.created":
            self._current_response = \
                (event.get("response") or {}).get("id") or \
                event.get("response_id")
        elif etype in ("response.audio.delta", "response.output_audio.delta"):
            if event.get("response_id") not in self._killed_responses:
                try:
                    self._enqueue_audio(base64.b64decode(event.get("delta") or ""))
                except (ValueError, TypeError):
                    pass
        elif etype in ("response.audio_transcript.delta",
                       "response.output_audio_transcript.delta",
                       "response.text.delta",
                       "response.output_text.delta"):
            await self._check_output(event.get("response_id") or "",
                                     event.get("delta") or "")
        elif etype == "response.function_call_arguments.done":
            await self._run_tool(event)
        elif etype == "response.done":
            self._out_transcripts.pop(
                (event.get("response") or {}).get("id"), None)
        elif etype == "error":
            # Post-config errors are recoverable per the docs (the session
            # stays open). Before session.updated, an error means our
            # config was rejected — the session is useless, bail so the
            # caller can fall back to the one-shot flow.
            log.warning("realtime error event: %s", event.get("error"))
            if not self.configured:
                self.failed = True
                self.done = True

    async def _check_input(self, transcript):
        """The kid's words: admin commands first (side effects run in
        core), then the blocklist — same order as every other flow."""
        if not transcript.strip():
            return
        log.info("kid said: %r", transcript[:120])
        hit = drawbox_core.intercept_transcript(transcript)
        if not hit:
            return
        log.info("intercepted (%s) in conversation", hit["action"])
        await self._kill_current_response()
        await self._speak(hit["voice_key"] or hit["say"])
        if hit["action"] == "blocked":
            self._strike()

    async def _check_output(self, response_id, delta):
        """The agent's words, checked as they stream. A hit kills playback
        mid-response; a syllable may escape the speaker — that's the
        physics of streaming, and why conversation mode is opt-in."""
        text = self._out_transcripts.get(response_id, "") + delta
        self._out_transcripts[response_id] = text
        if response_id in self._killed_responses:
            return
        if drawbox_core.safety_mode_enabled() and not drawbox_core.is_safe(text):
            log.warning("agent output blocked: %r", text[-120:])
            self._killed_responses.add(response_id)
            await self._kill_current_response()
            await self._speak("blocked")
            self._strike()

    async def _run_tool(self, event):
        name = event.get("name") or ""
        call_id = event.get("call_id") or ""
        try:
            args = json.loads(event.get("arguments") or "{}")
        except ValueError:
            args = {}
        if name != "draw_coloring_page":
            outcome = {"ok": False, "message": f"Unknown tool {name!r}."}
        else:
            outcome = drawbox_core.execute_draw_tool(args.get("description"))
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": outcome["message"],
            },
        })
        await self._send({"type": "response.create"})

    async def _kill_current_response(self):
        """Stop what the agent is saying: drop unplayed audio locally, mark
        the in-flight response dead so its later deltas are discarded, and
        ask the server to cancel (a no-op in VAD mode per the docs — the
        local drop is the kill that matters)."""
        if self._current_response:
            self._killed_responses.add(self._current_response)
        self._clear_audio()
        try:
            await self._send({"type": "response.cancel"})
        except Exception:
            pass

    def _strike(self):
        self.block_strikes += 1
        if self.block_strikes >= BLOCK_STRIKES_LIMIT:
            log.warning("ending session after %d blocklist strikes",
                        self.block_strikes)
            self.done = True


class _Speaker:
    """Playback thread: drains pcm16 chunks into a sounddevice stream."""

    def __init__(self, sd):
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._stream = sd.OutputStream(samplerate=AGENT_AUDIO_RATE,
                                       channels=1, dtype="int16")
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._stream.start()
        self._thread.start()

    def enqueue(self, data):
        if data:
            self._queue.put(data)

    def clear(self):
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def close(self):
        self._stop.set()
        self._queue.put(b"")
        self._thread.join(timeout=2)
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    def _run(self):
        while not self._stop.is_set():
            data = self._queue.get()
            if not data:
                continue
            try:
                frames = np.frombuffer(data, dtype="<i2").reshape(-1, 1)
                self._stream.write(frames)
            except Exception as e:
                log.warning("playback failed: %s", e)


async def _run_session_async(voice, state):
    import sounddevice as sd
    import websockets

    drawbox_core.apply_api_keys()
    if not drawbox_core.XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY not set")

    config = drawbox_core.realtime_session_config()
    loop = asyncio.get_running_loop()
    mic_chunks = asyncio.Queue()

    def mic_cb(indata, _frames, _time_info, status):
        if status:
            log.debug("mic status: %s", status)
        data = resample_to_pcm16(indata.copy())
        loop.call_soon_threadsafe(mic_chunks.put_nowait, data)

    speaker = _Speaker(sd)
    async with websockets.connect(
            drawbox_core.XAI_REALTIME_URL,
            additional_headers={
                "Authorization": f"Bearer {drawbox_core.XAI_API_KEY}"},
    ) as ws:
        async def send(payload):
            await ws.send(json.dumps(payload))

        def _blocking_speak(key_or_text):
            # Cached keys play instantly; anything else (the pairing
            # message with its one-time code) synthesizes live.
            if key_or_text in drawbox_core.load_scripts()["voice_lines"]:
                voice.play(key_or_text)
            else:
                voice.play_dynamic(key_or_text)

        async def speak(key_or_text):
            # Executor keeps the event loop alive (websocket pings, frame
            # buffering) while mpg123 blocks; the pump still waits, which
            # preserves audio ordering after a moderation kill.
            await loop.run_in_executor(None, _blocking_speak, key_or_text)

        session = AgentSession(send, speak, speaker.enqueue, speaker.clear)
        await send({"type": "session.update", "session": config})
        speaker.start()
        started_at = time.time()
        log.info("conversation session started")
        # Persistent tasks, recreated only after completion: cancelling a
        # pending ws.recv() each loop iteration is forbidden by the
        # websockets library (concurrent recv) and can drop events —
        # tool calls, transcripts, audio (Bugbot, PR #39).
        mic_task = recv_task = None
        try:
            with sd.InputStream(samplerate=MIC_RATE, channels=1,
                                callback=mic_cb):
                while not session.done:
                    now = time.time()
                    if now - started_at > drawbox_core.AGENT_SESSION_MAX_S:
                        log.info("session cap reached")
                        break
                    if now - session.last_activity > IDLE_TIMEOUT_S:
                        log.info("session idle; ending")
                        break
                    if session.configured and not state["started"]:
                        state["started"] = True
                    if mic_task is None:
                        mic_task = asyncio.create_task(mic_chunks.get())
                    if recv_task is None:
                        recv_task = asyncio.create_task(ws.recv())
                    done, _pending = await asyncio.wait(
                        {mic_task, recv_task}, timeout=1.0,
                        return_when=asyncio.FIRST_COMPLETED)
                    if mic_task in done:
                        chunk = mic_task.result()
                        mic_task = None
                        await send({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode(),
                        })
                    if recv_task in done:
                        raw = recv_task.result()  # raises on connection loss
                        recv_task = None
                        try:
                            event = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        await session.handle_event(event)
        finally:
            for task in (mic_task, recv_task):
                if task is not None:
                    task.cancel()
            speaker.close()
    if session.failed:
        raise RuntimeError("xAI rejected the session config")
    log.info("conversation session ended")


def run_session(voice):
    """Run one conversation session, blocking; never raises.

    Returns True when a session actually got configured and ran — even if
    it later died mid-chat (falling back to the one-shot "I'm listening!"
    after minutes of conversation would be jarring; the box speaks the
    error line instead). Returns False only when no session ever started,
    so the caller's one-shot fallback keeps the button alive.
    """
    state = {"started": False}
    try:
        asyncio.run(_run_session_async(voice, state))
        return True
    except Exception as e:
        log.warning("conversation session failed: %s", e)
        if state["started"]:
            try:
                voice.play("error")
            except Exception:
                pass
        return state["started"]
