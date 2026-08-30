/*
 * DrawBox voice button — Waveshare ESP32-S3-Touch-AMOLED-2.16.
 *
 * One on-screen button. Press it, say what you want ("draw me a
 * dinosaur"), and DrawBox prints it. The box only records audio and
 * POSTs a WAV to the Pi dashboard's /api/voice/generate; Whisper, the
 * safety filter, image generation, and printing all run on the Pi.
 *
 * Serial console (115200, native USB) test hooks:
 *   t  — simulate a button press
 *   d  — dump the last recording as base64 WAV (between marker lines)
 *   s  — print status (state, WiFi, heap, PSRAM)
 */

#include <Arduino.h>
#include <ESPmDNS.h>
#include <WiFi.h>
#include <Wire.h>
#include <driver/i2s.h>

#include "Arduino_GFX_Library.h"
#include "TouchDrvCSTXXX.hpp"
#include "es7210.h"
#include "pin_config.h"
#include "wifi_credentials.h"

// ── AUDIO ────────────────────────────────────────
#define SAMPLE_RATE_HZ 16000
#define I2S_CH I2S_NUM_1
#define DEFAULT_RECORD_SECONDS 8
#define MAX_RECORD_SECONDS 15
#define READ_CHUNK_SAMPLES 480  // 30 ms per i2s_read

// ── HTTP ─────────────────────────────────────────
// Generation takes 10–60 s; the server's gunicorn allows 120 s.
#define HTTP_RESPONSE_TIMEOUT_MS 120000UL
#define HTTP_CONNECT_TIMEOUT_MS 10000UL

// ── UI ───────────────────────────────────────────
// The face IS the button: a big smiley the kid presses. It blinks and
// looks around while idle, opens its mouth with the kid's voice while
// listening, and naps while the Pi draws.
#define BUTTON_CX 240
#define BUTTON_CY 230
#define BUTTON_R 130
#define RESULT_CY 160
#define RESULT_R 100
#define RESULT_OK_SHOW_MS 6000UL
#define RESULT_ERR_SHOW_MS 8000UL

#define C_BG RGB565(10, 12, 24)
#define C_TITLE RGB565(255, 214, 90)
#define C_FACE RGB565(255, 205, 66)
#define C_FACE_RIM RGB565(214, 158, 24)
#define C_FEATURE RGB565(60, 38, 16)
#define C_CHEEK RGB565(255, 140, 120)
#define C_WAVE RGB565(90, 200, 255)
#define C_WAVE_DIM RGB565(36, 84, 128)
#define C_OK RGB565(80, 210, 110)
#define C_ERR RGB565(250, 160, 60)
#define C_DIM RGB565(150, 150, 160)

enum class Face { HAPPY, LISTEN, THINK, JOY, ERR };

enum class AppState { WIFI_CONNECTING, IDLE, LISTENING, THINKING, RESULT };

static const char *stateName(AppState s) {
  switch (s) {
    case AppState::WIFI_CONNECTING: return "WIFI_CONNECTING";
    case AppState::IDLE: return "IDLE";
    case AppState::LISTENING: return "LISTENING";
    case AppState::THINKING: return "THINKING";
    case AppState::RESULT: return "RESULT";
  }
  return "?";
}

Arduino_DataBus *bus = new Arduino_ESP32QSPI(
    LCD_CS, LCD_SCLK, LCD_SDIO0, LCD_SDIO1, LCD_SDIO2, LCD_SDIO3);
Arduino_CO5300 *gfx = new Arduino_CO5300(
    bus, LCD_RESET, 0 /* rotation */, LCD_WIDTH, LCD_HEIGHT, 0, 0, 0, 0);
TouchDrvCST92xx touch;

static AppState state = AppState::WIFI_CONNECTING;
static uint32_t stateSince = 0;
static bool resultOk = false;
static String resultTitle;
static String resultDetail;

// One PSRAM block: 44-byte WAV header followed by PCM, so the serial
// dump and the HTTP body read from the same contiguous bytes.
static uint8_t *wavBuf = nullptr;
static size_t wavCapacity = 0;
static size_t wavLen = 0;  // header + PCM actually recorded

static int recordSeconds = DEFAULT_RECORD_SECONDS;
static uint32_t lastWifiAttempt = 0;
static int16_t lastPeak = 0;

// Below this peak the recording is room tone. Whisper hallucinates
// text from near-silence (it invented Japanese from an empty room and
// printed it), so quiet takes are rejected on-device. Speech at arm's
// length peaks around 2000; ambient measured around 400.
#define QUIET_PEAK 550

// ── SMALL DRAW HELPERS ───────────────────────────

static int textWidth(const char *s, uint8_t size) {
  return (int)strlen(s) * 6 * size;
}

static void drawCentered(const char *s, int y, uint8_t size, uint16_t color) {
  gfx->setTextSize(size);
  gfx->setTextColor(color);
  gfx->setCursor((LCD_WIDTH - textWidth(s, size)) / 2, y);
  gfx->print(s);
}

// Wrap on spaces into centered lines. Kid transcripts are short; three
// lines is plenty before it turns into soup.
static void drawWrapped(const String &text, int y, uint8_t size,
                        uint16_t color, int maxLines) {
  const int maxChars = (LCD_WIDTH - 40) / (6 * size);
  String rest = text;
  for (int line = 0; line < maxLines && rest.length(); line++) {
    String chunk;
    if ((int)rest.length() <= maxChars) {
      chunk = rest;
      rest = "";
    } else {
      int cut = rest.lastIndexOf(' ', maxChars);
      if (cut < maxChars / 2) cut = maxChars;
      chunk = rest.substring(0, cut);
      rest = rest.substring(cut);
      rest.trim();
      if (line == maxLines - 1 && rest.length() && chunk.length() > 3)
        chunk = chunk.substring(0, chunk.length() - 3) + "...";
    }
    drawCentered(chunk.c_str(), y + line * (8 * size + 4), size, color);
  }
}

// ── THE FACE ─────────────────────────────────────
// Angles are Arduino_GFX/PIL convention: 0 at 3 o'clock, clockwise on
// screen. A smile is the 35..145 arc (through the bottom).

static void drawEyes(int cx, int cy, int r, Face f, bool closed,
                     int pupilDX) {
  int ex = r * 35 / 100, ey = r * 22 / 100, er = r * 20 / 100;
  if (f == Face::LISTEN) er = r * 25 / 100;
  // Erase the eye band (stays well inside the face disc).
  gfx->fillRect(cx - ex - er - 4, cy - ey - er - 4,
                2 * (ex + er + 4), 2 * (er + 4), C_FACE);
  for (int s = -1; s <= 1; s += 2) {
    int x = cx + s * ex, y = cy - ey;
    if (f == Face::JOY) {
      gfx->fillArc(x, y + er / 2, er, er - 7, 160, 380, C_FEATURE);
    } else if (closed || f == Face::THINK) {
      gfx->fillRect(x - er, y - 3, 2 * er, 7, C_FEATURE);
    } else {
      gfx->fillCircle(x, y, er, RGB565_WHITE);
      gfx->fillCircle(x + pupilDX, y, er * 45 / 100, C_FEATURE);
    }
  }
}

static void drawMouth(int cx, int cy, int r, Face f, float open) {
  int my = cy + r * 38 / 100;
  int maxRy = r * 30 / 100;
  // The wipe must cover every mouth shape: the joy smile is the widest
  // (0.50r arc at 25 degrees reaches ~0.45r), not the open ellipse.
  int wipeRx = r * 47 / 100 + 3;
  gfx->fillRect(cx - wipeRx, my - maxRy - 3,
                2 * wipeRx, 2 * (maxRy + 3), C_FACE);
  if (f == Face::ERR) {
    gfx->fillRect(cx - r * 30 / 100, my - 4, 2 * (r * 30 / 100), 9,
                  C_FEATURE);
  } else if (open > 0.05f) {
    gfx->fillEllipse(cx, my, r * 22 / 100,
                     r * 6 / 100 + (int)(open * r * 24 / 100), C_FEATURE);
  } else {
    int rr = (f == Face::JOY) ? r * 50 / 100 : r * 46 / 100;
    gfx->fillArc(cx, cy + r / 10, rr, rr - r * 8 / 100,
                 (f == Face::JOY) ? 25 : 35,
                 (f == Face::JOY) ? 155 : 145, C_FEATURE);
  }
}

static void drawFace(int cx, int cy, int r, Face f, float open = 0.0f,
                     bool closed = false, int pupilDX = 0) {
  gfx->fillCircle(cx, cy, r, C_FACE);
  gfx->fillArc(cx, cy, r, r - 7, 0, 360, C_FACE_RIM);
  for (int s = -1; s <= 1; s += 2)
    gfx->fillEllipse(cx + s * r * 62 / 100, cy + r * 18 / 100,
                     r * 11 / 100, r * 8 / 100, C_CHEEK);
  drawEyes(cx, cy, r, f, closed, pupilDX);
  drawMouth(cx, cy, r, f, open);
}

static void drawConnecting() {
  gfx->fillScreen(C_BG);
  drawCentered("DrawBox", 14, 4, C_TITLE);
  drawFace(BUTTON_CX, BUTTON_CY, BUTTON_R, Face::THINK);
  drawCentered("waking up...", 388, 3, RGB565_WHITE);
  drawCentered(WIFI_SSID, 442, 2, C_DIM);
}

static void drawIdle() {
  gfx->fillScreen(C_BG);
  drawCentered("DrawBox", 14, 4, C_TITLE);
  drawFace(BUTTON_CX, BUTTON_CY, BUTTON_R, Face::HAPPY);
  drawCentered("Press me!", 388, 5, RGB565_WHITE);
  drawCentered("then tell me what to draw", 448, 2, C_DIM);
}

static void drawListening() {
  gfx->fillScreen(C_BG);
  drawCentered("I'm listening!", 14, 4, RGB565_WHITE);
  drawFace(BUTTON_CX, BUTTON_CY, BUTTON_R, Face::LISTEN, 0.3f);
  drawCentered("speak now", 448, 2, C_DIM);
}

// One listening animation frame: progress ring sweep, mouth following
// the mic level, and sound-wave arcs rippling on both sides.
static void drawListenFrame(float fraction, float level, uint32_t frame) {
  float deg = fraction * 360.0f;
  if (deg > 1.0f)
    gfx->fillArc(BUTTON_CX, BUTTON_CY, BUTTON_R + 22, BUTTON_R + 11,
                 270, 270 + deg, RGB565_WHITE);
  drawMouth(BUTTON_CX, BUTTON_CY, BUTTON_R, Face::LISTEN, level);
  for (int i = 0; i < 3; i++) {
    uint16_t c = ((frame % 3) == (uint32_t)i) ? C_WAVE : C_WAVE_DIM;
    int rr = BUTTON_R + 35 + i * 15;
    gfx->fillArc(BUTTON_CX, BUTTON_CY, rr + 5, rr, -24 + i * 3, 24 - i * 3, c);
    gfx->fillArc(BUTTON_CX, BUTTON_CY, rr + 5, rr, 156 + i * 3, 204 - i * 3, c);
  }
}

static void drawThinking() {
  gfx->fillScreen(C_BG);
  drawFace(BUTTON_CX, BUTTON_CY, BUTTON_R, Face::THINK);
  drawCentered("Drawing...", 388, 5, C_TITLE);
  drawCentered("this takes a minute", 448, 2, C_DIM);
}

// Orbiting spinner segment around the napping face.
static void drawThinkingTick(uint32_t tick) {
  int prev = ((tick + 11) * 30) % 360;  // where the segment was last frame
  gfx->fillArc(BUTTON_CX, BUTTON_CY, BUTTON_R + 22, BUTTON_R + 11,
               prev, prev + 30, C_BG);
  int start = (tick * 30) % 360;
  gfx->fillArc(BUTTON_CX, BUTTON_CY, BUTTON_R + 22, BUTTON_R + 11,
               start, start + 90, C_TITLE);
}

static void drawResult() {
  gfx->fillScreen(C_BG);
  drawFace(BUTTON_CX, RESULT_CY, RESULT_R, resultOk ? Face::JOY : Face::ERR);
  gfx->fillArc(BUTTON_CX, RESULT_CY, RESULT_R + 24, RESULT_R + 15, 0, 360,
               resultOk ? C_OK : C_ERR);
  drawWrapped(resultTitle, 300, 3, RGB565_WHITE, 3);
  if (resultDetail.length())
    drawWrapped(String("\"") + resultDetail + "\"", 396, 2, C_DIM, 2);
}

static void setState(AppState next) {
  Serial.printf("[state] %s -> %s @%lums\n",
                stateName(state), stateName(next), (unsigned long)millis());
  state = next;
  stateSince = millis();
  switch (state) {
    case AppState::WIFI_CONNECTING: drawConnecting(); break;
    case AppState::IDLE: drawIdle(); break;
    case AppState::LISTENING: drawListening(); break;
    case AppState::THINKING: drawThinking(); break;
    case AppState::RESULT: drawResult(); break;
  }
}

// ── WAV ──────────────────────────────────────────

static void writeWavHeader(uint8_t *dst, uint32_t pcmBytes) {
  uint32_t byteRate = SAMPLE_RATE_HZ * 2;
  uint32_t riffLen = 36 + pcmBytes;
  memcpy(dst, "RIFF", 4);
  memcpy(dst + 4, &riffLen, 4);
  memcpy(dst + 8, "WAVEfmt ", 8);
  uint32_t fmtLen = 16;
  memcpy(dst + 16, &fmtLen, 4);
  uint16_t fmt = 1, channels = 1, blockAlign = 2, bits = 16;
  uint32_t rate = SAMPLE_RATE_HZ;
  memcpy(dst + 20, &fmt, 2);
  memcpy(dst + 22, &channels, 2);
  memcpy(dst + 24, &rate, 4);
  memcpy(dst + 28, &byteRate, 4);
  memcpy(dst + 32, &blockAlign, 2);
  memcpy(dst + 34, &bits, 2);
  memcpy(dst + 36, "data", 4);
  memcpy(dst + 40, &pcmBytes, 4);
}

// ── MIC ──────────────────────────────────────────

static bool micInit() {
  audio_hal_codec_config_t cfg = {};
  cfg.adc_input = AUDIO_HAL_ADC_INPUT_ALL;
  cfg.codec_mode = AUDIO_HAL_CODEC_MODE_ENCODE;
  cfg.i2s_iface.mode = AUDIO_HAL_MODE_SLAVE;
  cfg.i2s_iface.fmt = AUDIO_HAL_I2S_NORMAL;
  cfg.i2s_iface.samples = AUDIO_HAL_16K_SAMPLES;
  cfg.i2s_iface.bits = AUDIO_HAL_BIT_LENGTH_16BITS;

  uint32_t err = ESP_OK;
  err |= es7210_adc_init(&Wire, &cfg);
  err |= es7210_adc_config_i2s(cfg.codec_mode, &cfg.i2s_iface);
  // Per the board schematic the two physical mics are MIC1/MIC2 (they
  // reach the ESP32 on SDOUT1); MIC3/4 is the ES8311 playback loopback
  // for echo cancellation, which we don't use.
  err |= es7210_adc_set_gain(
      (es7210_input_mics_t)(ES7210_INPUT_MIC1 | ES7210_INPUT_MIC2),
      (es7210_gain_value_t)GAIN_33DB);
  err |= es7210_adc_set_gain(
      (es7210_input_mics_t)(ES7210_INPUT_MIC3 | ES7210_INPUT_MIC4),
      (es7210_gain_value_t)GAIN_0DB);
  err |= es7210_adc_ctrl_state(cfg.codec_mode, AUDIO_HAL_CTRL_START);
  if (err != ESP_OK) {
    Serial.println("[mic] ES7210 init failed");
    return false;
  }

  i2s_config_t i2s_config = {};
  i2s_config.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  i2s_config.sample_rate = SAMPLE_RATE_HZ;
  i2s_config.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  // Both slots: mic1 and mic2 arrive interleaved; recordAudio keeps the
  // louder one so a finger over one mic can't mute the box.
  i2s_config.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  i2s_config.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  i2s_config.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  // ~190 ms of stereo cushion: the face animation blocks this loop for
  // tens of ms per frame, and the vendor's 8x64 buffer dropped samples
  // (8 s of audio took 12 s of wall clock, chopping the speech).
  i2s_config.dma_buf_count = 6;
  i2s_config.dma_buf_len = 1024;
  i2s_config.use_apll = false;
  i2s_config.tx_desc_auto_clear = true;
  i2s_config.fixed_mclk = 0;
  i2s_config.mclk_multiple = I2S_MCLK_MULTIPLE_256;
  i2s_config.bits_per_chan = I2S_BITS_PER_CHAN_16BIT;
  i2s_config.chan_mask =
      (i2s_channel_t)(I2S_TDM_ACTIVE_CH0 | I2S_TDM_ACTIVE_CH1);

  i2s_pin_config_t pins = {};
  pins.bck_io_num = PIN_ES7210_BCLK;
  pins.ws_io_num = PIN_ES7210_LRCK;
  pins.data_in_num = PIN_ES7210_DIN;
  pins.mck_io_num = PIN_ES7210_MCLK;
  pins.data_out_num = I2S_PIN_NO_CHANGE;

  if (i2s_driver_install(I2S_CH, &i2s_config, 0, NULL) != ESP_OK ||
      i2s_set_pin(I2S_CH, &pins) != ESP_OK) {
    Serial.println("[mic] i2s driver install failed");
    return false;
  }
  i2s_zero_dma_buffer(I2S_CH);
  return true;
}

// Records both mic slots interleaved, keeps the louder one as mono PCM
// in wavBuf, and returns the PCM byte count.
static size_t recordAudio(int seconds) {
  const size_t monoSamples = (size_t)seconds * SAMPLE_RATE_HZ;
  const size_t stereoSamples = monoSamples * 2;
  int16_t *pcm = (int16_t *)(wavBuf + 44);
  size_t got = 0;
  uint64_t energy[2] = {0, 0};
  int16_t peak[2] = {0, 0};
  int16_t framePeak = 0;
  float level = 0.0f;
  uint32_t lastFrame = 0, frame = 0;

  Serial.printf("[rec] start seconds=%d\n", seconds);
  i2s_zero_dma_buffer(I2S_CH);
  while (got < stereoSamples) {
    size_t want = min((size_t)READ_CHUNK_SAMPLES * 2, stereoSamples - got);
    size_t bytesRead = 0;
    i2s_read(I2S_CH, pcm + got, want * sizeof(int16_t), &bytesRead,
             pdMS_TO_TICKS(200));
    size_t n = bytesRead / sizeof(int16_t);
    for (size_t i = 0; i < n; i++) {
      int16_t v = pcm[got + i];
      int slot = (got + i) & 1;
      energy[slot] += (int32_t)v * v;
      if (v > peak[slot]) peak[slot] = v;
      if (-v > peak[slot]) peak[slot] = (int16_t)-v;
      if (v > framePeak) framePeak = v;
      if (-v > framePeak) framePeak = (int16_t)-v;
    }
    got += n;
    if (millis() - lastFrame > 90) {
      // Fast attack, slow decay: the mouth pops open with the voice and
      // eases shut in pauses.
      float chunkLevel = min(1.0f, (float)framePeak / 9000.0f);
      level = max(chunkLevel, level * 0.75f);
      drawListenFrame((float)got / (float)stereoSamples, level, frame++);
      framePeak = 0;
      lastFrame = millis();
    }
  }
  int hot = energy[1] > energy[0] ? 1 : 0;
  size_t monoGot = got / 2;
  for (size_t i = 0; i < monoGot; i++) pcm[i] = pcm[2 * i + hot];
  size_t pcmBytes = monoGot * sizeof(int16_t);
  writeWavHeader(wavBuf, pcmBytes);
  wavLen = 44 + pcmBytes;
  lastPeak = max(peak[0], peak[1]);
  Serial.printf("[rec] done samples=%u bytes=%u slot=%d peak0=%d peak1=%d\n",
                (unsigned)monoGot, (unsigned)wavLen, hot, peak[0], peak[1]);
  return pcmBytes;
}

// ── HTTP ─────────────────────────────────────────

static IPAddress resolveServer() {
  String host = DRAWBOX_HOST;
  IPAddress ip;
  if (ip.fromString(host)) return ip;
  if (host.endsWith(".local")) {
    String name = host.substring(0, host.length() - 6);
    ip = MDNS.queryHost(name.c_str(), 4000);
    if (ip != IPAddress()) return ip;
    Serial.printf("[http] mDNS lookup failed for %s\n", host.c_str());
    return IPAddress();
  }
  if (WiFi.hostByName(host.c_str(), ip)) return ip;
  return IPAddress();
}

// Pulls one flat field out of the server's JSON without a JSON library —
// the response is a known, flat shape.
static String jsonField(const String &body, const char *key) {
  String pat = String("\"") + key + "\":";
  int i = body.indexOf(pat);
  if (i < 0) return "";
  i += pat.length();
  while (i < (int)body.length() && body[i] == ' ') i++;
  if (i >= (int)body.length()) return "";
  if (body[i] != '"') {
    int e = i;
    while (e < (int)body.length() && body[e] != ',' && body[e] != '}') e++;
    String v = body.substring(i, e);
    v.trim();
    return v;
  }
  i++;
  String out;
  while (i < (int)body.length()) {
    char c = body[i];
    if (c == '\\' && i + 1 < (int)body.length()) {
      char n = body[i + 1];
      out += (n == 'n' || n == 't') ? ' ' : n;
      i += 2;
      continue;
    }
    if (c == '"') break;
    out += c;
    i++;
  }
  return out;
}

// Reads one HTTP response (status line, headers, body) with a deadline.
static bool readHttpResponse(WiFiClient &client, int &status, String &body) {
  uint32_t deadline = millis() + HTTP_RESPONSE_TIMEOUT_MS;
  String statusLine;
  uint32_t tick = 0;
  while (millis() < deadline) {
    if (client.available()) {
      statusLine = client.readStringUntil('\n');
      break;
    }
    if (!client.connected() && !client.available()) return false;
    if (state == AppState::THINKING) drawThinkingTick(tick++);
    delay(100);
  }
  if (!statusLine.startsWith("HTTP/")) return false;
  status = statusLine.substring(9, 12).toInt();
  while (millis() < deadline) {  // headers
    String line = client.readStringUntil('\n');
    line.trim();
    if (!line.length()) break;
  }
  while ((client.connected() || client.available()) && millis() < deadline) {
    if (client.available()) {
      body += (char)client.read();
      if (body.length() > 8192) break;  // voice responses are tiny
    } else {
      delay(20);
    }
  }
  return true;
}

// POST the recorded WAV; fills the result fields. Never throws/blocks
// past the HTTP deadline.
static void sendToDrawBox() {
  IPAddress ip = resolveServer();
  if (ip == IPAddress()) {
    resultOk = false;
    resultTitle = "Can't find DrawBox";
    resultDetail = String("looked for ") + DRAWBOX_HOST;
    return;
  }
  WiFiClient client;
  client.setTimeout(HTTP_CONNECT_TIMEOUT_MS / 1000);
  Serial.printf("[http] POST http://%s:%d/api/voice/generate (%u bytes)\n",
                ip.toString().c_str(), DRAWBOX_PORT, (unsigned)wavLen);
  uint32_t t0 = millis();
  if (!client.connect(ip, DRAWBOX_PORT)) {
    resultOk = false;
    resultTitle = "DrawBox is offline";
    resultDetail = ip.toString() + " didn't answer";
    return;
  }
  client.printf("POST /api/voice/generate HTTP/1.1\r\n"
                "Host: %s:%d\r\n"
                "Authorization: Bearer %s\r\n"
                "Content-Type: audio/wav\r\n"
                "Content-Length: %u\r\n"
                "Connection: close\r\n\r\n",
                DRAWBOX_HOST, DRAWBOX_PORT, DRAWBOX_TOKEN, (unsigned)wavLen);
  for (size_t off = 0; off < wavLen; off += 4096) {
    size_t n = min((size_t)4096, wavLen - off);
    if (client.write(wavBuf + off, n) != n) {
      client.stop();
      resultOk = false;
      resultTitle = "Upload failed";
      resultDetail = "WiFi hiccup - try again";
      return;
    }
  }

  int status = 0;
  String body;
  bool gotReply = readHttpResponse(client, status, body);
  client.stop();
  Serial.printf("[http] status=%d in %.1fs body=%s\n", status,
                (millis() - t0) / 1000.0, body.c_str());
  if (!gotReply) {
    resultOk = false;
    resultTitle = "No answer";
    resultDetail = "DrawBox took too long";
    return;
  }
  if (status == 401) {
    resultOk = false;
    resultTitle = "Not paired";
    resultDetail = "re-pair me with DrawBox";
    return;
  }

  String transcript = jsonField(body, "transcript");
  resultOk = jsonField(body, "ok") == "true";
  if (resultOk) {
    resultTitle = jsonField(body, "message");
    if (!resultTitle.length()) resultTitle = "Here it comes!";
  } else {
    resultTitle = jsonField(body, "error");
    if (!resultTitle.length()) resultTitle = "Something went wrong";
  }
  resultDetail = transcript;
  Serial.printf("[result] ok=%d transcript=\"%s\"\n",
                resultOk ? 1 : 0, transcript.c_str());
}

// Best-effort: match the box's recording window to the dashboard's
// record_seconds setting so both DrawBoxes feel the same.
static void fetchRecordSeconds() {
  IPAddress ip = resolveServer();
  if (ip == IPAddress()) return;
  WiFiClient client;
  if (!client.connect(ip, DRAWBOX_PORT)) return;
  client.printf("GET /api/settings HTTP/1.1\r\n"
                "Host: %s:%d\r\n"
                "Authorization: Bearer %s\r\n"
                "Connection: close\r\n\r\n",
                DRAWBOX_HOST, DRAWBOX_PORT, DRAWBOX_TOKEN);
  uint32_t deadline = millis() + 5000;
  String body;
  while ((client.connected() || client.available()) && millis() < deadline) {
    if (client.available()) body += (char)client.read();
    else delay(10);
    if (body.length() > 8192) break;
  }
  client.stop();
  int v = jsonField(body, "record_seconds").toInt();
  if (v >= 3) {
    // The dashboard allows up to 30 s; the box clamps to its buffer.
    recordSeconds = min(v, MAX_RECORD_SECONDS);
    Serial.printf("[cfg] record_seconds=%d (dashboard said %d)\n",
                  recordSeconds, v);
  }
}

// ── THE ONE FLOW ─────────────────────────────────

static void handlePress() {
  setState(AppState::LISTENING);
  size_t pcmBytes = recordAudio(recordSeconds);
  if (pcmBytes == 0 || lastPeak < QUIET_PEAK) {
    Serial.printf("[rec] too quiet (peak=%d), not uploading\n", lastPeak);
    resultOk = false;
    resultTitle = "I didn't hear anything";
    resultDetail = "come closer and try again";
    setState(AppState::RESULT);
    return;
  }
  setState(AppState::THINKING);
  sendToDrawBox();
  setState(AppState::RESULT);
}

// ── SERIAL TEST HOOKS ────────────────────────────

static void dumpWavBase64() {
  static const char *tbl =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  if (!wavLen) {
    Serial.println("[dump] no recording yet");
    return;
  }
  Serial.printf("BEGIN_WAV_B64 %u\n", (unsigned)wavLen);
  char line[77];
  int col = 0;
  for (size_t i = 0; i < wavLen; i += 3) {
    uint32_t v = (uint32_t)wavBuf[i] << 16;
    if (i + 1 < wavLen) v |= (uint32_t)wavBuf[i + 1] << 8;
    if (i + 2 < wavLen) v |= wavBuf[i + 2];
    line[col++] = tbl[(v >> 18) & 63];
    line[col++] = tbl[(v >> 12) & 63];
    line[col++] = (i + 1 < wavLen) ? tbl[(v >> 6) & 63] : '=';
    line[col++] = (i + 2 < wavLen) ? tbl[v & 63] : '=';
    if (col >= 76) {
      line[col] = 0;
      Serial.println(line);
      col = 0;
    }
  }
  if (col) {
    line[col] = 0;
    Serial.println(line);
  }
  Serial.println("END_WAV_B64");
}

static void printStatus() {
  Serial.printf("[status] state=%s wifi=%d ip=%s rssi=%d heap=%u psram=%u "
                "record_s=%d lastwav=%u\n",
                stateName(state), WiFi.status(),
                WiFi.localIP().toString().c_str(), WiFi.RSSI(),
                (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram(),
                recordSeconds, (unsigned)wavLen);
}

static void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 't' && (state == AppState::IDLE || state == AppState::RESULT)) {
      Serial.println("[serial] simulated press");
      handlePress();
    } else if (c == 'd') {
      dumpWavBase64();
    } else if (c == 's') {
      printStatus();
    }
  }
}

// ── SETUP / LOOP ─────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[boot] DrawBox voice button");

  Wire.begin(IIC_SDA, IIC_SCL);

  if (!gfx->begin()) Serial.println("[boot] gfx->begin() failed");
  bus->writeC8D8(0x36, 0xA0);  // vendor panel orientation, matches touch map
  gfx->fillScreen(C_BG);
  gfx->setBrightness(200);

  touch.setPins(TP_RST, TP_INT);
  if (!touch.begin(Wire, CST92XX_SLAVE_ADDRESS, IIC_SDA, IIC_SCL)) {
    Serial.println("[boot] touch offline");
  } else {
    touch.setMaxCoordinates(LCD_WIDTH, LCD_HEIGHT);
    touch.setSwapXY(true);
    touch.setMirrorXY(true, false);
    Serial.printf("[boot] touch %s\n", touch.getModelName());
  }

  // Stereo capture briefly needs 2x the mono PCM size in the same block.
  wavCapacity = 44 + (size_t)MAX_RECORD_SECONDS * SAMPLE_RATE_HZ * 2 * 2;
  wavBuf = (uint8_t *)ps_malloc(wavCapacity);
  Serial.printf("[boot] psram=%u wavbuf=%s\n", (unsigned)ESP.getPsramSize(),
                wavBuf ? "ok" : "FAILED");
  if (!wavBuf) {
    drawCentered("PSRAM missing", 220, 3, C_ERR);
    while (true) delay(1000);
  }

  if (!micInit()) {
    drawCentered("Mic init failed", 220, 3, C_ERR);
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWifiAttempt = millis();
  setState(AppState::WIFI_CONNECTING);
}

void loop() {
  handleSerial();

  switch (state) {
    case AppState::WIFI_CONNECTING:
      if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[wifi] up ip=%s rssi=%d\n",
                      WiFi.localIP().toString().c_str(), WiFi.RSSI());
        MDNS.begin("drawbox-button");
        fetchRecordSeconds();
        setState(AppState::IDLE);
      } else if (millis() - lastWifiAttempt > 20000) {
        Serial.println("[wifi] retrying");
        WiFi.disconnect();
        WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
        lastWifiAttempt = millis();
      }
      delay(100);
      break;

    case AppState::IDLE: {
      if (WiFi.status() != WL_CONNECTED) {
        setState(AppState::WIFI_CONNECTING);
        lastWifiAttempt = millis();
        break;
      }
      // Keep the face alive: blink now and then, glance around.
      static uint32_t nextBlink = 0, blinkUntil = 0, nextGlance = 0;
      static int pupilDX = 0;
      uint32_t now = millis();
      bool redrawEyes = false;
      if (now > nextBlink) {
        blinkUntil = now + 140;
        nextBlink = now + 2300 + (esp_random() % 2200);
        redrawEyes = true;
      }
      if (blinkUntil && now > blinkUntil) {
        blinkUntil = 0;
        redrawEyes = true;
      }
      if (now > nextGlance) {
        static const int looks[] = {-9, 0, 9, 0};
        pupilDX = looks[esp_random() % 4];
        nextGlance = now + 1800 + (esp_random() % 2600);
        redrawEyes = true;
      }
      if (redrawEyes)
        drawEyes(BUTTON_CX, BUTTON_CY, BUTTON_R, Face::HAPPY,
                 blinkUntil != 0, pupilDX);

      int16_t x[2], y[2];
      uint8_t n = touch.getPoint(x, y, 1);
      if (n) {
        long dx = x[0] - BUTTON_CX, dy = y[0] - BUTTON_CY;
        // Generous hit zone: the face plus its surrounding ring.
        long hitR = BUTTON_R + 30;
        if (dx * dx + dy * dy <= hitR * hitR) {
          Serial.printf("[touch] press at %d,%d\n", x[0], y[0]);
          handlePress();
        }
      }
      delay(30);
      break;
    }

    case AppState::RESULT: {
      uint32_t showFor = resultOk ? RESULT_OK_SHOW_MS : RESULT_ERR_SHOW_MS;
      int16_t x[2], y[2];
      if (millis() - stateSince > showFor || touch.getPoint(x, y, 1))
        setState(AppState::IDLE);
      delay(30);
      break;
    }

    default:
      // LISTENING and THINKING run synchronously inside handlePress.
      delay(10);
      break;
  }
}
