// USB <-> print head serial bridge for the M5Stack ATOM Printer (SKU K118).
// Replaces the stock AP/MQTT firmware. Both links run at 9600 so the host
// paces the print head; the head's CTS line (G19) is not needed.

static const int HEAD_RX = 33;
static const int HEAD_TX = 23;

void setup() {
    Serial.begin(9600);
    Serial2.begin(9600, SERIAL_8N1, HEAD_RX, HEAD_TX);
}

void loop() {
    while (Serial.available() > 0) {
        Serial2.write(Serial.read());
    }
    while (Serial2.available() > 0) {
        Serial.write(Serial2.read());
    }
}
