// Conversation-mode go/no-go spike (plan Phase 2).
//
// The open question for running Grok Voice Agent sessions on this box is
// memory: mbedTLS wants ~45-50 KB of internal heap for a WSS connection,
// and this sketch already runs LVGL with a DMA draw buffer. This probe
// answers it on the real device: serial 'w' opens a TLS websocket to
// api.x.ai next to the live UI and prints heap/PSRAM at each step, plus
// an allocation probe approximating a session's working set (audio
// chunk staging + event buffers).
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
    // part — the server just refuses the upgrade afterwards. Either
    // way we learn whether TLS fits next to LVGL.
    client.addHeader("Authorization", "Bearer spike-probe");
    spikeReport("client built");
    uint32_t t0 = millis();
    bool connected =
        client.connect("wss://api.x.ai/v1/realtime?model=grok-voice-latest");
    spikeReport(connected ? "wss connected" : "wss refused");
    Serial.printf("[spike] connect %s in %lums\n",
                  connected ? "OK" : "rejected/failed",
                  (unsigned long)(millis() - t0));
    if (connected) {
      client.send("{\"type\":\"session.update\",\"session\":{}}");
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
