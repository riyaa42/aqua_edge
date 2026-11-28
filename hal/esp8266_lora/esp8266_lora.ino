// NodeMCU ESP8266 + SX1278 as the gateway computer's LoRa modem over USB serial.
// The computer's side is hal/esp8266_passthrough.py. Wiring and flashing: hal/README.md,
// "Gateway modem (ESP8266)". Needs only the ESP8266 board package (SPI.h): no other libraries.
//
// The ESP does everything that is timing-critical: radio setup, listen before talk, receiving and
// recovering a faulty radio. It only carries packet bytes: keys, signing, acknowledgements and
// retries stay on the computer. The radio behaviour mirrors hal/lora_pi.py; keep the two alike.
//
// Serial protocol, 115200 baud. A frame is COBS(body + CRC-16/CCITT-FALSE, high byte first)
// followed by 0x00, so boot noise and damaged bytes are dropped. body = type, sequence, data.
//   to the ESP:    HELLO -                 SEND cad(0/1) packet
//   from the ESP:  INFO see sendInfo()     SENT result      RX rssi(2) snr(1) packet     LOG text
// INFO and SENT repeat the request's sequence number; the INFO sent at boot has sequence 0.
#include <SPI.h>

// Wiring (NodeMCU): SCK D5, MISO D6, MOSI D7 (hardware SPI). DIO0 is not used.
const uint8_t PIN_NSS = 5;             // D1 (not D8: a pull-up there stops the board booting)
const uint8_t PIN_RESET = 4;           // D2

// Radio settings: must equal hal/lora433.py and hal/lora_pi.py. The computer checks them.
const uint32_t FREQ_HZ = 433175000UL;
const uint8_t SF = 7;
const uint8_t BW_BITS = 0x70;          // bandwidth 125 kHz
const uint8_t CR_BITS = 0x02;          // coding rate 4/5
const uint16_t PREAMBLE = 8;
const uint8_t SYNC_WORD = 0x12;
const uint8_t TX_DBM = 10;

const uint8_t MAX_PAYLOAD = 255;
const uint8_t CAD_TRIES = 5;
const long CAD_BACKOFF_MIN_MS = 100, CAD_BACKOFF_MAX_MS = 1000;
const uint32_t RX_WAIT_MS = 450;       // longest packet's time on air, plus a margin
const uint32_t TX_TIMEOUT_MS = 2000, CAD_TIMEOUT_MS = 1000;
const uint32_t HEALTH_MS = 5000, RECOVER_MS = 5000;

const uint32_t BAUD = 115200;
const uint8_t PROTOCOL = 1;
const uint8_t MSG_HELLO = 0x01, MSG_SEND = 0x02;
const uint8_t MSG_INFO = 0x81, MSG_SENT = 0x82, MSG_RX = 0x83, MSG_LOG = 0x84;
const uint8_t SENT_OK = 0, SENT_RADIO_FAULT = 1, SENT_BAD_LENGTH = 2;
const size_t MAX_BODY = 2 + 3 + MAX_PAYLOAD;   // the longest message: RX
const size_t MAX_FRAME = MAX_BODY + 2 + 3;     // CRC, and COBS overhead

// SX1278 registers and modes (same as hal/lora_pi.py)
const uint8_t REG_FIFO = 0x00, REG_OPMODE = 0x01, REG_FRF = 0x06, REG_PA = 0x09;
const uint8_t REG_FIFO_PTR = 0x0D, REG_TX_BASE = 0x0E, REG_RX_BASE = 0x0F, REG_RX_CURRENT = 0x10;
const uint8_t REG_IRQ = 0x12, REG_RX_BYTES = 0x13, REG_MODEM_STAT = 0x18, REG_PKT_SNR = 0x19;
const uint8_t REG_PKT_RSSI = 0x1A, REG_MODEM1 = 0x1D, REG_MODEM2 = 0x1E, REG_PREAMBLE = 0x20;
const uint8_t REG_PAYLOAD_LEN = 0x22, REG_MODEM3 = 0x26, REG_SYNC = 0x39, REG_DIO_MAP1 = 0x40;
const uint8_t REG_VERSION = 0x42;
const uint8_t MODE_SLEEP = 0x88, MODE_STANDBY = 0x89, MODE_TX = 0x8B, MODE_RX = 0x8D, MODE_CAD = 0x8F;
const uint8_t IRQ_RX_DONE = 0x40, IRQ_CRC_ERROR = 0x20, IRQ_TX_DONE = 0x08, IRQ_CAD_DONE = 0x04;
const uint8_t IRQ_CAD_BUSY = 0x01;
const uint8_t MODEM_RECEIVING = 0x01 | 0x02 | 0x08;
const uint8_t CHIP_VERSION = 0x12;

bool radioOk = false;
bool recoverTried = false;
uint32_t lastRecover = 0, lastHealth = 0;
uint8_t chipVersion = 0;

uint8_t inFrame[MAX_FRAME];
size_t inLength = 0;
bool inOverflow = false;

// --- serial frames ---

uint16_t crc16(const uint8_t *data, size_t n) {
  uint16_t crc = 0xFFFF;
  while (n--) {
    crc ^= (uint16_t)(*data++) << 8;
    for (uint8_t i = 0; i < 8; i++) crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
  }
  return crc;
}

void sendFrame(uint8_t type, uint8_t seq, const uint8_t *data, size_t n) {
  static uint8_t body[MAX_BODY + 2], out[MAX_FRAME];
  if (n > MAX_BODY - 2) n = MAX_BODY - 2;
  body[0] = type;
  body[1] = seq;
  memcpy(body + 2, data, n);
  uint16_t crc = crc16(body, n + 2);
  body[n + 2] = crc >> 8;
  body[n + 3] = crc & 0xFF;
  size_t length = n + 4, code = 0, w = 1;        // COBS: `code` is where the current block's size goes
  uint8_t run = 1;
  for (size_t i = 0; i < length; i++) {
    if (body[i]) {
      out[w++] = body[i];
      run++;
    }
    if (!body[i] || run == 0xFF) {
      out[code] = run;
      run = 1;
      code = w++;
    }
  }
  out[code] = run;
  out[w++] = 0x00;
  Serial.write(out, w);
}

// Decodes one COBS frame (without its 0x00) in place. Returns the length, or 0 if it is broken.
size_t cobsDecode(uint8_t *buf, size_t n) {
  size_t r = 0, w = 0;
  while (r < n) {
    uint8_t code = buf[r++];
    if (code == 0 || r + code - 1 > n) return 0;
    for (uint8_t i = 1; i < code; i++) buf[w++] = buf[r++];
    if (code != 0xFF && r < n) buf[w++] = 0;
  }
  return w;
}

void logText(const char *text) {
  sendFrame(MSG_LOG, 0, (const uint8_t *)text, strlen(text));
}

// --- SX1278 ---

uint8_t spiByte(uint8_t address, uint8_t value) {
  SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
  digitalWrite(PIN_NSS, LOW);
  SPI.transfer(address);
  uint8_t got = SPI.transfer(value);
  digitalWrite(PIN_NSS, HIGH);
  SPI.endTransaction();
  return got;
}

uint8_t readReg(uint8_t reg) { return spiByte(reg & 0x7F, 0); }
void writeReg(uint8_t reg, uint8_t value) { spiByte(reg | 0x80, value); }

void burst(uint8_t address, const uint8_t *out, uint8_t *in, size_t n) {
  SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
  digitalWrite(PIN_NSS, LOW);
  SPI.transfer(address);
  for (size_t i = 0; i < n; i++) {
    uint8_t got = SPI.transfer(out ? out[i] : 0);
    if (in) in[i] = got;
  }
  digitalWrite(PIN_NSS, HIGH);
  SPI.endTransaction();
}

// Resets the chip, applies every setting and starts receiving. False: no SX1278 answers.
bool configureRadio() {
  digitalWrite(PIN_RESET, LOW);
  delay(10);
  digitalWrite(PIN_RESET, HIGH);
  delay(10);
  chipVersion = readReg(REG_VERSION);
  if (chipVersion != CHIP_VERSION) return false;
  writeReg(REG_OPMODE, 0x00);                    // sleep in FSK mode: LoRa mode can only be set while asleep
  writeReg(REG_OPMODE, MODE_SLEEP);
  uint32_t frf = (uint32_t)(((uint64_t)FREQ_HZ << 19) / 32000000ULL);
  writeReg(REG_FRF, frf >> 16);
  writeReg(REG_FRF + 1, frf >> 8);
  writeReg(REG_FRF + 2, frf);
  writeReg(REG_TX_BASE, 0x00);
  writeReg(REG_RX_BASE, 0x00);
  writeReg(REG_PA, 0x80 | (TX_DBM - 2));         // PA_BOOST pin, the one SX1278 boards (e.g. Ra-02) use
  writeReg(REG_MODEM1, BW_BITS | CR_BITS);       // explicit header
  writeReg(REG_MODEM2, (SF << 4) | 0x04);        // CRC on
  writeReg(REG_MODEM3, 0x04 | (SF >= 11 ? 0x08 : 0x00));
  writeReg(REG_PREAMBLE, PREAMBLE >> 8);
  writeReg(REG_PREAMBLE + 1, PREAMBLE & 0xFF);
  writeReg(REG_SYNC, SYNC_WORD);
  writeReg(REG_DIO_MAP1, 0x00);
  writeReg(REG_IRQ, 0xFF);
  writeReg(REG_OPMODE, MODE_RX);
  return true;
}

bool radioHealthy() {
  return readReg(REG_VERSION) == CHIP_VERSION && readReg(REG_SYNC) == SYNC_WORD && readReg(REG_OPMODE) == MODE_RX;
}

// Sets a faulty radio up again, at most every RECOVER_MS. True when it works again.
bool recoverRadio() {
  if (radioOk) return true;
  if (recoverTried && millis() - lastRecover < RECOVER_MS) return false;
  recoverTried = true;
  lastRecover = millis();
  if (!configureRadio()) {
    logText("radio recovery failed, will try again");
    return false;
  }
  radioOk = true;
  logText("radio set up again");
  return true;
}

void radioFault(const char *what) {
  logText(what);
  radioOk = false;
  recoverRadio();
}

// Moves a received packet out of the radio's buffer and passes it to the computer.
void takeRx() {
  uint8_t flags = readReg(REG_IRQ);
  if (!(flags & IRQ_RX_DONE)) return;
  static uint8_t message[3 + MAX_PAYLOAD];
  uint8_t n = 0;
  bool good = !(flags & IRQ_CRC_ERROR);
  if (good) {
    n = readReg(REG_RX_BYTES);
    writeReg(REG_FIFO_PTR, readReg(REG_RX_CURRENT));
    burst(REG_FIFO, nullptr, message + 3, n);
    int8_t snr = (int8_t)readReg(REG_PKT_SNR);                 // in quarter dB
    int16_t rssi = -164 + readReg(REG_PKT_RSSI);               // low-frequency band formula
    if (snr < 0) rssi += snr / 4;
    message[0] = (uint16_t)rssi >> 8;
    message[1] = (uint16_t)rssi & 0xFF;
    message[2] = (uint8_t)snr;
  }
  writeReg(REG_IRQ, IRQ_RX_DONE | IRQ_CRC_ERROR);
  if (good && n) sendFrame(MSG_RX, 0, message, 3 + n);
}

// Waits for an IRQ flag. Returns all the flags, or -1 if the radio did not finish or answer.
int waitFlag(uint8_t flag, uint32_t timeoutMs) {
  uint32_t start = millis();
  uint8_t flags;
  while (!((flags = readReg(REG_IRQ)) & flag)) {
    if (millis() - start > timeoutMs) return -1;
    delay(1);                                    // also feeds the watchdog
  }
  if (readReg(REG_VERSION) != CHIP_VERSION) return -1;   // a dead SPI line reads all ones
  return flags;
}

// The radio cannot send or listen for activity without dropping a packet that is coming in
// (often an ACK or an alert). Waits for it, at most the longest packet's time. True: still coming.
bool letReceptionFinish() {
  uint32_t start = millis();
  while ((readReg(REG_MODEM_STAT) & MODEM_RECEIVING) && !(readReg(REG_IRQ) & IRQ_RX_DONE)) {
    if (millis() - start > RX_WAIT_MS) return true;
    delay(2);
  }
  return false;
}

void leaveRx() {
  takeRx();
  writeReg(REG_OPMODE, MODE_STANDBY);
  takeRx();                                      // one may have finished between the two lines above
  writeReg(REG_IRQ, 0xFF);
}

// Listen before talk: 1 busy, 0 free, -1 radio fault.
int channelBusy() {
  if (letReceptionFinish()) return 1;
  leaveRx();
  writeReg(REG_OPMODE, MODE_CAD);
  int flags = waitFlag(IRQ_CAD_DONE, CAD_TIMEOUT_MS);
  if (flags < 0) return -1;
  writeReg(REG_IRQ, 0xFF);
  writeReg(REG_OPMODE, MODE_RX);
  return (flags & IRQ_CAD_BUSY) ? 1 : 0;
}

bool sendNow(const uint8_t *data, uint8_t n) {
  letReceptionFinish();
  leaveRx();
  writeReg(REG_FIFO_PTR, 0x00);
  burst(REG_FIFO | 0x80, data, nullptr, n);
  writeReg(REG_PAYLOAD_LEN, n);
  writeReg(REG_OPMODE, MODE_TX);
  if (waitFlag(IRQ_TX_DONE, TX_TIMEOUT_MS) < 0) return false;
  writeReg(REG_IRQ, 0xFF);
  writeReg(REG_OPMODE, MODE_RX);
  return true;
}

// With cad: listen first, back off while the channel is busy, then send anyway.
uint8_t transmitPacket(const uint8_t *data, uint8_t n, bool cad) {
  if (!recoverRadio()) return SENT_RADIO_FAULT;
  if (cad) {
    for (uint8_t i = 0; i < CAD_TRIES; i++) {
      int busy = channelBusy();
      if (busy < 0) {
        radioFault("radio error: channel check did not finish");
        return SENT_RADIO_FAULT;
      }
      if (!busy) break;
      uint32_t wait = random(CAD_BACKOFF_MIN_MS, CAD_BACKOFF_MAX_MS + 1), start = millis();
      while (millis() - start < wait) {          // keep receiving while backing off
        takeRx();
        delay(1);
      }
    }
  }
  if (!sendNow(data, n)) {
    radioFault("radio error: send did not finish");
    return SENT_RADIO_FAULT;
  }
  return SENT_OK;
}

// --- messages from the computer ---

// protocol, radio ok, chip version, frequency(4), SF, bandwidth bits, coding rate bits,
// preamble(2), sync word, TX power
void sendInfo(uint8_t seq) {
  bool ok = radioOk && readReg(REG_VERSION) == CHIP_VERSION;
  const uint8_t info[] = {PROTOCOL, (uint8_t)ok, chipVersion,
                          (uint8_t)(FREQ_HZ >> 24), (uint8_t)(FREQ_HZ >> 16), (uint8_t)(FREQ_HZ >> 8),
                          (uint8_t)FREQ_HZ, SF, BW_BITS, CR_BITS,
                          (uint8_t)(PREAMBLE >> 8), (uint8_t)PREAMBLE, SYNC_WORD, TX_DBM};
  sendFrame(MSG_INFO, seq, info, sizeof info);
}

void handleFrame(uint8_t *frame, size_t n) {
  n = cobsDecode(frame, n);
  if (n < 4) return;
  uint16_t got = ((uint16_t)frame[n - 2] << 8) | frame[n - 1];
  if (crc16(frame, n - 2) != got) return;
  uint8_t type = frame[0], seq = frame[1];
  const uint8_t *data = frame + 2;
  size_t length = n - 4;
  if (type == MSG_HELLO) {
    sendInfo(seq);
  } else if (type == MSG_SEND) {
    uint8_t result = SENT_BAD_LENGTH;
    if (length >= 2 && length - 1 <= MAX_PAYLOAD) result = transmitPacket(data + 1, length - 1, data[0] != 0);
    sendFrame(MSG_SENT, seq, &result, 1);
  }
}

void readSerial() {
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c == 0) {
      if (!inOverflow && inLength) handleFrame(inFrame, inLength);
      inLength = 0;
      inOverflow = false;
      return;                                    // one message per loop, so the radio is served too
    }
    if (inLength < sizeof inFrame) inFrame[inLength++] = c;
    else inOverflow = true;
  }
}

void setup() {
  Serial.setRxBufferSize(1024);
  Serial.begin(BAUD);
  pinMode(PIN_NSS, OUTPUT);
  digitalWrite(PIN_NSS, HIGH);
  pinMode(PIN_RESET, OUTPUT);
  digitalWrite(PIN_RESET, HIGH);
  SPI.begin();
  Serial.write((uint8_t)0);                      // ends whatever noise the boot loader printed
  radioOk = configureRadio();
  if (!radioOk) {
    recoverTried = true;
    lastRecover = millis();
  }
  sendInfo(0);
}

void loop() {
  readSerial();
  if (!radioOk) {
    recoverRadio();
  } else {
    takeRx();
    if (millis() - lastHealth >= HEALTH_MS) {
      lastHealth = millis();
      if (!radioHealthy()) radioFault("radio error: radio is no longer set up and receiving");
    }
  }
  delay(1);
}
