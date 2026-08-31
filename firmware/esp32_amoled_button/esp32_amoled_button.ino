/*
 * DrawBox voice button — Waveshare ESP32-S3-Touch-AMOLED-2.16.
 *
 * One on-screen buddy. Tap the face, say what you want ("draw me a
 * dinosaur"), and DrawBox prints it. The box only records audio and
 * POSTs a WAV to the Pi dashboard's /api/voice/generate; Whisper, the
 * safety filter, image generation, and printing all run on the Pi.
 *
 * UI is LVGL 8.4 over Arduino_GFX. The face is pre-rendered on the host
 * (gen_face_assets.py) into anti-aliased bitmaps; the firmware only
 * swaps eye/mouth frames, so nothing is vector-drawn at runtime except
 * LVGL's own anti-aliased arcs.
 *
 * Serial console (115200, native USB) test hooks:
 *   t  — simulate a button press
 *   d  — dump the last recording as base64 WAV between marker lines
 *   p  — dump a screenshot of the live UI as base64 RGB565
 *   s  — print status (state, WiFi, heap, PSRAM)
 */

#include <Arduino.h>
#include <ESPmDNS.h>
#include <WiFi.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <lvgl.h>

#include "Arduino_GFX_Library.h"
#include "TouchDrvCSTXXX.hpp"
#include "es7210.h"
#include "es8311.h"
#include "face_assets.h"
#include "pin_config.h"
#include "wifi_credentials.h"

// ── AUDIO ────────────────────────────────────────
#define SAMPLE_RATE_HZ 16000
#define I2S_CH I2S_NUM_1
#define DEFAULT_RECORD_SECONDS 8
#define MAX_RECORD_SECONDS 15
#define READ_CHUNK_SAMPLES 480

// Below this peak the recording is room tone. Whisper hallucinates text
// from near-silence (it invented Japanese from an empty room and printed
// it), so quiet takes are rejected on-device. Speech at arm's length
// peaks around 2000; ambient measured around 400.
#define QUIET_PEAK 550

// ── HTTP ─────────────────────────────────────────
// Generation takes 10–60 s; the server's gunicorn allows 120 s.
#define HTTP_RESPONSE_TIMEOUT_MS 120000UL
#define HTTP_CONNECT_TIMEOUT_MS 10000UL

// ── UI ───────────────────────────────────────────
#define RESULT_OK_SHOW_MS 6000UL
#define RESULT_ERR_SHOW_MS 8000UL
#define BG_COLOR lv_color_make(10, 12, 24)
#define TITLE_COLOR lv_color_make(255, 214, 90)
#define DIM_COLOR lv_color_make(150, 150, 160)
#define OK_COLOR lv_color_make(80, 210, 110)
#define ERR_COLOR lv_color_make(250, 160, 60)
#define WAVE_COLOR lv_color_make(90, 200, 255)

enum class AppState { WIFI_CONNECTING, IDLE, LISTENING, THINKING, RESULT };

struct Note {
  float freq;  // Hz; 0 = rest
  uint16_t ms;
};

// Named before any function that mentions it: the .ino preprocessor
// hoists prototypes above later type definitions.
struct VoiceLine {
  char key[20];
  uint8_t variant;
  int16_t *pcm;
  uint32_t samples;
};

#define VOICE_CACHE_CAP 24
#define VOICE_MAX_SECONDS 12
#define VOICE_MAX_BYTES \
  ((uint32_t)SAMPLE_RATE_HZ * VOICE_MAX_SECONDS * sizeof(int16_t))
#define VOICE_FETCH_TIMEOUT_MS 15000UL
#define VOICE_JOKE_WAIT_MS 2000UL

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
static String resultVoiceKey;
static volatile bool pressRequested = false;
static volatile bool dismissRequested = false;

static VoiceLine voiceCache[VOICE_CACHE_CAP];
static int voiceCacheCount = 0;
static int thinkingVariants = 1;
static int jokeCount = 0;
static struct {
  int16_t *pcm;
  uint32_t samples;
} jokeSlot = {nullptr, 0};

// One PSRAM block: 44-byte WAV header followed by PCM, so the serial
// dump and the HTTP body read from the same contiguous bytes.
static uint8_t *wavBuf = nullptr;
static size_t wavCapacity = 0;
static size_t wavLen = 0;

static int recordSeconds = DEFAULT_RECORD_SECONDS;
static uint32_t lastWifiAttempt = 0;
static int16_t lastPeak = 0;

// ── LVGL GLUE (vendor-demo shapes) ───────────────

static lv_disp_draw_buf_t draw_buf;

// CO5300 wants even start and odd end coordinates on both axes.
static void rounder_cb(lv_disp_drv_t *, lv_area_t *area) {
  if (area->x1 % 2 != 0) area->x1--;
  if (area->y1 % 2 != 0) area->y1--;
  if (area->x2 % 2 == 0) area->x2++;
  if (area->y2 % 2 == 0) area->y2++;
}

static void flush_cb(lv_disp_drv_t *disp, const lv_area_t *area,
                     lv_color_t *color_p) {
  uint32_t w = area->x2 - area->x1 + 1;
  uint32_t h = area->y2 - area->y1 + 1;
  gfx->draw16bitRGBBitmap(area->x1, area->y1, (uint16_t *)&color_p->full,
                          w, h);
  lv_disp_flush_ready(disp);
}

static void touch_read_cb(lv_indev_drv_t *, lv_indev_data_t *data) {
  int16_t x[2], y[2];
  if (touch.getPoint(x, y, 1)) {
    data->state = LV_INDEV_STATE_PR;
    data->point.x = x[0];
    data->point.y = y[0];
  } else {
    data->state = LV_INDEV_STATE_REL;
  }
}

static void lvgl_tick_cb(void *) { lv_tick_inc(2); }

// ── WIDGETS ──────────────────────────────────────

static lv_obj_t *lblTitle, *lblBig, *lblHint;
static lv_obj_t *imgDisc, *imgEyes, *imgMouth, *imgPupilL, *imgPupilR;
static lv_obj_t *arcRing, *spinner;
static lv_obj_t *waves[6];

static const lv_img_dsc_t *const MOUTH_LEVELS[] = {
    &img_mouth_o1, &img_mouth_o2, &img_mouth_o3, &img_mouth_o4};

static void setPupils(bool shown, int dx) {
  if (!shown) {
    lv_obj_add_flag(imgPupilL, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(imgPupilR, LV_OBJ_FLAG_HIDDEN);
    return;
  }
  lv_obj_clear_flag(imgPupilL, LV_OBJ_FLAG_HIDDEN);
  lv_obj_clear_flag(imgPupilR, LV_OBJ_FLAG_HIDDEN);
  int py = FACE_CY_PX - EYE_DY_PX - PUPIL_SIZE_PX / 2;
  lv_obj_set_pos(imgPupilL,
                 FACE_CX_PX - EYE_DX_PX - PUPIL_SIZE_PX / 2 + dx, py);
  lv_obj_set_pos(imgPupilR,
                 FACE_CX_PX + EYE_DX_PX - PUPIL_SIZE_PX / 2 + dx, py);
}

static void setFace(const lv_img_dsc_t *eyes, const lv_img_dsc_t *mouth,
                    bool pupils) {
  lv_img_set_src(imgEyes, eyes);
  lv_img_set_src(imgMouth, mouth);
  setPupils(pupils, 0);
}

static void waveOpaExec(void *obj, int32_t v) {
  lv_obj_set_style_opa((lv_obj_t *)obj, v, 0);
}

static void showWaves(bool shown) {
  for (int i = 0; i < 6; i++) {
    if (shown) {
      lv_obj_clear_flag(waves[i], LV_OBJ_FLAG_HIDDEN);
      lv_anim_t a;
      lv_anim_init(&a);
      lv_anim_set_var(&a, waves[i]);
      lv_anim_set_exec_cb(&a, waveOpaExec);
      lv_anim_set_values(&a, LV_OPA_20, LV_OPA_COVER);
      lv_anim_set_time(&a, 500);
      lv_anim_set_playback_time(&a, 500);
      lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
      lv_anim_set_delay(&a, (i % 3) * 160);
      lv_anim_start(&a);
    } else {
      lv_anim_del(waves[i], waveOpaExec);
      lv_obj_add_flag(waves[i], LV_OBJ_FLAG_HIDDEN);
    }
  }
}

static lv_obj_t *makeLabel(const lv_font_t *font, lv_color_t color,
                           lv_align_t align, int oy) {
  lv_obj_t *l = lv_label_create(lv_scr_act());
  lv_obj_set_style_text_font(l, font, 0);
  lv_obj_set_style_text_color(l, color, 0);
  lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
  lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
  lv_obj_set_width(l, 460);
  lv_obj_align(l, align, 0, oy);
  return l;
}

static void onTap(lv_event_t *) {
  Serial.println("[touch] tap");
  if (state == AppState::IDLE) {
    pressRequested = true;
  } else if (state == AppState::RESULT) {
    dismissRequested = true;
  }
}

static void blinkTimerCb(lv_timer_t *t) {
  static bool closed = false;
  if (state != AppState::IDLE) {
    // applyState already set the right face for the new state; restoring
    // open eyes here would stomp the listening/thinking expression.
    closed = false;
    return;
  }
  closed = !closed;
  if (closed) {
    lv_img_set_src(imgEyes, &img_eyes_closed);
    setPupils(false, 0);
    lv_timer_set_period(t, 140);
  } else {
    lv_img_set_src(imgEyes, &img_eyes_open);
    setPupils(true, 0);
    lv_timer_set_period(t, 2200 + (esp_random() % 2400));
  }
}

static void glanceTimerCb(lv_timer_t *t) {
  if (state != AppState::IDLE) return;
  static const int looks[] = {-9, 0, 9, 0};
  setPupils(true, looks[esp_random() % 4]);
  lv_timer_set_period(t, 1800 + (esp_random() % 2600));
}

static void buildUi() {
  lv_obj_t *scr = lv_scr_act();
  lv_obj_set_style_bg_color(scr, BG_COLOR, 0);
  lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_add_event_cb(scr, onTap, LV_EVENT_CLICKED, NULL);

  // Progress/result ring and the thinking spinner sit behind the face.
  arcRing = lv_arc_create(scr);
  lv_obj_set_size(arcRing, 420, 420);
  lv_obj_align(arcRing, LV_ALIGN_CENTER, 0, FACE_CY_PX - 240);
  lv_arc_set_rotation(arcRing, 270);
  lv_arc_set_bg_angles(arcRing, 0, 360);
  lv_arc_set_range(arcRing, 0, 100);
  lv_obj_remove_style(arcRing, NULL, LV_PART_KNOB);
  lv_obj_clear_flag(arcRing, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_style_arc_width(arcRing, 12, LV_PART_MAIN);
  lv_obj_set_style_arc_width(arcRing, 12, LV_PART_INDICATOR);
  lv_obj_set_style_arc_color(arcRing, lv_color_make(30, 34, 56),
                             LV_PART_MAIN);
  lv_obj_set_style_arc_color(arcRing, lv_color_white(), LV_PART_INDICATOR);

  spinner = lv_spinner_create(scr, 1200, 70);
  lv_obj_set_size(spinner, 420, 420);
  lv_obj_align(spinner, LV_ALIGN_CENTER, 0, FACE_CY_PX - 240);
  lv_obj_remove_style(spinner, NULL, LV_PART_KNOB);
  lv_obj_set_style_arc_width(spinner, 12, LV_PART_MAIN);
  lv_obj_set_style_arc_width(spinner, 12, LV_PART_INDICATOR);
  lv_obj_set_style_arc_color(spinner, BG_COLOR, LV_PART_MAIN);
  lv_obj_set_style_arc_color(spinner, TITLE_COLOR, LV_PART_INDICATOR);

  for (int i = 0; i < 6; i++) {
    lv_obj_t *w = lv_arc_create(scr);
    int ring = i % 3;
    int size = 440 + ring * 30;
    lv_obj_set_size(w, size, size);
    lv_obj_align(w, LV_ALIGN_CENTER, 0, FACE_CY_PX - 240);
    bool left = i >= 3;
    int span = 40 - ring * 6;
    // Left waves straddle 180 degrees; right waves straddle 0/360.
    if (left) lv_arc_set_bg_angles(w, 180 - span / 2, 180 + span / 2);
    else lv_arc_set_bg_angles(w, 360 - span / 2, span / 2);
    lv_arc_set_value(w, 0);
    lv_obj_remove_style(w, NULL, LV_PART_KNOB);
    lv_obj_remove_style(w, NULL, LV_PART_INDICATOR);
    lv_obj_clear_flag(w, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_arc_width(w, 7, LV_PART_MAIN);
    lv_obj_set_style_arc_color(w, WAVE_COLOR, LV_PART_MAIN);
    lv_obj_set_style_arc_rounded(w, true, LV_PART_MAIN);
    waves[i] = w;
  }

  imgDisc = lv_img_create(scr);
  lv_img_set_src(imgDisc, &img_face_disc);
  lv_obj_set_pos(imgDisc, FACE_DISC_X, FACE_DISC_Y);
  imgEyes = lv_img_create(scr);
  lv_img_set_src(imgEyes, &img_eyes_open);
  lv_obj_set_pos(imgEyes, EYES_X, EYES_Y);
  imgMouth = lv_img_create(scr);
  lv_img_set_src(imgMouth, &img_mouth_smile);
  lv_obj_set_pos(imgMouth, MOUTH_X, MOUTH_Y);
  imgPupilL = lv_img_create(scr);
  lv_img_set_src(imgPupilL, &img_pupil);
  imgPupilR = lv_img_create(scr);
  lv_img_set_src(imgPupilR, &img_pupil);
  setPupils(true, 0);

  lblTitle = makeLabel(&lv_font_montserrat_32, TITLE_COLOR,
                       LV_ALIGN_TOP_MID, 12);
  lblBig = makeLabel(&lv_font_montserrat_36, lv_color_white(),
                     LV_ALIGN_BOTTOM_MID, -46);
  lblHint = makeLabel(&lv_font_montserrat_24, DIM_COLOR,
                      LV_ALIGN_BOTTOM_MID, -12);

  lv_timer_create(blinkTimerCb, 2600, NULL);
  lv_timer_create(glanceTimerCb, 2100, NULL);
}

static void applyState() {
  bool showRing = false, showSpinner = false, showWavesNow = false;
  switch (state) {
    case AppState::WIFI_CONNECTING:
      setFace(&img_eyes_closed, &img_mouth_smile, false);
      lv_label_set_text(lblTitle, "DrawBox");
      lv_label_set_text(lblBig, "waking up...");
      lv_label_set_text(lblHint, WIFI_SSID);
      break;
    case AppState::IDLE:
      setFace(&img_eyes_open, &img_mouth_smile, true);
      lv_label_set_text(lblTitle, "DrawBox");
      lv_label_set_text(lblBig, "Press me!");
      lv_label_set_text(lblHint, "then tell me what to draw");
      break;
    case AppState::LISTENING:
      setFace(&img_eyes_wide, &img_mouth_o1, true);
      lv_label_set_text(lblTitle, "I'm listening!");
      lv_label_set_text(lblBig, "");
      lv_label_set_text(lblHint, "speak now");
      lv_obj_set_style_arc_color(arcRing, lv_color_white(),
                                 LV_PART_INDICATOR);
      lv_arc_set_value(arcRing, 0);
      showRing = true;
      showWavesNow = true;
      break;
    case AppState::THINKING:
      setFace(&img_eyes_closed, &img_mouth_smile, false);
      lv_label_set_text(lblTitle, "");
      lv_label_set_text(lblBig, "Drawing...");
      lv_label_set_text(lblHint, "this takes a minute");
      showSpinner = true;
      break;
    case AppState::RESULT:
      // No full-circle ring here: LVGL v8 arcs show seams at the
      // quadrant diagonals when closed; the face plus a colored message
      // reads better anyway.
      if (resultOk) {
        setFace(&img_eyes_joy, &img_mouth_joy, false);
      } else {
        setFace(&img_eyes_open, &img_mouth_flat, true);
      }
      lv_label_set_text(lblTitle, "");
      lv_label_set_text(lblBig, resultTitle.c_str());
      lv_label_set_text(lblHint, resultDetail.length()
                                     ? ("\"" + resultDetail + "\"").c_str()
                                     : "");
      break;
  }
  lv_obj_set_style_text_color(
      lblBig, state == AppState::RESULT
                  ? (resultOk ? OK_COLOR : ERR_COLOR)
                  : lv_color_white(), 0);
  if (showRing) lv_obj_clear_flag(arcRing, LV_OBJ_FLAG_HIDDEN);
  else lv_obj_add_flag(arcRing, LV_OBJ_FLAG_HIDDEN);
  if (showSpinner) lv_obj_clear_flag(spinner, LV_OBJ_FLAG_HIDDEN);
  else lv_obj_add_flag(spinner, LV_OBJ_FLAG_HIDDEN);
  showWaves(showWavesNow);
  lv_refr_now(NULL);
}

static void setState(AppState next) {
  Serial.printf("[state] %s -> %s @%lums\n",
                stateName(state), stateName(next), (unsigned long)millis());
  state = next;
  stateSince = millis();
  applyState();
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
  // Full duplex: RX from the ES7210 mics, TX to the ES8311 speaker DAC.
  i2s_config.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_TX);
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
  pins.data_out_num = PIN_ES8311_DOUT;

  if (i2s_driver_install(I2S_CH, &i2s_config, 0, NULL) != ESP_OK ||
      i2s_set_pin(I2S_CH, &pins) != ESP_OK) {
    Serial.println("[mic] i2s driver install failed");
    return false;
  }
  i2s_zero_dma_buffer(I2S_CH);
  return true;
}

// ── SPEAKER ──────────────────────────────────────
// The ES8311 DAC shares the mic's I2S clocks (full duplex, one bus).

#define SPEAKER_VOLUME 85

static bool speakerInit() {
  pinMode(PIN_PA, OUTPUT);
  digitalWrite(PIN_PA, HIGH);
  es8311_handle_t es = es8311_create(0, ES8311_ADDRRES_0);
  if (!es) {
    Serial.println("[spk] es8311 create failed");
    return false;
  }
  const es8311_clock_config_t clk = {
      .mclk_inverted = false,
      .sclk_inverted = false,
      .mclk_from_mclk_pin = true,
      .mclk_frequency = SAMPLE_RATE_HZ * 256,
      .sample_frequency = SAMPLE_RATE_HZ,
  };
  // microphone_config is misnamed upstream: it also powers up the DAC
  // (SYSTEM_REG12) and bypasses its EQ. Without it the speaker is mute.
  if (es8311_init(es, &clk, ES8311_RESOLUTION_16, ES8311_RESOLUTION_16) !=
          ESP_OK ||
      es8311_sample_frequency_config(es, clk.mclk_frequency,
                                     clk.sample_frequency) != ESP_OK ||
      es8311_microphone_config(es, false) != ESP_OK ||
      es8311_voice_volume_set(es, SPEAKER_VOLUME, NULL) != ESP_OK) {
    Serial.println("[spk] es8311 init failed");
    return false;
  }
  return true;
}

// Renders and plays a note sequence: sine plus a soft second harmonic,
// short attack, exponential decay. Blocks until the sound has actually
// left the speaker: the TX DMA holds ~380 ms, so i2s_write alone returns
// while the chirp is still playing — straight into the next recording,
// which defeated the quiet gate (the mic heard the chirp at peak 32227).
static void playChirp(const Note *notes, int count) {
  static int16_t frame[512 * 2];
  uint32_t t0 = millis();
  uint32_t totalMs = 0;
  for (int n = 0; n < count; n++) totalMs += notes[n].ms;
  for (int n = 0; n < count; n++) {
    uint32_t total = (uint32_t)notes[n].ms * SAMPLE_RATE_HZ / 1000;
    float phase = 0.0f;
    float step = 2.0f * PI * notes[n].freq / SAMPLE_RATE_HZ;
    uint32_t done = 0;
    while (done < total) {
      uint32_t chunk = min((uint32_t)512, total - done);
      for (uint32_t i = 0; i < chunk; i++) {
        float t = (float)(done + i) / total;
        float env = min(1.0f, (done + i) / (0.008f * SAMPLE_RATE_HZ)) *
                    expf(-3.2f * t);
        float s = notes[n].freq
                      ? (sinf(phase) + 0.25f * sinf(2 * phase)) * env
                      : 0.0f;
        int16_t v = (int16_t)(s * 22000.0f);
        frame[2 * i] = v;
        frame[2 * i + 1] = v;
        phase += step;
      }
      size_t written = 0;
      i2s_write(I2S_CH, frame, chunk * 2 * sizeof(int16_t), &written,
                pdMS_TO_TICKS(200));
      done += chunk;
    }
  }
  int32_t left = (int32_t)totalMs + 80 - (int32_t)(millis() - t0);
  if (left > 0) delay(left);
}

static void chirpListen() {
  static const Note seq[] = {{587, 110}, {880, 200}, {0, 90}};
  playChirp(seq, 3);
}

static void chirpDone() {
  static const Note seq[] = {{523, 110}, {659, 110}, {784, 240}};
  playChirp(seq, 3);
}

static void chirpError() {
  static const Note seq[] = {{330, 160}, {262, 280}};
  playChirp(seq, 2);
}

static void chirpHello() {
  static const Note seq[] = {{392, 90}, {523, 160}};
  playChirp(seq, 2);
}

static void playPcm(const int16_t *pcm, uint32_t samples) {
  if (!pcm || !samples) return;
  static int16_t frame[512 * 2];
  uint32_t t0 = millis();
  uint32_t done = 0;
  int chunks = 0;
  while (done < samples) {
    uint32_t chunk = min((uint32_t)512, samples - done);
    for (uint32_t i = 0; i < chunk; i++) {
      int16_t v = pcm[done + i];
      frame[2 * i] = v;
      frame[2 * i + 1] = v;
    }
    size_t written = 0;
    i2s_write(I2S_CH, frame, chunk * 2 * sizeof(int16_t), &written,
              pdMS_TO_TICKS(200));
    done += chunk;
    chunks++;
    if ((chunks & 3) == 0) lv_timer_handler();
  }
  uint32_t totalMs = samples * 1000UL / SAMPLE_RATE_HZ;
  int32_t left = (int32_t)totalMs + 80 - (int32_t)(millis() - t0);
  if (left > 0) delay(left);
}

static void playLine(const char *key) {
  int matches[VOICE_CACHE_CAP];
  int n = 0;
  for (int i = 0; i < voiceCacheCount; i++) {
    if (voiceCache[i].pcm && strcmp(voiceCache[i].key, key) == 0)
      matches[n++] = i;
  }
  if (n == 0) {
    if (strcmp(key, "listening") == 0) chirpListen();
    else if (strcmp(key, "printing") == 0) chirpDone();
    else if (strcmp(key, "ready") == 0) chirpHello();
    else chirpError();
    return;
  }
  int pick = matches[esp_random() % (uint32_t)n];
  playPcm(voiceCache[pick].pcm, voiceCache[pick].samples);
}

// Serial 'b': speaker loopback self-test. Plays a tone while reading the
// mics on the same duplex bus in the same loop, so the RX window covers
// the tone; a healthy speaker shows up as a big peak.
static void beepSelfTest() {
  static int16_t tx[512 * 2];
  int16_t rx[512];
  int16_t peak = 0;
  float phase = 0.0f;
  const float step = 2.0f * PI * 880.0f / SAMPLE_RATE_HZ;
  const uint32_t total = SAMPLE_RATE_HZ * 7 / 10;
  uint32_t done = 0;
  while (done < total) {
    uint32_t chunk = min((uint32_t)512, total - done);
    for (uint32_t i = 0; i < chunk; i++) {
      int16_t v = (int16_t)(sinf(phase) * 26000.0f);
      tx[2 * i] = v;
      tx[2 * i + 1] = v;
      phase += step;
    }
    size_t written = 0, got = 0;
    i2s_write(I2S_CH, tx, chunk * 2 * sizeof(int16_t), &written,
              pdMS_TO_TICKS(200));
    i2s_read(I2S_CH, rx, sizeof(rx), &got, pdMS_TO_TICKS(50));
    for (size_t i = 0; i < got / 2; i++) {
      if (rx[i] > peak) peak = rx[i];
      if (-rx[i] > peak) peak = (int16_t)-rx[i];
    }
    done += chunk;
  }
  Serial.printf("[beep] played 880Hz, mic loopback peak=%d %s\n", peak,
                peak > QUIET_PEAK ? "(speaker heard)" : "(NOT heard)");
}

// Records both mic slots interleaved, keeps the louder one as mono PCM
// in wavBuf, and returns the PCM byte count. Animates the mouth and the
// progress ring from the live level while recording.
static size_t recordAudio(int seconds) {
  const size_t monoSamples = (size_t)seconds * SAMPLE_RATE_HZ;
  const size_t stereoSamples = monoSamples * 2;
  int16_t *pcm = (int16_t *)(wavBuf + 44);
  size_t got = 0;
  uint64_t energy[2] = {0, 0};
  int16_t peak[2] = {0, 0};
  int16_t framePeak = 0;
  float level = 0.0f;
  uint32_t lastFrame = 0;

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
      int idx = level < 0.18f ? 0 : level < 0.4f ? 1 : level < 0.7f ? 2 : 3;
      lv_img_set_src(imgMouth, MOUTH_LEVELS[idx]);
      lv_arc_set_value(arcRing, (int)(100.0f * got / stereoSamples));
      lv_timer_handler();
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

// One mDNS resolve per boot instead of one per fetch: lookups take up to
// 4 s each on a weak signal, and the voice prefetch alone makes 15 calls.
static IPAddress serverIp;

static void forgetServerIp() { serverIp = IPAddress(); }

static IPAddress resolveServer() {
  if (serverIp != IPAddress()) return serverIp;
  String host = DRAWBOX_HOST;
  IPAddress ip;
  if (ip.fromString(host)) {
    serverIp = ip;
    return ip;
  }
  if (host.endsWith(".local")) {
    String name = host.substring(0, host.length() - 6);
    ip = MDNS.queryHost(name.c_str(), 4000);
    if (ip == IPAddress())
      Serial.printf("[http] mDNS lookup failed for %s\n", host.c_str());
  } else if (!WiFi.hostByName(host.c_str(), ip)) {
    ip = IPAddress();
  }
  serverIp = ip;
  return ip;
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

static bool fetchVoiceWav(const char *key, int variant, int16_t **pcm,
                          uint32_t *samples);

// Reads one HTTP response (status line, headers, body) with a deadline,
// pumping LVGL so the spinner keeps moving.
static bool readHttpResponse(WiFiClient &client, int &status, String &body) {
  uint32_t deadline = millis() + HTTP_RESPONSE_TIMEOUT_MS;
  uint32_t waitStart = millis();
  bool joked = false;
  String statusLine;
  while (millis() < deadline) {
    if (client.available()) {
      statusLine = client.readStringUntil('\n');
      break;
    }
    if (!client.connected() && !client.available()) return false;
    if (!joked && jokeCount > 0 &&
        millis() - waitStart >= VOICE_JOKE_WAIT_MS) {
      joked = true;
      int idx = (int)(esp_random() % (uint32_t)jokeCount);
      int16_t *pcm = nullptr;
      uint32_t samples = 0;
      if (fetchVoiceWav("joke", idx, &pcm, &samples)) {
        if (jokeSlot.pcm) free(jokeSlot.pcm);
        jokeSlot.pcm = pcm;
        jokeSlot.samples = samples;
        Serial.printf("[voice] joke %d (%u samples)\n", idx,
                      (unsigned)samples);
        playPcm(jokeSlot.pcm, jokeSlot.samples);
      }
    }
    lv_timer_handler();
    delay(60);
  }
  if (!statusLine.startsWith("HTTP/") || statusLine.length() > 256)
    return false;
  status = statusLine.substring(9, 12).toInt();
  while (millis() < deadline) {  // headers
    String line = client.readStringUntil('\n');
    if (line.length() > 1024) return false;  // hostile header flood
    line.trim();
    if (!line.length()) break;
  }
  while ((client.connected() || client.available()) && millis() < deadline) {
    if (client.available()) {
      body += (char)client.read();
      if (body.length() > 8192) break;  // voice responses are tiny
    } else {
      lv_timer_handler();
      delay(20);
    }
  }
  return true;
}

// POST the recorded WAV; fills the result fields. Never throws/blocks
// past the HTTP deadline.
static void sendToDrawBox() {
  resultVoiceKey = "";
  IPAddress ip = resolveServer();
  if (ip == IPAddress()) {
    resultOk = false;
    resultVoiceKey = "error";
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
    forgetServerIp();
    resultOk = false;
    resultVoiceKey = "error";
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
      resultVoiceKey = "error";
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
    resultVoiceKey = "error";
    resultTitle = "No answer";
    resultDetail = "DrawBox took too long";
    return;
  }
  if (status == 401) {
    resultOk = false;
    resultVoiceKey = "error";
    resultTitle = "Not paired";
    resultDetail = "re-pair me with DrawBox";
    return;
  }

  String transcript = jsonField(body, "transcript");
  resultOk = jsonField(body, "ok") == "true";
  resultVoiceKey = jsonField(body, "voice_key");
  // A hostile/buggy server could stuff kilobytes into these; the LVGL
  // label pool is 48 KB and an allocation failure halts the box.
  if (transcript.length() > 120) transcript = transcript.substring(0, 120);
  if (resultOk) {
    resultTitle = jsonField(body, "message");
    if (!resultTitle.length()) resultTitle = "Here it comes!";
  } else {
    resultTitle = jsonField(body, "error");
    if (!resultTitle.length()) resultTitle = "Something went wrong";
  }
  resultDetail = transcript;
  if (resultTitle.length() > 96) resultTitle = resultTitle.substring(0, 96);
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

static bool readExact(WiFiClient &client, uint8_t *dst, size_t n,
                      uint32_t deadline) {
  size_t got = 0;
  while (got < n) {
    if (millis() >= deadline) return false;
    int avail = client.available();
    if (avail > 0) {
      size_t want = n - got;
      if ((size_t)avail < want) want = (size_t)avail;
      int nread = client.read(dst + got, want);
      if (nread <= 0) return false;
      got += (size_t)nread;
    } else if (!client.connected()) {
      return false;
    } else {
      lv_timer_handler();
      delay(5);
    }
  }
  return true;
}

static bool skipExact(WiFiClient &client, size_t n, uint32_t deadline) {
  uint8_t buf[64];
  while (n > 0) {
    size_t want = n < sizeof(buf) ? n : sizeof(buf);
    if (!readExact(client, buf, want, deadline)) return false;
    n -= want;
  }
  return true;
}

static bool fetchVoiceWav(const char *key, int variant, int16_t **pcm,
                          uint32_t *samples) {
  *pcm = nullptr;
  *samples = 0;
  IPAddress ip = resolveServer();
  if (ip == IPAddress()) return false;
  WiFiClient client;
  client.setTimeout(VOICE_FETCH_TIMEOUT_MS / 1000);
  if (!client.connect(ip, DRAWBOX_PORT)) {
    forgetServerIp();
    return false;
  }
  uint32_t deadline = millis() + VOICE_FETCH_TIMEOUT_MS;
  client.printf("GET /api/voice/line?key=%s&i=%d HTTP/1.1\r\n"
                "Host: %s:%d\r\n"
                "Authorization: Bearer %s\r\n"
                "Connection: close\r\n\r\n",
                key, variant, DRAWBOX_HOST, DRAWBOX_PORT, DRAWBOX_TOKEN);

  while (millis() < deadline && !client.available()) {
    if (!client.connected()) {
      client.stop();
      return false;
    }
    lv_timer_handler();
    delay(10);
  }
  String statusLine = client.readStringUntil('\n');
  if (!statusLine.startsWith("HTTP/") || statusLine.length() > 256) {
    client.stop();
    return false;
  }
  int status = statusLine.substring(9, 12).toInt();
  if (status != 200) {
    Serial.printf("[voice] HTTP %d for %s\n", status, key);
    client.stop();
    return false;
  }
  while (millis() < deadline) {
    String line = client.readStringUntil('\n');
    if (line.length() > 1024) {
      client.stop();
      return false;
    }
    line.trim();
    if (!line.length()) break;
  }

  uint8_t riff[12];
  if (!readExact(client, riff, 12, deadline) ||
      memcmp(riff, "RIFF", 4) != 0 || memcmp(riff + 8, "WAVE", 4) != 0) {
    client.stop();
    return false;
  }

  bool haveFmt = false;
  int16_t *buf = nullptr;
  uint32_t nSamples = 0;
  bool ok = false;
  while (millis() < deadline) {
    uint8_t ch[8];
    if (!readExact(client, ch, 8, deadline)) break;
    uint32_t sz = 0;
    memcpy(&sz, ch + 4, 4);
    if (memcmp(ch, "fmt ", 4) == 0) {
      if (sz < 16) break;
      uint8_t fmt[16];
      if (!readExact(client, fmt, 16, deadline)) break;
      if (sz > 16 && !skipExact(client, sz - 16, deadline)) break;
      uint16_t channels = 0, bits = 0;
      uint32_t rate = 0;
      memcpy(&channels, fmt + 2, 2);
      memcpy(&rate, fmt + 4, 4);
      memcpy(&bits, fmt + 14, 2);
      if (rate != SAMPLE_RATE_HZ || channels != 1 || bits != 16) {
        Serial.printf("[voice] bad wav %s: %u Hz %u ch %u bit\n", key,
                      (unsigned)rate, (unsigned)channels, (unsigned)bits);
        break;
      }
      haveFmt = true;
    } else if (memcmp(ch, "data", 4) == 0) {
      if (sz > VOICE_MAX_BYTES) {
        Serial.printf("[voice] clip too long for %s (%u bytes)\n", key,
                      (unsigned)sz);
        break;
      }
      buf = (int16_t *)ps_malloc(sz ? sz : 1);
      if (!buf) {
        Serial.printf("[voice] ps_malloc failed for %s\n", key);
        break;
      }
      if (sz && !readExact(client, (uint8_t *)buf, sz, deadline)) {
        free(buf);
        buf = nullptr;
        break;
      }
      nSamples = sz / 2;
      if (haveFmt) ok = true;
      break;
    } else if (!skipExact(client, sz, deadline)) {
      break;
    }
    if ((sz & 1) && !readExact(client, ch, 1, deadline)) break;
  }

  client.stop();
  if (!ok) {
    if (buf) free(buf);
    return false;
  }
  *pcm = buf;
  *samples = nSamples;
  return true;
}

// Total PSRAM budget for cached clips: the wav buffer and LVGL also live
// there, and a hostile server could otherwise fill every slot with
// max-length audio.
#define VOICE_CACHE_BUDGET_BYTES (3u * 1024u * 1024u)
static uint32_t voiceCacheBytes = 0;
static bool voiceCacheFull = false;

static bool voiceLineCached(const char *key, uint8_t variant) {
  for (int i = 0; i < voiceCacheCount; i++) {
    if (voiceCache[i].variant == variant &&
        strcmp(voiceCache[i].key, key) == 0)
      return true;
  }
  return false;
}

static void cacheVoiceLine(const char *key, uint8_t variant) {
  if (voiceCacheCount >= VOICE_CACHE_CAP) return;
  if (voiceLineCached(key, variant)) return;
  int16_t *pcm = nullptr;
  uint32_t samples = 0;
  if (!fetchVoiceWav(key, variant, &pcm, &samples)) {
    Serial.printf("[voice] fetch failed for %s\n", key);
    return;
  }
  if (voiceCacheBytes + samples * 2 > VOICE_CACHE_BUDGET_BYTES) {
    Serial.printf("[voice] cache budget reached, skipping %s\n", key);
    voiceCacheFull = true;  // retrying can't help; stop the idle refetch
    free(pcm);
    return;
  }
  voiceCacheBytes += samples * 2;
  VoiceLine &slot = voiceCache[voiceCacheCount++];
  strncpy(slot.key, key, sizeof(slot.key) - 1);
  slot.key[sizeof(slot.key) - 1] = '\0';
  slot.variant = variant;
  slot.pcm = pcm;
  slot.samples = samples;
  Serial.printf("[voice] cached %s (%u samples)\n", key, (unsigned)samples);
}

// The full line set, attempted until every prefetch key is cached; the
// idle loop retries on a flaky boot (weak WiFi made silent one-shot
// failures a chirps-forever boot).
static const char *const PREFETCH_KEYS[] = {
    "ready",       "listening", "printing",     "error",     "too_short",
    "busy",        "blocked",   "poop_blocked", "say_please"};
#define PREFETCH_KEY_COUNT (sizeof(PREFETCH_KEYS) / sizeof(PREFETCH_KEYS[0]))
static bool voiceCacheReady = false;
static uint32_t lastVoiceAttempt = 0;

static void fetchVoiceManifest() {
  lastVoiceAttempt = millis();
  IPAddress ip = resolveServer();
  if (ip == IPAddress()) {
    Serial.println("[voice] manifest skipped (no server)");
    return;
  }
  WiFiClient client;
  if (!client.connect(ip, DRAWBOX_PORT)) {
    Serial.println("[voice] manifest connect failed");
    forgetServerIp();
    return;
  }
  client.printf("GET /api/voice/lines HTTP/1.1\r\n"
                "Host: %s:%d\r\n"
                "Authorization: Bearer %s\r\n"
                "Connection: close\r\n\r\n",
                DRAWBOX_HOST, DRAWBOX_PORT, DRAWBOX_TOKEN);
  uint32_t deadline = millis() + 8000;
  String body;
  while ((client.connected() || client.available()) && millis() < deadline) {
    if (client.available()) body += (char)client.read();
    else {
      lv_timer_handler();
      delay(10);
    }
    if (body.length() > 8192) break;
  }
  client.stop();
  int tv = jsonField(body, "thinking").toInt();
  thinkingVariants = tv > 0 ? tv : 1;
  // The completeness check below must only demand what can fit next to
  // the fixed keys, or a long dashboard pick-list stalls readiness forever.
  int maxThinking = VOICE_CACHE_CAP - (int)PREFETCH_KEY_COUNT;
  if (thinkingVariants > maxThinking) thinkingVariants = maxThinking;
  int jc = jsonField(body, "jokes").toInt();
  jokeCount = jc > 0 ? jc : 0;
  Serial.printf("[voice] manifest thinking=%d jokes=%d\n", thinkingVariants,
                jokeCount);

  for (size_t i = 0; i < PREFETCH_KEY_COUNT; i++) {
    cacheVoiceLine(PREFETCH_KEYS[i], 0);
    lv_timer_handler();
  }
  for (int v = 0; v < thinkingVariants && voiceCacheCount < VOICE_CACHE_CAP;
       v++) {
    cacheVoiceLine("thinking", (uint8_t)v);
    lv_timer_handler();
  }

  bool complete = true;
  for (size_t i = 0; i < PREFETCH_KEY_COUNT; i++)
    if (!voiceLineCached(PREFETCH_KEYS[i], 0)) complete = false;
  for (int v = 0; v < thinkingVariants && v < VOICE_CACHE_CAP; v++)
    if (!voiceLineCached("thinking", (uint8_t)v)) complete = false;
  voiceCacheReady = complete || voiceCacheFull;
  Serial.printf("[voice] cache %s (%d lines)\n",
                complete ? "ready" : "incomplete, will retry",
                voiceCacheCount);
}

// ── THE ONE FLOW ─────────────────────────────────

static void handlePress() {
  setState(AppState::LISTENING);
  playLine("listening");
  size_t pcmBytes = recordAudio(recordSeconds);
  if (pcmBytes == 0 || lastPeak < QUIET_PEAK) {
    Serial.printf("[rec] too quiet (peak=%d), not uploading\n", lastPeak);
    resultOk = false;
    resultVoiceKey = "too_short";
    resultTitle = "I didn't hear anything";
    resultDetail = "come closer and try again";
    setState(AppState::RESULT);
    playLine("too_short");
    return;
  }
  setState(AppState::THINKING);
  playLine("thinking");
  sendToDrawBox();
  setState(AppState::RESULT);
  if (resultOk) playLine("printing");
  else playLine(resultVoiceKey.length() ? resultVoiceKey.c_str() : "error");
}

// ── SERIAL TEST HOOKS ────────────────────────────

static void dumpBase64(const char *tag, const uint8_t *data, size_t len) {
  static const char *tbl =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  Serial.printf("BEGIN_%s_B64 %u\n", tag, (unsigned)len);
  char line[77];
  int col = 0;
  for (size_t i = 0; i < len; i += 3) {
    uint32_t v = (uint32_t)data[i] << 16;
    if (i + 1 < len) v |= (uint32_t)data[i + 1] << 8;
    if (i + 2 < len) v |= data[i + 2];
    line[col++] = tbl[(v >> 18) & 63];
    line[col++] = tbl[(v >> 12) & 63];
    line[col++] = (i + 1 < len) ? tbl[(v >> 6) & 63] : '=';
    line[col++] = (i + 2 < len) ? tbl[v & 63] : '=';
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
  Serial.printf("END_%s_B64\n", tag);
}

static void dumpScreenshot() {
  uint32_t need = lv_snapshot_buf_size_needed(lv_scr_act(),
                                              LV_IMG_CF_TRUE_COLOR);
  uint8_t *buf = (uint8_t *)ps_malloc(need);
  if (!buf) {
    Serial.println("[shot] no memory");
    return;
  }
  lv_img_dsc_t dsc;
  if (lv_snapshot_take_to_buf(lv_scr_act(), LV_IMG_CF_TRUE_COLOR, &dsc,
                              buf, need) != LV_RES_OK) {
    Serial.println("[shot] snapshot failed");
    free(buf);
    return;
  }
  Serial.printf("[shot] %ux%u %u bytes\n", dsc.header.w, dsc.header.h,
                (unsigned)dsc.data_size);
  dumpBase64("SHOT", dsc.data, dsc.data_size);
  free(buf);
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
      pressRequested = true;
    } else if (c == 'd') {
      if (wavLen) dumpBase64("WAV", wavBuf, wavLen);
      else Serial.println("[dump] no recording yet");
    } else if (c == 'p') {
      dumpScreenshot();
    } else if (c == 'b') {
      beepSelfTest();
    } else if (c == 's') {
      printStatus();
    }
  }
}

// ── SETUP / LOOP ─────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[boot] DrawBox voice button (LVGL)");

  Wire.begin(IIC_SDA, IIC_SCL);

  if (!gfx->begin()) Serial.println("[boot] gfx->begin() failed");
  bus->writeC8D8(0x36, 0xA0);  // vendor panel orientation, matches touch map
  gfx->fillScreen(RGB565_BLACK);
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

  lv_init();
  // One modest buffer, not the demo's double: WiFi + a 256 KB upload need
  // internal heap too, and double-buffering brought free heap to 10 KB.
  uint32_t bufPx = LCD_WIDTH * LCD_HEIGHT / 8;
  lv_color_t *buf1 = (lv_color_t *)heap_caps_malloc(
      bufPx * sizeof(lv_color_t), MALLOC_CAP_DMA);
  Serial.printf("[boot] lvgl v%d.%d buf=%s heap=%u\n", lv_version_major(),
                lv_version_minor(), buf1 ? "ok" : "FAILED",
                (unsigned)ESP.getFreeHeap());
  lv_disp_draw_buf_init(&draw_buf, buf1, NULL, bufPx);

  static lv_disp_drv_t disp_drv;
  lv_disp_drv_init(&disp_drv);
  disp_drv.hor_res = LCD_WIDTH;
  disp_drv.ver_res = LCD_HEIGHT;
  disp_drv.flush_cb = flush_cb;
  disp_drv.rounder_cb = rounder_cb;
  disp_drv.draw_buf = &draw_buf;
  lv_disp_drv_register(&disp_drv);

  static lv_indev_drv_t indev_drv;
  lv_indev_drv_init(&indev_drv);
  indev_drv.type = LV_INDEV_TYPE_POINTER;
  indev_drv.read_cb = touch_read_cb;
  lv_indev_drv_register(&indev_drv);

  const esp_timer_create_args_t tick_args = {.callback = &lvgl_tick_cb,
                                             .name = "lvgl_tick"};
  esp_timer_handle_t tick_timer = NULL;
  esp_timer_create(&tick_args, &tick_timer);
  esp_timer_start_periodic(tick_timer, 2000);

  buildUi();

  // Stereo capture briefly needs 2x the mono PCM size in the same block.
  wavCapacity = 44 + (size_t)MAX_RECORD_SECONDS * SAMPLE_RATE_HZ * 2 * 2;
  wavBuf = (uint8_t *)ps_malloc(wavCapacity);
  Serial.printf("[boot] psram=%u wavbuf=%s\n", (unsigned)ESP.getPsramSize(),
                wavBuf ? "ok" : "FAILED");
  if (!wavBuf) {
    lv_label_set_text(lblBig, "PSRAM missing");
    while (true) {
      lv_timer_handler();
      delay(50);
    }
  }

  if (!micInit()) lv_label_set_text(lblBig, "Mic init failed");
  if (!speakerInit()) Serial.println("[boot] speaker unavailable");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWifiAttempt = millis();
  setState(AppState::WIFI_CONNECTING);
}

void loop() {
  handleSerial();
  lv_timer_handler();

  // Serial 't' may arrive during RESULT; a new take starts right away.
  if (pressRequested &&
      (state == AppState::IDLE || state == AppState::RESULT)) {
    pressRequested = false;
    handlePress();
    return;
  }
  pressRequested = false;

  switch (state) {
    case AppState::WIFI_CONNECTING:
      if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[wifi] up ip=%s rssi=%d\n",
                      WiFi.localIP().toString().c_str(), WiFi.RSSI());
        MDNS.begin("drawbox-button");
        fetchRecordSeconds();
        fetchVoiceManifest();
        setState(AppState::IDLE);
        playLine("ready");
      } else if (millis() - lastWifiAttempt > 20000) {
        Serial.println("[wifi] retrying");
        WiFi.disconnect();
        WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
        lastWifiAttempt = millis();
      }
      break;

    case AppState::IDLE:
      if (WiFi.status() != WL_CONNECTED) {
        forgetServerIp();
        setState(AppState::WIFI_CONNECTING);
        lastWifiAttempt = millis();
      } else if (!voiceCacheReady &&
                 millis() - lastVoiceAttempt > 30000) {
        fetchVoiceManifest();
      }
      break;

    case AppState::RESULT: {
      uint32_t showFor = resultOk ? RESULT_OK_SHOW_MS : RESULT_ERR_SHOW_MS;
      if (dismissRequested || millis() - stateSince > showFor) {
        dismissRequested = false;
        setState(AppState::IDLE);
      }
      break;
    }

    default:
      break;
  }
  delay(5);
}
