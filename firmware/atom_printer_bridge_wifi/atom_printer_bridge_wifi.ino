// WiFi <-> print head bridge for the M5Stack ATOM Printer (SKU K118).
// Joins the WLAN in wifi_credentials.h and pipes one raw TCP client (port
// 9100) to the print head, so DrawBox on the Pi can print without a cable.
// Boot and WiFi state lines go to USB only, never to the head.
//
// Without a wifi_credentials.h the sketch still compiles and runs as a
// plain USB-serial-to-head bridge: same pipe, no radio, red LED. That
// replaces the old separate atom_printer_bridge sketch.
//
// LED: red = no WiFi (or USB-only mode), green = WiFi up, blue = printing.

#if __has_include("wifi_credentials.h")
#define HAS_WIFI 1
#include <WiFi.h>
#include <ESPmDNS.h>
#include "wifi_credentials.h"
#endif

static const int HEAD_RX = 33;
static const int HEAD_TX = 23;
static const int LED_PIN = 27;

#ifdef HAS_WIFI
static const uint16_t TCP_PORT = 9100;
static const char *MDNS_NAME = "drawbox-atom";  // resolves as drawbox-atom.local
// A printing client always has bytes in flight, so a long-quiet one is
// gone. Without this cutoff, a peer that vanished without a FIN (power
// cut mid-job) would hold the single client slot until someone
// power-cycles the printer.
static const unsigned long CLIENT_IDLE_MS = 60000;

WiFiServer server(TCP_PORT);
WiFiClient client;
bool wifiWasUp = false;
bool everConnected = false;
bool scannedOnFailure = false;
bool hadClient = false;
unsigned long lastClientActivity = 0;

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

void idleLed() {
    if (wifiWasUp) {
        rgbLedWrite(LED_PIN, 0, 24, 0);
    } else {
        rgbLedWrite(LED_PIN, 24, 0, 0);
    }
}
#endif

void setup() {
    Serial.begin(9600);
    Serial2.begin(9600, SERIAL_8N1, HEAD_RX, HEAD_TX);
    rgbLedWrite(LED_PIN, 24, 0, 0);
#ifdef HAS_WIFI
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);  // mains powered; keep TCP latency predictable
    WiFi.onEvent(onWiFiEvent);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.printf("wifi: connecting to %s\r\n", WIFI_SSID);
    server.begin();
    server.setNoDelay(true);
#else
    Serial.println("no wifi_credentials.h — USB bridge mode");
#endif
}

void loop() {
#ifdef HAS_WIFI
    bool wifiUp = WiFi.status() == WL_CONNECTED;
    if (wifiUp != wifiWasUp) {
        wifiWasUp = wifiUp;
        if (wifiUp) {
            everConnected = true;
            MDNS.begin(MDNS_NAME);
            Serial.printf("wifi: up ip=%s port=%u mdns=%s.local\r\n",
                          WiFi.localIP().toString().c_str(), TCP_PORT,
                          MDNS_NAME);
        } else {
            MDNS.end();
            // The core's auto-reconnect handles the retry loop.
            Serial.println("wifi: down, reconnecting");
        }
        if (!client) {
            idleLed();
        }
    }

    // Never joined within 15 s of boot: list what the radio actually sees
    // (2.4 GHz only), once. Not on later drops — the scan would abort the
    // auto-reconnect that usually fixes those.
    if (!everConnected && !scannedOnFailure && millis() > 15000) {
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

    // One client at a time; the next connection waits in the accept
    // backlog until this one closes, which serializes jobs at the printer.
    // A half-open client (no FIN) stays connected() forever, so quiet ones
    // get dropped by the idle cutoff.
    if (client && millis() - lastClientActivity > CLIENT_IDLE_MS) {
        client.stop();
        client = WiFiClient();
        Serial.println("tcp: client idle, dropped");
    }
    // operator bool is connected(), so a normally-closed client turns
    // falsy on its own once drained; this is where we notice either kind
    // of departure.
    if (!client && hadClient) {
        hadClient = false;
        idleLed();
        Serial.println("tcp: client closed");
    }
    if (!client) {
        client = server.accept();
        if (client) {
            hadClient = true;
            lastClientActivity = millis();
            rgbLedWrite(LED_PIN, 0, 0, 24);
            Serial.printf("tcp: client %s\r\n",
                          client.remoteIP().toString().c_str());
        }
    }

    while (client && client.available() > 0) {
        Serial2.write(client.read());
        lastClientActivity = millis();
    }

    // USB -> head pauses during a TCP job so the two paths can't
    // interleave bytes mid-raster and print garbage.
    if (!client) {
        while (Serial.available() > 0) {
            Serial2.write(Serial.read());
        }
    }
    while (Serial2.available() > 0) {
        uint8_t b = Serial2.read();
        if (client && client.connected()) {
            client.write(b);
        } else {
            Serial.write(b);
        }
    }
#else
    while (Serial.available() > 0) {
        Serial2.write(Serial.read());
    }
    while (Serial2.available() > 0) {
        Serial.write(Serial2.read());
    }
#endif
}
