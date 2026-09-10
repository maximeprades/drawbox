// Conversation-mode go/no-go spike (plan Phase 2).
//
// The open question for running Grok Voice Agent sessions on this box is
// memory: mbedTLS wants ~45-50 KB of internal heap for a WSS connection,
// and this sketch already runs LVGL with a DMA draw buffer. This probe
// answers it on the real device: serial 'w' opens a TLS websocket to the
// Vercel AI Gateway realtime route (the same endpoint the Pi client uses;
// see drawbox_core.GATEWAY_REALTIME_URL) next to the live UI and prints
// heap/PSRAM at each step, plus an allocation probe approximating a
// session's working set (audio chunk staging + event buffers).
//
// Gateway contract, mirrored from drawbox_core / @ai-sdk/gateway: the
// model id rides the `?ai-model-id=` query and the minted `vcst_` token
// rides Sec-WebSocket-Protocol as `ai-gateway-auth.<token>` next to the
// `ai-gateway-realtime.v1` marker. The real client gets both the URL and
// the protocol list from POST /api/realtime/token on the Pi.
//
// Guarded by __has_include so the firmware builds even when the
// ArduinoWebsockets library isn't installed (build.sh installs it).
// The spike deliberately uses setInsecure(): certificate pinning is a
// production decision; heap is what we're measuring.
#pragma once

#if __has_include(<ArduinoWebsockets.h>)
#include <ArduinoWebsockets.h>
#define HAVE_REALTIME_SPIKE 1

static void spikeReport(const char *stage) {
  Serial.printf("[spike] %-18s heap=%6u minfree=%6u psram=%u\n", stage,
                (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMinFreeHeap(),
                (unsigned)ESP.getFreePsram());
}

static void runRealtimeSpike() {
  Serial.println("[spike] realtime WSS heap probe starting");
  spikeReport("baseline");

  // Working-set probe first: one audio chunk in flight each way plus a
  // base64 staging buffer and an event buffer, roughly what the session
  // client needs beyond TLS itself.
  uint8_t *ws1 = (uint8_t *)malloc(8 * 1024);
  uint8_t *ws2 = (uint8_t *)malloc(12 * 1024);
  spikeReport("workset alloc");
  bool worksetOk = ws1 && ws2;
  free(ws1);
  free(ws2);

  {
    websockets::WebsocketsClient client;
    client.setInsecure();
    // A bogus token still pays the full TLS handshake — the expensive
    // part — the Gateway answers 401 "Invalid client secret" afterwards.
    // Either way we learn whether TLS fits next to LVGL.
    client.addHeader("Sec-WebSocket-Protocol",
                     "ai-gateway-realtime.v1, ai-gateway-auth.vcst_spike_probe");
    spikeReport("client built");
    uint32_t t0 = millis();
    bool connected = client.connect(
        "wss://ai-gateway.vercel.sh/v4/ai/realtime-model"
        "?ai-model-id=spacexai/grok-voice-think-fast-2.0");
    spikeReport(connected ? "wss connected" : "wss refused");
    Serial.printf("[spike] connect %s in %lums\n",
                  connected ? "OK" : "rejected/failed",
                  (unsigned long)(millis() - t0));
    if (connected) {
      // Normalized AI SDK event, not OpenAI's session.update.
      client.send("{\"type\":\"session-update\",\"config\":{}}");
      uint32_t until = millis() + 3000;
      while (millis() < until) {
        client.poll();
        lv_timer_handler();
        delay(10);
      }
      spikeReport("after traffic");
      client.close();
    }
  }
  delay(200);
  spikeReport("released");
  Serial.printf("[spike] verdict: workset=%s — see minfree above; below "
                "~20KB minfree during 'wss connected' means Phase 4 needs "
                "the Pi-proxy fallback\n",
                worksetOk ? "ok" : "FAILED");
}
#else
#define HAVE_REALTIME_SPIKE 0
static void runRealtimeSpike() {
  Serial.println("[spike] ArduinoWebsockets library not installed; "
                 "run build.sh to fetch it");
}
#endif
