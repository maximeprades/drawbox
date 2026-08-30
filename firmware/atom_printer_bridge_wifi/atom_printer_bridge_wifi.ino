// WiFi <-> print head bridge for the M5Stack ATOM Printer (SKU K118).
// Joins the WLAN in wifi_credentials.h and pipes one raw TCP client (port
// 9100) to the print head, so DrawBox on the Pi can print without a cable.
// The USB pipe from atom_printer_bridge still works as a fallback; boot and
// WiFi state lines go to USB only, never to the head.
//
// LED: red = no WiFi, green = WiFi up, blue = client connected.

#include <WiFi.h>
#include <ESPmDNS.h>

#include "wifi_credentials.h"  // copy wifi_credentials.h.example, fill in

static const int HEAD_RX = 33;
static const int HEAD_TX = 23;
static const int LED_PIN = 27;
static const uint16_t TCP_PORT = 9100;
static const char *MDNS_NAME = "drawbox-atom";  // resolves as drawbox-atom.local

WiFiServer server(TCP_PORT);
WiFiClient client;
bool wifiWasUp = false;
bool scannedOnFailure = false;

// Diagnostics go to USB only — the box is headless and "red LED" says
// nothing about wrong password vs no such network.
void onWiFiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
    if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
        // 201=no AP found, 202=auth failed, 15=handshake timeout (usually
        // a wrong password).
        Serial.printf("wifi: disconnected reason=%d\r\n",
                      info.wifi_sta_disconnected.reason);
    }
}

void setup() {
    Serial.begin(9600);
    Serial2.begin(9600, SERIAL_8N1, HEAD_RX, HEAD_TX);
    rgbLedWrite(LED_PIN, 24, 0, 0);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);  // mains powered; keep TCP latency predictable
    WiFi.onEvent(onWiFiEvent);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.printf("wifi: connecting to %s\r\n", WIFI_SSID);
    server.begin();
    server.setNoDelay(true);
}

void loop() {
    bool wifiUp = WiFi.status() == WL_CONNECTED;
    if (wifiUp != wifiWasUp) {
        wifiWasUp = wifiUp;
        if (wifiUp) {
            MDNS.begin(MDNS_NAME);
            rgbLedWrite(LED_PIN, 0, 24, 0);
            Serial.printf("wifi: up ip=%s port=%u mdns=%s.local\r\n",
                          WiFi.localIP().toString().c_str(), TCP_PORT,
                          MDNS_NAME);
        } else {
            MDNS.end();
            rgbLedWrite(LED_PIN, 24, 0, 0);
            // The core's auto-reconnect handles the retry loop.
            Serial.println("wifi: down, reconnecting");
        }
    }

    // Still not on the network after 15 s: list what the radio actually
    // sees (2.4 GHz only), once. Blocking is fine — nothing to bridge yet,
    // and the scan aborts the join attempt, so restart it after.
    if (!wifiUp && !scannedOnFailure && millis() > 15000) {
        scannedOnFailure = true;
        Serial.println("wifi: still down, scanning for networks");
        WiFi.disconnect(true);  // stop the retry loop; it blocks the scan
        delay(100);
        int n = WiFi.scanNetworks();
        for (int i = 0; i < n; i++) {
            Serial.printf("  '%s' ch=%d rssi=%d auth=%d\r\n",
                          WiFi.SSID(i).c_str(), WiFi.channel(i),
                          WiFi.RSSI(i), WiFi.encryptionType(i));
        }
        WiFi.scanDelete();
        WiFi.begin(WIFI_SSID, WIFI_PASS);
    }

    // One client at a time; the next connection waits in the accept backlog
    // until this one closes, which serializes jobs at the printer.
    // connected() stays true until the peer closed AND the buffer is drained,
    // so a finished job is never cut short.
    if (client && !client.connected()) {
        client.stop();
        rgbLedWrite(LED_PIN, 0, 24, 0);
        Serial.println("tcp: client closed");
    }
    if (!client) {
        client = server.accept();
        if (client) {
            rgbLedWrite(LED_PIN, 0, 0, 24);
            Serial.printf("tcp: client %s\r\n",
                          client.remoteIP().toString().c_str());
        }
    }

    while (client && client.available() > 0) {
        Serial2.write(client.read());
    }
    while (Serial.available() > 0) {
        Serial2.write(Serial.read());
    }
    while (Serial2.available() > 0) {
        uint8_t b = Serial2.read();
        if (client && client.connected()) {
            client.write(b);
        } else {
            Serial.write(b);
        }
    }
}
