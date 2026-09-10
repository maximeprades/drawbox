// Conversation mode on the ESP32 box: one live Grok Voice Agent session
// over the Vercel AI Gateway, the same session the Pi runs in
// drawbox_realtime.py. The agent does the talking; this file does the
// plumbing and the policing, mirroring AgentSession on the Pi:
//
//   - streams mic audio up (input-audio-append), plays audio-delta out,
//   - forwards draw_coloring_page tool calls to POST /api/agent/draw
//     (the gated pipeline; the box never touches the image model),
//   - sends every kid transcript to POST /api/agent/intercept and the
//     agent's streamed words to POST /api/agent/moderate — the blocklist
//     and the admin commands live on the Pi, not here,
//   - ends on idle silence, the session cap, two blocklist strikes, a
//     long press, or a dropped socket.
//
// Wire protocol: the AI SDK's normalized realtime events (kebab-case
// types, camelCase fields). The box gets the WSS URL, the
// Sec-WebSocket-Protocol header (carrying the 5-minute client secret) and
// the shared session config from POST /api/realtime/token; it never sees
// the long-lived Gateway key.
//
// Audio: the codecs run at 16 kHz (SAMPLE_RATE_HZ); the agent speaks and
// listens at the rate in the session config (24 kHz). Linear resampling
// bridges the two in both directions. The speaker and the mics share a
// box with no echo cancellation, so mic frames are dropped while the
// agent's audio is playing (walkie-talkie, no barge-in) — otherwise the
// server VAD hears the agent talk to itself.
//
// Included from the sketch after the audio/HTTP helpers it uses. The
// ArduinoWebsockets include is unconditional on purpose: Arduino's
// library discovery only follows plain #include lines, so an
// __has_include guard would quietly compile the box without the library
// (and without conversation mode). build.sh installs it.
#pragma once

#include <ArduinoWebsockets.h>
#include <mbedtls/base64.h>

#include "gateway_ca.h"

// One input-audio-append per 60 ms of mic audio (the Pi sends 50 ms).
#define RT_MIC_CHUNK_SAMPLES 960
// = drawbox_realtime.IDLE_TIMEOUT_S / BLOCK_STRIKES_LIMIT.
#define RT_IDLE_TIMEOUT_MS 45000UL
#define RT_BLOCK_STRIKES_LIMIT 2
#define RT_DEFAULT_MAX_SESSION_S 300
#define RT_DEFAULT_AGENT_RATE_HZ 24000
// Unplayed agent audio waiting for the speaker (PSRAM). The server streams
// faster than real time, so this is the backlog of one long answer.
#define RT_RING_SECONDS 40
// The agent's transcript is re-checked at most this often while it streams.
#define RT_MODERATE_INTERVAL_MS 1000UL
// Mic stays muted this long after the last write into the TX DMA, which
// itself holds ~380 ms of audio still to be played.
#define RT_ECHO_GUARD_MS 700UL
// The TX DMA depth in ms: the mouth keeps moving while it drains.
#define RT_DMA_TAIL_MS 380UL
#define RT_HTTP_TIMEOUT_MS 15000UL
#define RT_TOKEN_BODY_MAX (24u * 1024u)
#define RT_SMALL_BODY_MAX 4096u
#define RT_OUT_TEXT_MAX 4096u
#define RT_KILLED_IDS 4
#define RT_SEEN_ITEMS 8

// ── JSON (flat, escape-aware) ────────────────────
// The sketch's jsonField() is fine for the Pi's tiny replies; the Gateway
// events carry escaped JSON-in-JSON (tool arguments) and \u escapes in
// transcripts, and the token reply nests the session object. These
// helpers work on a NUL-terminated buffer and never copy the payload.

static const char *rtFindKey(const char *body, const char *key) {
  char pat[48];
  int n = snprintf(pat, sizeof(pat), "\"%s\"", key);
  if (n <= 0 || n >= (int)sizeof(pat)) return nullptr;
  const char *p = body;
  while ((p = strstr(p, pat)) != nullptr) {
    const char *q = p + n;
    while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
    if (*q == ':') {
      q++;
      while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
      return q;
    }
    p += n;
  }
  return nullptr;
}

// Bounds of the raw (still escaped) contents of a string value.
static bool rtJsonStringSpan(const char *body, const char *key,
                             const char **start, size_t *len) {
  const char *v = rtFindKey(body, key);
  if (!v || *v != '"') return false;
  v++;
  const char *e = v;
  while (*e && *e != '"') {
    if (*e == '\\' && e[1]) e++;
    e++;
  }
  if (*e != '"') return false;
  *start = v;
  *len = (size_t)(e - v);
  return true;
}

static void rtAppendUtf8(String &out, uint32_t cp) {
  if (cp < 0x80) {
    out += (char)cp;
  } else if (cp < 0x800) {
    out += (char)(0xC0 | (cp >> 6));
    out += (char)(0x80 | (cp & 0x3F));
  } else if (cp < 0x10000) {
    out += (char)(0xE0 | (cp >> 12));
    out += (char)(0x80 | ((cp >> 6) & 0x3F));
    out += (char)(0x80 | (cp & 0x3F));
  } else {
    out += (char)(0xF0 | (cp >> 18));
    out += (char)(0x80 | ((cp >> 12) & 0x3F));
    out += (char)(0x80 | ((cp >> 6) & 0x3F));
    out += (char)(0x80 | (cp & 0x3F));
  }
}

static uint32_t rtHex4(const char *s) {
  uint32_t v = 0;
  for (int i = 0; i < 4; i++) {
    char c = s[i];
    v <<= 4;
    if (c >= '0' && c <= '9') v |= (uint32_t)(c - '0');
    else if (c >= 'a' && c <= 'f') v |= (uint32_t)(c - 'a' + 10);
    else if (c >= 'A' && c <= 'F') v |= (uint32_t)(c - 'A' + 10);
    else return 0xFFFFFFFF;
  }
  return v;
}

// Decodes JSON string escapes into UTF-8. Flask emits every non-ASCII
// character as \uXXXX, so accents in a French transcript depend on this.
static String rtJsonUnescape(const char *s, size_t len) {
  String out;
  out.reserve(len);
  size_t i = 0;
  while (i < len) {
    char c = s[i];
    if (c != '\\' || i + 1 >= len) {
      out += c;
      i++;
      continue;
    }
    char n = s[i + 1];
    i += 2;
    switch (n) {
      case 'n': out += '\n'; break;
      case 't': out += '\t'; break;
      case 'r': out += '\r'; break;
      case 'b': out += '\b'; break;
      case 'f': out += '\f'; break;
      case 'u': {
        if (i + 4 > len) return out;
        uint32_t cp = rtHex4(s + i);
        if (cp == 0xFFFFFFFF) return out;
        i += 4;
        if (cp >= 0xD800 && cp <= 0xDBFF && i + 6 <= len && s[i] == '\\' &&
            s[i + 1] == 'u') {
          uint32_t lo = rtHex4(s + i + 2);
          if (lo >= 0xDC00 && lo <= 0xDFFF) {
            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
            i += 6;
          }
        }
        rtAppendUtf8(out, cp);
        break;
      }
      default: out += n; break;  // \" \\ \/
    }
  }
  return out;
}

static String rtJsonString(const char *body, const char *key) {
  const char *s;
  size_t len;
  if (!rtJsonStringSpan(body, key, &s, &len)) return "";
  return rtJsonUnescape(s, len);
}

// Bounds of a nested object value, braces included, strings respected.
static bool rtJsonObjectSpan(const char *body, const char *key,
                             const char **start, size_t *len) {
  const char *v = rtFindKey(body, key);
  if (!v || *v != '{') return false;
  int depth = 0;
  bool inStr = false;
  const char *p = v;
  for (; *p; p++) {
    char c = *p;
    if (inStr) {
      if (c == '\\' && p[1]) p++;
      else if (c == '"') inStr = false;
      continue;
    }
    if (c == '"') inStr = true;
    else if (c == '{') depth++;
    else if (c == '}' && --depth == 0) {
      *start = v;
      *len = (size_t)(p - v + 1);
      return true;
    }
  }
  return false;
}

static int rtJsonInt(const char *body, const char *key, int fallback) {
  const char *v = rtFindKey(body, key);
  if (!v || !(*v == '-' || (*v >= '0' && *v <= '9'))) return fallback;
  return atoi(v);
}

static String rtJsonEscape(const String &s) {
  String out;
  out.reserve(s.length() + 8);
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if ((uint8_t)c < 0x20) {
          char buf[8];
          snprintf(buf, sizeof(buf), "\\u%04x", (unsigned)(uint8_t)c);
          out += buf;
        } else {
          out += c;
        }
    }
  }
  return out;
}

// ── HTTP (JSON POST to the Pi) ───────────────────

static bool rtHttpPost(const char *path, const String &json, int &status,
                       String &body, size_t maxBody) {
  status = 0;
  body = "";
  IPAddress ip = resolveServer();
  if (ip == IPAddress()) return false;
  WiFiClient client;
  client.setTimeout(HTTP_CONNECT_TIMEOUT_MS / 1000);
  if (!client.connect(ip, DRAWBOX_PORT)) {
    forgetServerIp();
    return false;
  }
  client.printf("POST %s HTTP/1.1\r\n"
                "Host: %s:%d\r\n"
                "Authorization: Bearer %s\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: %u\r\n"
                "Connection: close\r\n\r\n",
                path, DRAWBOX_HOST, DRAWBOX_PORT, DRAWBOX_TOKEN,
                (unsigned)json.length());
  client.write((const uint8_t *)json.c_str(), json.length());

  uint32_t deadline = millis() + RT_HTTP_TIMEOUT_MS;
  while (millis() < deadline && !client.available()) {
    if (!client.connected()) {
      client.stop();
      return false;
    }
    lv_timer_handler();
    delay(5);
  }
  String statusLine = client.readStringUntil('\n');
  if (!statusLine.startsWith("HTTP/") || statusLine.length() > 256) {
    client.stop();
    return false;
  }
  status = statusLine.substring(9, 12).toInt();
  long contentLength = -1;
  while (millis() < deadline) {
    String line = client.readStringUntil('\n');
    if (line.length() > 1024) {
      client.stop();
      return false;
    }
    line.trim();
    if (!line.length()) break;
    if (line.startsWith("Content-Length:") || line.startsWith("content-length:"))
      contentLength = line.substring(15).toInt();
  }
  if (contentLength > (long)maxBody) {
    client.stop();
    return false;
  }
  if (contentLength > 0) body.reserve((size_t)contentLength);
  while ((client.connected() || client.available()) && millis() < deadline) {
    if (contentLength >= 0 && (long)body.length() >= contentLength) break;
    int avail = client.available();
    if (avail > 0) {
      char buf[256];
      int want = avail < (int)sizeof(buf) ? avail : (int)sizeof(buf);
      int n = client.read((uint8_t *)buf, want);
      if (n <= 0) break;
      body.concat(buf, n);
      if (body.length() > maxBody) break;
    } else {
      lv_timer_handler();
      delay(5);
    }
  }
  client.stop();
  return true;
}

// ── AUDIO PLUMBING ───────────────────────────────

// Linear interpolation with a one-sample carry, so chunk boundaries are
// seamless. The phase is an exact fraction (source position = pos/dst in
// samples, reduced by the gcd), so 960 samples in are exactly 1440 out
// and a 5-minute session does not drift.
struct RtResampler {
  int16_t prev;
  uint32_t pos;  // source position * dst, into [prev, in[0], in[1], ...]
  uint32_t src, dst;

  void reset(uint32_t s, uint32_t d) {
    prev = 0;
    pos = 0;
    uint32_t a = s, b = d;
    while (b) {
      uint32_t t = a % b;
      a = b;
      b = t;
    }
    src = s / a;
    dst = d / a;
  }

  // out must hold ceil(n * dst / src) samples.
  size_t run(const int16_t *in, size_t n, int16_t *out) {
    if (!n) return 0;
    size_t o = 0;
    uint32_t end = (uint32_t)n * dst;
    while (pos < end) {
      uint32_t i = pos / dst;
      int32_t a = i == 0 ? prev : in[i - 1];
      int32_t b = in[i];
      int32_t frac = (int32_t)(pos % dst);
      out[o++] = (int16_t)(a + (int32_t)(((int64_t)(b - a) * frac) / (int32_t)dst));
      pos += src;
    }
    prev = in[n - 1];
    pos -= end;
    return o;
  }
};

struct RtRing {
  int16_t *buf;
  size_t cap, head, tail, count;

  void clear() { head = tail = count = 0; }

  void push(const int16_t *src, size_t n) {
    if (n > cap - count) {
      Serial.printf("[rt] playback ring full, dropping %u samples\n",
                    (unsigned)(n - (cap - count)));
      n = cap - count;
    }
    for (size_t i = 0; i < n; i++) {
      buf[head] = src[i];
      head = (head + 1) % cap;
    }
    count += n;
  }

  size_t peek(int16_t *dst, size_t n) const {
    if (n > count) n = count;
    size_t t = tail;
    for (size_t i = 0; i < n; i++) {
      dst[i] = buf[t];
      t = (t + 1) % cap;
    }
    return n;
  }

  void consume(size_t n) {
    if (n > count) n = count;
    tail = (tail + n) % cap;
    count -= n;
  }
};

// ArduinoWebsockets replaces its TCP client with a fresh WiFiClientSecure
// for wss:// URLs and only forwards setCACert() to it, so the root is
// pinned through the library's own hook (gateway_ca.h).

struct RtSession {
  websockets::WebsocketsClient ws;
  bool configured = false;  // session-updated seen
  bool done = false;
  bool failed = false;      // error before session-updated
  bool wsClosed = false;
  uint32_t startedAt = 0;
  uint32_t lastActivity = 0;
  uint32_t maxSessionMs = RT_DEFAULT_MAX_SESSION_S * 1000UL;
  uint32_t agentRate = RT_DEFAULT_AGENT_RATE_HZ;
  int strikes = 0;

  String currentResponse;
  String killed[RT_KILLED_IDS];
  int killedNext = 0;
  String seenItems[RT_SEEN_ITEMS];
  int seenNext = 0;
  String outResponse;  // response whose transcript outText holds
  String outText;
  size_t moderatedLen = 0;
  uint32_t lastModerate = 0;

  // Mic: stereo capture -> louder slot mono -> agent rate -> base64 JSON.
  int16_t *micStereo = nullptr;
  size_t micFill = 0;  // stereo frames in micStereo
  int16_t *micMono = nullptr;
  int16_t *micUp = nullptr;
  char *micMsg = nullptr;
  size_t micMsgCap = 0;
  RtResampler up;

  // Speaker: base64 -> agent rate -> 16 kHz -> ring -> I2S.
  uint8_t *scratch = nullptr;
  size_t scratchCap = 0;
  RtResampler down;
  RtRing ring;
  uint32_t lastPlayoutMs = 0;
  int16_t playPeak = 0;
  float mouthLevel = 0.0f;
  uint32_t lastFrame = 0;
  bool kidTalking = false;
  bool drawing = false;
};

static RtSession *rt = nullptr;

static bool rtAlloc(RtSession &s) {
  size_t upCap = RT_MIC_CHUNK_SAMPLES * 4;  // room for any rate up to 64 kHz
  s.micStereo = (int16_t *)ps_malloc(RT_MIC_CHUNK_SAMPLES * 2 * sizeof(int16_t));
  s.micMono = (int16_t *)ps_malloc(RT_MIC_CHUNK_SAMPLES * sizeof(int16_t));
  s.micUp = (int16_t *)ps_malloc(upCap * sizeof(int16_t));
  s.micMsgCap = 64 + (upCap * sizeof(int16_t) * 4) / 3 + 8;
  s.micMsg = (char *)ps_malloc(s.micMsgCap);
  s.ring.cap = (size_t)RT_RING_SECONDS * SAMPLE_RATE_HZ;
  s.ring.buf = (int16_t *)ps_malloc(s.ring.cap * sizeof(int16_t));
  s.ring.clear();
  return s.micStereo && s.micMono && s.micUp && s.micMsg && s.ring.buf;
}

static void rtFree(RtSession &s) {
  free(s.micStereo);
  free(s.micMono);
  free(s.micUp);
  free(s.micMsg);
  free(s.scratch);
  free(s.ring.buf);
  s.micStereo = s.micMono = s.micUp = nullptr;
  s.micMsg = nullptr;
  s.scratch = nullptr;
  s.ring.buf = nullptr;
}

static bool rtScratch(RtSession &s, size_t need) {
  if (s.scratchCap >= need) return true;
  uint8_t *p = (uint8_t *)ps_malloc(need);
  if (!p) return false;
  free(s.scratch);
  s.scratch = p;
  s.scratchCap = need;
  return true;
}

static bool rtSend(const char *json, size_t len) {
  if (!rt || rt->wsClosed) return false;
  bool ok = rt->ws.send(json, len);
  if (!ok) Serial.println("[rt] ws send failed");
  return ok;
}

static bool rtSend(const String &json) { return rtSend(json.c_str(), json.length()); }

static bool rtSend(const char *json) { return rtSend(json, strlen(json)); }

static bool rtSpeakerBusy() {
  if (!rt) return false;
  if (rt->ring.count) return true;
  return millis() - rt->lastPlayoutMs < RT_ECHO_GUARD_MS;
}

static void rtPumpMic() {
  RtSession &s = *rt;
  size_t wantFrames = RT_MIC_CHUNK_SAMPLES - s.micFill;
  size_t got = 0;
  // Zero ticks: take whatever the DMA has, never block the socket pump.
  i2s_read(I2S_CH, s.micStereo + s.micFill * 2,
           wantFrames * 2 * sizeof(int16_t), &got, 0);
  s.micFill += got / (2 * sizeof(int16_t));
  if (s.micFill < RT_MIC_CHUNK_SAMPLES) return;
  s.micFill = 0;
  if (rtSpeakerBusy()) return;

  uint64_t energy[2] = {0, 0};
  for (size_t i = 0; i < RT_MIC_CHUNK_SAMPLES; i++) {
    int32_t l = s.micStereo[2 * i], r = s.micStereo[2 * i + 1];
    energy[0] += (uint64_t)(l * l);
    energy[1] += (uint64_t)(r * r);
  }
  int hot = energy[1] > energy[0] ? 1 : 0;
  for (size_t i = 0; i < RT_MIC_CHUNK_SAMPLES; i++)
    s.micMono[i] = s.micStereo[2 * i + hot];

  size_t n = s.up.run(s.micMono, RT_MIC_CHUNK_SAMPLES, s.micUp);
  static const char head[] = "{\"type\":\"input-audio-append\",\"audio\":\"";
  size_t off = sizeof(head) - 1;
  memcpy(s.micMsg, head, off);
  size_t olen = 0;
  if (mbedtls_base64_encode((unsigned char *)s.micMsg + off, s.micMsgCap - off - 3,
                            &olen, (const unsigned char *)s.micUp,
                            n * sizeof(int16_t)) != 0) {
    return;
  }
  off += olen;
  s.micMsg[off++] = '"';
  s.micMsg[off++] = '}';
  rtSend(s.micMsg, off);
}

// Tops the TX DMA (~380 ms deep) up from the ring without blocking: zero
// ticks, stop as soon as the driver takes less than offered.
static void rtPumpPlayback() {
  RtSession &s = *rt;
  static int16_t mono[256];
  static int16_t frame[256 * 2];
  for (int pass = 0; pass < 8 && s.ring.count; pass++) {
    size_t n = s.ring.peek(mono, 256);
    for (size_t i = 0; i < n; i++) {
      int16_t v = mono[i];
      frame[2 * i] = v;
      frame[2 * i + 1] = v;
      if (v > s.playPeak) s.playPeak = v;
      if (-v > s.playPeak) s.playPeak = (int16_t)-v;
    }
    size_t written = 0;
    i2s_write(I2S_CH, frame, n * 2 * sizeof(int16_t), &written, 0);
    size_t frames = written / (2 * sizeof(int16_t));
    if (frames) {
      s.ring.consume(frames);
      s.lastPlayoutMs = millis();
    }
    if (frames < n) break;
  }
}

static void rtAnimate() {
  RtSession &s = *rt;
  if (millis() - s.lastFrame < 90) return;
  s.lastFrame = millis();
  if (s.ring.count || millis() - s.lastPlayoutMs < RT_DMA_TAIL_MS) {
    float chunkLevel = min(1.0f, (float)s.playPeak / 9000.0f);
    s.mouthLevel = max(chunkLevel, s.mouthLevel * 0.75f);
    int idx = s.mouthLevel < 0.18f ? 0 : s.mouthLevel < 0.4f ? 1
              : s.mouthLevel < 0.7f ? 2 : 3;
    lv_img_set_src(imgMouth, MOUTH_LEVELS[idx]);
  } else {
    s.mouthLevel = 0.0f;
    lv_img_set_src(imgMouth, s.kidTalking ? &img_mouth_o1 : &img_mouth_smile);
  }
  s.playPeak = 0;
}

// ── POLICY (mirrors drawbox_realtime.AgentSession) ─

static bool rtIsKilled(const String &responseId) {
  if (!responseId.length()) return false;
  for (int i = 0; i < RT_KILLED_IDS; i++)
    if (rt->killed[i] == responseId) return true;
  return false;
}

static void rtMarkKilled(const String &responseId) {
  if (!responseId.length() || rtIsKilled(responseId)) return;
  rt->killed[rt->killedNext] = responseId;
  rt->killedNext = (rt->killedNext + 1) % RT_KILLED_IDS;
}

// Stop what the agent is saying: drop unplayed audio, mark the response
// dead so later deltas are discarded, ask the server to cancel.
static void rtKillCurrentResponse() {
  rtMarkKilled(rt->currentResponse);
  rt->ring.clear();
  i2s_zero_dma_buffer(I2S_CH);
  rtSend("{\"type\":\"response-cancel\"}");
}

static void rtStrike() {
  rt->strikes++;
  if (rt->strikes >= RT_BLOCK_STRIKES_LIMIT) {
    Serial.printf("[rt] ending session after %d blocklist strikes\n",
                  rt->strikes);
    rt->done = true;
  }
}

// Speak the server's exact line (dynamic clip by cache key), else the
// cached voice line, else the generic error line.
static void rtSpeak(const String &ackKey, const String &voiceKey) {
  if (ackKey.length() == 12) {
    int16_t *pcm = nullptr;
    uint32_t samples = 0;
    String path = String("/api/voice/clip?k=") + ackKey;
    if (fetchWavFromPath(path.c_str(), &pcm, &samples)) {
      playPcm(pcm, samples);
      free(pcm);
      rt->lastPlayoutMs = millis();
      return;
    }
  }
  playLine(voiceKey.length() ? voiceKey.c_str() : "error");
  rt->lastPlayoutMs = millis();
}

// The kid's words: admin commands first (side effects run on the Pi),
// then the blocklist — same order as every other flow.
static void rtCheckInput(const String &transcript) {
  if (!transcript.length()) return;
  Serial.printf("[rt] kid said: %s\n", transcript.c_str());
  int status = 0;
  String body;
  if (!rtHttpPost("/api/agent/intercept",
                  "{\"transcript\":\"" + rtJsonEscape(transcript) + "\"}",
                  status, body, RT_SMALL_BODY_MAX)) {
    Serial.println("[rt] intercept unreachable");
    return;
  }
  if (status == 403) {
    // Conversation mode was switched off mid-chat.
    Serial.println("[rt] conversation mode turned off, ending");
    conversationMode = false;
    rt->done = true;
    return;
  }
  String action = rtJsonString(body.c_str(), "action");
  if (!action.length()) return;
  Serial.printf("[rt] intercepted (%s)\n", action.c_str());
  rtKillCurrentResponse();
  rtSpeak(rtJsonString(body.c_str(), "ack_key"),
          rtJsonString(body.c_str(), "voice_key"));
  if (action == "blocked") rtStrike();
}

// The agent's words, checked as they stream. A hit kills playback
// mid-response; a syllable may escape the speaker — that's the physics
// of streaming plus one LAN round trip, and why conversation mode is
// opt-in.
static void rtModerate() {
  RtSession &s = *rt;
  if (s.outText.length() == s.moderatedLen) return;
  s.moderatedLen = s.outText.length();
  s.lastModerate = millis();
  if (rtIsKilled(s.outResponse)) return;
  int status = 0;
  String body;
  if (!rtHttpPost("/api/agent/moderate",
                  "{\"text\":\"" + rtJsonEscape(s.outText) + "\"}", status,
                  body, RT_SMALL_BODY_MAX)) {
    return;
  }
  if (status == 403) {
    conversationMode = false;
    s.done = true;
    return;
  }
  const char *v = rtFindKey(body.c_str(), "blocked");
  if (v && strncmp(v, "true", 4) == 0) {
    Serial.println("[rt] agent output blocked");
    rtMarkKilled(s.outResponse);
    rtKillCurrentResponse();
    rtSpeak("", "blocked");
    rtStrike();
  }
}

static void rtCheckOutput(const String &responseId, const String &delta) {
  RtSession &s = *rt;
  if (responseId != s.outResponse) {
    s.outResponse = responseId;
    s.outText = "";
    s.moderatedLen = 0;
  }
  s.outText += delta;
  if (s.outText.length() > RT_OUT_TEXT_MAX) {
    s.outText = s.outText.substring(s.outText.length() - RT_OUT_TEXT_MAX / 2);
    s.moderatedLen = 0;
  }
  if (rtIsKilled(responseId)) return;
  if (millis() - s.lastModerate >= RT_MODERATE_INTERVAL_MS) rtModerate();
}

// draw_coloring_page → POST /api/agent/draw. The arguments string is
// already the {"description": ...} object the endpoint wants, so it is
// forwarded verbatim; the reply is the tool output, forwarded verbatim.
static void rtRunTool(const char *json) {
  String name = rtJsonString(json, "name");
  String callId = rtJsonString(json, "callId");
  String args = rtJsonString(json, "arguments");
  String outcome;
  if (name != "draw_coloring_page") {
    outcome = "{\"ok\":false,\"message\":\"Unknown tool.\"}";
  } else {
    rt->drawing = true;
    lv_label_set_text(lblHint, "sending your drawing...");
    int status = 0;
    if (!args.startsWith("{")) args = "{}";
    if (!rtHttpPost("/api/agent/draw", args, status, outcome, RT_SMALL_BODY_MAX) ||
        status == 0 || !outcome.startsWith("{")) {
      outcome = "{\"ok\":false,\"message\":\"DrawBox could not be reached.\"}";
    }
    Serial.printf("[rt] draw -> %d %s\n", status, outcome.c_str());
    lv_label_set_text(lblHint, "hold to stop");
  }
  String item = "{\"type\":\"conversation-item-create\",\"item\":{"
                "\"type\":\"function-call-output\",\"callId\":\"" +
                rtJsonEscape(callId) + "\",\"name\":\"" + rtJsonEscape(name) +
                "\",\"output\":\"" + rtJsonEscape(outcome) + "\"}}";
  rtSend(item);
  rtSend("{\"type\":\"response-create\"}");
}

static void rtPushAudioDelta(const char *json) {
  RtSession &s = *rt;
  if (rtIsKilled(rtJsonString(json, "responseId"))) return;
  const char *b64;
  size_t b64len;
  if (!rtJsonStringSpan(json, "delta", &b64, &b64len) || !b64len) return;
  size_t pcmCap = (b64len / 4) * 3 + 4;
  // Decoded agent-rate PCM followed by the 16 kHz version of it.
  size_t downCap = (pcmCap / sizeof(int16_t)) * SAMPLE_RATE_HZ / s.agentRate + 8;
  if (!rtScratch(s, pcmCap + downCap * sizeof(int16_t))) {
    Serial.println("[rt] scratch alloc failed");
    return;
  }
  size_t olen = 0;
  if (mbedtls_base64_decode(s.scratch, pcmCap, &olen, (const unsigned char *)b64,
                            b64len) != 0) {
    return;
  }
  int16_t *pcm = (int16_t *)s.scratch;
  int16_t *down = (int16_t *)(s.scratch + pcmCap);
  size_t n = s.down.run(pcm, olen / sizeof(int16_t), down);
  s.ring.push(down, n);
}

static void rtHandleEvent(const char *json) {
  RtSession &s = *rt;
  String type = rtJsonString(json, "type");
  if (type == "audio-delta") {
    rtPushAudioDelta(json);
  } else if (type == "session-updated") {
    if (!s.configured) Serial.println("[rt] session configured");
    s.configured = true;
    lv_label_set_text(lblBig, "");
  } else if (type == "session-created") {
    // informational
  } else if (type == "speech-started") {
    s.lastActivity = millis();
    s.kidTalking = true;
    lv_img_set_src(imgEyes, &img_eyes_wide);
  } else if (type == "speech-stopped") {
    s.kidTalking = false;
    lv_img_set_src(imgEyes, &img_eyes_open);
  } else if (type == "input-transcription-completed") {
    s.lastActivity = millis();
    // xAI can emit more than one completed event per item; without dedup
    // one blocked utterance burns both strikes.
    String itemId = rtJsonString(json, "itemId");
    if (itemId.length()) {
      for (int i = 0; i < RT_SEEN_ITEMS; i++)
        if (s.seenItems[i] == itemId) return;
      s.seenItems[s.seenNext] = itemId;
      s.seenNext = (s.seenNext + 1) % RT_SEEN_ITEMS;
    }
    rtCheckInput(rtJsonString(json, "transcript"));
  } else if (type == "response-created") {
    s.currentResponse = rtJsonString(json, "responseId");
  } else if (type == "audio-transcript-delta" || type == "text-delta") {
    rtCheckOutput(rtJsonString(json, "responseId"), rtJsonString(json, "delta"));
  } else if (type == "function-call-arguments-done") {
    rtRunTool(json);
  } else if (type == "response-done") {
    if (rtJsonString(json, "responseId") == s.outResponse) rtModerate();
    s.drawing = false;
  } else if (type == "error") {
    Serial.printf("[rt] error event: %s (%s)\n",
                  rtJsonString(json, "message").c_str(),
                  rtJsonString(json, "code").c_str());
    // Before session-updated an error means the config was rejected;
    // the session is useless, bail so the caller falls back to one-shot.
    if (!s.configured) {
      s.failed = true;
      s.done = true;
    }
  }
}

static void rtOnMessage(websockets::WebsocketsMessage msg) {
  if (!rt || !msg.isText()) return;
  rtHandleEvent(msg.c_str());
}

static void rtOnEvent(websockets::WebsocketsEvent ev, String data) {
  if (!rt) return;
  if (ev == websockets::WebsocketsEvent::ConnectionClosed) {
    Serial.printf("[rt] ws closed %s\n", data.c_str());
    rt->wsClosed = true;
  } else if (ev == websockets::WebsocketsEvent::ConnectionOpened) {
    Serial.println("[rt] wss connected");
  }
}

// ── THE SESSION ──────────────────────────────────

// Runs one conversation, blocking. Returns true when a session actually
// got configured and ran — even if it later died mid-chat (the box speaks
// the error line then). Returns false only when no session ever started,
// so the caller's one-shot fallback keeps the button alive — exactly
// drawbox_realtime.run_session's contract.
static bool runConversation() {
  int status = 0;
  String body;
  if (!rtHttpPost("/api/realtime/token", "{}", status, body, RT_TOKEN_BODY_MAX)) {
    Serial.println("[rt] token: DrawBox unreachable");
    return false;
  }
  if (status == 403) {
    Serial.println("[rt] conversation mode is off on the server");
    conversationMode = false;
    return false;
  }
  if (status != 200) {
    Serial.printf("[rt] token: HTTP %d %s\n", status, body.c_str());
    return false;
  }
  String url = rtJsonString(body.c_str(), "url");
  String protocolHeader = rtJsonString(body.c_str(), "protocol_header");
  const char *sess;
  size_t sessLen;
  if (!url.startsWith("wss://") || !protocolHeader.length() ||
      !rtJsonObjectSpan(body.c_str(), "session", &sess, &sessLen)) {
    Serial.println("[rt] token reply missing url/protocol_header/session");
    return false;
  }
  String sessionUpdate = "{\"type\":\"session-update\",\"config\":";
  sessionUpdate.concat(sess, sessLen);
  sessionUpdate += "}";
  int maxS = rtJsonInt(body.c_str(), "max_session_s", RT_DEFAULT_MAX_SESSION_S);
  // Both audio formats in the shared config carry the same rate; the
  // first "rate" from the session object on is it (no other key in the
  // reply is called rate).
  int rate = rtJsonInt(sess, "rate", RT_DEFAULT_AGENT_RATE_HZ);
  body = "";

  RtSession *s = new RtSession();
  if (!s || !rtAlloc(*s)) {
    Serial.println("[rt] PSRAM alloc failed");
    if (s) {
      rtFree(*s);
      delete s;
    }
    return false;
  }
  rt = s;
  s->maxSessionMs = (uint32_t)(maxS > 0 ? maxS : RT_DEFAULT_MAX_SESSION_S) * 1000UL;
  s->agentRate = rate > 0 ? (uint32_t)rate : RT_DEFAULT_AGENT_RATE_HZ;
  s->up.reset(SAMPLE_RATE_HZ, s->agentRate);
  s->down.reset(s->agentRate, SAMPLE_RATE_HZ);
  Serial.printf("[rt] session: agent %u Hz, cap %us, heap=%u psram=%u\n",
                (unsigned)s->agentRate, (unsigned)(s->maxSessionMs / 1000),
                (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram());

  conversationStopRequested = false;
  setState(AppState::CONVERSING);
  lv_label_set_text(lblBig, "connecting...");
  lv_refr_now(NULL);

  s->ws.setCACert(GATEWAY_CA_PEM);
  s->ws.addHeader("Sec-WebSocket-Protocol", protocolHeader);
  s->ws.onMessage(rtOnMessage);
  s->ws.onEvent(rtOnEvent);
  uint32_t t0 = millis();
  if (!s->ws.connect(url)) {
    Serial.printf("[rt] wss connect failed after %lums (heap=%u)\n",
                  (unsigned long)(millis() - t0), (unsigned)ESP.getFreeHeap());
    rt = nullptr;
    rtFree(*s);
    delete s;
    return false;
  }
  rtSend(sessionUpdate);
  sessionUpdate = "";
  i2s_zero_dma_buffer(I2S_CH);
  s->startedAt = s->lastActivity = millis();
  s->lastPlayoutMs = millis() - RT_ECHO_GUARD_MS;

  while (!s->done) {
    s->ws.poll();
    if (!s->ws.available()) {
      s->wsClosed = true;
      break;
    }
    rtPumpMic();
    rtPumpPlayback();
    rtAnimate();
    if (conversationStopRequested) {
      Serial.println("[rt] stopped by long press");
      break;
    }
    if (millis() - s->startedAt > s->maxSessionMs) {
      Serial.println("[rt] session cap reached");
      break;
    }
    if (millis() - s->lastActivity > RT_IDLE_TIMEOUT_MS) {
      Serial.println("[rt] session idle, ending");
      break;
    }
    if (millis() - lastHeartbeat > 60000) sendHeartbeat();
    while (Serial.available()) {
      char c = Serial.read();
      if (c == 'x') conversationStopRequested = true;
      else if (c == 's') printStatus();
    }
    lv_timer_handler();
    delay(2);
  }

  bool started = s->configured;
  bool crashed = s->wsClosed && !s->done && started;
  s->ws.close();
  rt = nullptr;
  rtFree(*s);
  delete s;
  i2s_zero_dma_buffer(I2S_CH);
  Serial.printf("[rt] session ended (started=%d crashed=%d heap=%u)\n",
                started ? 1 : 0, crashed ? 1 : 0, (unsigned)ESP.getFreeHeap());
  if (crashed) playLine("error");
  return started;
}
