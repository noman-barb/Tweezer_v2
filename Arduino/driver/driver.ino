/*
  Arduino Due command bridge with CRC-8 (poly 0x07)

  Fixes:
    - ADC/DAC set to 12-bit by default (analogReadResolution/analogWriteResolution)
    - Analog pin normalization: 0..11 => A0..A11 to avoid ambiguity
    - Safe batch write for pins 22..29 using g_APinDescription + OER enable
    - Clean UART_READ path (buffer then [count][bytes]; removed old quick path)
    - Increased command read timeout to 150 ms
    - Added CMD_ADC_RES to set ADC read resolution from host (optional)

  Supported commands (little-endian; CRC-8 at end of packet):
  Core:
    0x01 DIGITAL_WRITE  [CMD, pin, state, CRC]
    0x02 DIGITAL_READ   [CMD, pin, CRC]            -> [ACK][state]
    0x03 ANALOG_WRITE   [CMD, pin, valL, valH, CRC]
    0x04 ANALOG_READ    [CMD, pin, CRC]            -> [ACK][valL][valH]
    0x05 BATCH_WRITE    [CMD, mask, CRC]           // pins 22..29 via fast port
    0x06 ADC_RES        [CMD, bits, CRC]           // NEW: analogReadResolution(bits)

  I2C (bus: 0=Wire, 1=Wire1):
    0x10 I2C_BEGIN      [CMD, bus, spdL, spdH, CRC]
    0x11 I2C_WRITE      [CMD, bus, addr, n, data.., CRC] -> [ACK]
    0x12 I2C_READ       [CMD, bus, addr, n, CRC]         -> [ACK][n bytes]

  SPI:
    0x20 SPI_BEGIN      [CMD, mode, bitOrder, clkDiv, CRC]
    0x21 SPI_TRANSFER   [CMD, csPin, n, data.., CRC] -> [ACK][n bytes]

  LCD:
    0x30 LCD_INIT       [CMD, addr, cols, rows, CRC]
    0x31 LCD_PRINT      [CMD, col, row, n, bytes.., CRC]
    0x32 LCD_CLEAR      [CMD, CRC]

  Servo:
    0x40 SERVO_ATTACH   [CMD, ch, pin, CRC]
    0x41 SERVO_WRITE    [CMD, ch, angle, CRC]
    0x42 SERVO_DETACH   [CMD, ch, CRC]

  Tone (TC2 ch1, safe):
    0x50 TONE           [CMD, pin, fL, fH, dL, dH, CRC]
    0x51 NOTONE         [CMD, pin, CRC]

  PWM / DAC:
    0x60 PWM_RESOLUTION [CMD, bits, CRC]           // analogWriteResolution(bits)
    0x61 PWM_WRITE      [CMD, pin, valL, valH, CRC]

  UART (1..3):
    0x70 UART_BEGIN     [CMD, port, baud0..3, CRC]
    0x71 UART_WRITE     [CMD, port, n, bytes.., CRC] -> [ACK][written]
    0x72 UART_READ      [CMD, port, n, CRC]          -> [ACK][k][k bytes]

  External Interrupts (8 slots):
    0x80 INT_ATTACH     [CMD, slot, pin, mode, CRC]
    0x81 INT_DETACH     [CMD, slot, CRC]
    0x82 INT_QUERY      [CMD, slot, CRC]            -> [ACK][cnt0..3]

  Soft Timer (TC2 ch2):
    0x90 TIMER_START    [CMD, period_us0..3, CRC]
    0x91 TIMER_STOP     [CMD, CRC]
    0x92 TIMER_COUNT    [CMD, CRC]                  -> [ACK][cnt0..3]

  Response:
    - On valid CRC: write ACK (0xAA), then payload
    - On bad CRC: write ERR (0xEE)
*/

#include <Wire.h>
#include <SPI.h>
#include <Servo.h>
#include <LiquidCrystal_I2C.h>

#define CMD_DIGITAL_WRITE 0x01
#define CMD_DIGITAL_READ  0x02
#define CMD_ANALOG_WRITE  0x03
#define CMD_ANALOG_READ   0x04
#define CMD_BATCH_WRITE   0x05
#define CMD_ADC_RES       0x06  // NEW

#define CMD_I2C_BEGIN     0x10
#define CMD_I2C_WRITE     0x11
#define CMD_I2C_READ      0x12

#define CMD_SPI_BEGIN     0x20
#define CMD_SPI_TRANSFER  0x21

#define CMD_LCD_INIT      0x30
#define CMD_LCD_PRINT     0x31
#define CMD_LCD_CLEAR     0x32

#define CMD_SERVO_ATTACH  0x40
#define CMD_SERVO_WRITE   0x41
#define CMD_SERVO_DETACH  0x42

#define CMD_TONE          0x50
#define CMD_NOTONE        0x51

#define CMD_PWM_RES       0x60
#define CMD_PWM_WRITE     0x61

#define CMD_UART_BEGIN    0x70
#define CMD_UART_WRITE    0x71
#define CMD_UART_READ     0x72

#define CMD_INT_ATTACH    0x80
#define CMD_INT_DETACH    0x81
#define CMD_INT_QUERY     0x82

#define CMD_TIMER_START   0x90
#define CMD_TIMER_STOP    0x91
#define CMD_TIMER_COUNT   0x92

#define ACK 0xAA
#define ERR 0xEE

// ===== CRC-8 (poly=0x07) =====
uint8_t crc8(const uint8_t *data, size_t len) {
  uint8_t crc = 0;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t j = 0; j < 8; j++) {
      if (crc & 0x80) crc = (uint8_t)((crc << 1) ^ 0x07);
      else crc <<= 1;
    }
  }
  return crc;
}

// ===== Globals =====
LiquidCrystal_I2C* lcd = nullptr;
Servo servos[8];
bool  servo_used[8] = {false};

// I2C helpers
TwoWire* getWire(uint8_t bus) {
  if (bus == 0) return &Wire;
#ifdef WIRE1_INTERFACE
  if (bus == 1) return &Wire1;
#endif
  return &Wire;
}

// UARTs map: ports 1..3 -> Serial1..3
HardwareSerial* getUart(uint8_t port) {
  switch (port) {
    case 1: return &Serial1;
    case 2: return &Serial2;
    case 3: return &Serial3;
  }
  return nullptr;
}

// ===== Analog pin normalization: 0..11 => A0..A11 =====
static inline uint8_t normalizeAnalogPin(uint8_t p) {
  if (p < 12) return A0 + p;
  return p;
}

// ===== Safer/flexible fast batch write for pins 22..29 =====
static uint32_t batch_mask = 0;
static Pio*     batch_port = nullptr;
static bool     batch_init = false;

static void init_batch_mask() {
  if (batch_init) return;
  batch_init = true;
  batch_port = g_APinDescription[22].pPort;
  for (int p = 22; p <= 29; ++p) {
    if (g_APinDescription[p].pPort != batch_port) { batch_port = nullptr; break; }
    batch_mask |= g_APinDescription[p].ulPin;
    pinMode(p, OUTPUT);
  }
  if (batch_port) {
    batch_port->PIO_OER = batch_mask; // enable outputs
  }
}

static inline void fastWrite22_29(uint8_t mask8) {
  init_batch_mask();
  if (!batch_port) {
    // Fallback (portable): just digitalWrite each
    for (int i=0;i<8;i++) {
      int pin = 22 + i;
      digitalWrite(pin, (mask8 & (1<<i)) ? HIGH : LOW);
    }
    return;
  }
  uint32_t v = 0;
  for (int i=0;i<8;i++) {
    if (mask8 & (1u<<i)) v |= g_APinDescription[22+i].ulPin;
  }
  batch_port->PIO_CODR = batch_mask; // clear all 22..29
  batch_port->PIO_SODR = v;          // set selected
}

// ===== Due-safe tone() using TC2 channel 1 (TC7) =====
volatile uint32_t __tone_toggles = 0;
static uint8_t   __tone_pin = 255;
static bool      __tone_active = false;

void TC7_Handler(void) {
  TC2->TC_CHANNEL[1].TC_SR;  // clear IRQ
  if (!__tone_active || __tone_pin == 255) return;
  digitalWrite(__tone_pin, !digitalRead(__tone_pin));
  if (__tone_toggles) {
    __tone_toggles--;
    if (__tone_toggles == 0) {
      TC2->TC_CHANNEL[1].TC_CCR = TC_CCR_CLKDIS;
      NVIC_DisableIRQ(TC7_IRQn);
      __tone_active = false;
      digitalWrite(__tone_pin, LOW);
      __tone_pin = 255;
    }
  }
}
void tone(uint8_t pin, unsigned int frequency, unsigned long duration = 0) {
  if (frequency == 0) return;
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  __tone_pin = pin;
  __tone_active = true;

  uint32_t toggle_rate = (uint32_t)frequency * 2UL;
  uint32_t rc = VARIANT_MCK / 32UL / toggle_rate; // TIMER_CLOCK3 = MCK/32
  if (rc < 2) rc = 2;

  pmc_enable_periph_clk(ID_TC7);
  TcChannel *ch = &TC2->TC_CHANNEL[1];
  ch->TC_CCR = TC_CCR_CLKDIS;
  ch->TC_IDR = 0xFFFFFFFF;
  (void)ch->TC_SR;
  ch->TC_CMR = TC_CMR_TCCLKS_TIMER_CLOCK3 | TC_CMR_WAVE | TC_CMR_WAVSEL_UP_RC;
  ch->TC_RC  = rc;
  ch->TC_IER = TC_IER_CPCS;

  NVIC_EnableIRQ(TC7_IRQn);
  ch->TC_CCR = TC_CCR_CLKEN | TC_CCR_SWTRG;

  if (duration == 0) __tone_toggles = 0;
  else {
    uint64_t toggles = (uint64_t)duration * (uint64_t)toggle_rate / 1000ULL;
    __tone_toggles = (toggles == 0 ? 1 : (uint32_t)toggles);
  }
}
void noTone(uint8_t /*pin*/) {
  TC2->TC_CHANNEL[1].TC_CCR = TC_CCR_CLKDIS;
  NVIC_DisableIRQ(TC7_IRQn);
  __tone_active = false;
  if (__tone_pin != 255) digitalWrite(__tone_pin, LOW);
  __tone_pin = 255;
  __tone_toggles = 0;
}

// ===== External interrupt registry (8 slots) =====
volatile uint32_t isr_count[8] = {0};
uint8_t isr_pin[8];
bool    isr_bound[8] = {false};

void isr_generic_0(){ isr_count[0]++; }
void isr_generic_1(){ isr_count[1]++; }
void isr_generic_2(){ isr_count[2]++; }
void isr_generic_3(){ isr_count[3]++; }
void isr_generic_4(){ isr_count[4]++; }
void isr_generic_5(){ isr_count[5]++; }
void isr_generic_6(){ isr_count[6]++; }
void isr_generic_7(){ isr_count[7]++; }

void (*isr_table[8])() = {
  isr_generic_0,isr_generic_1,isr_generic_2,isr_generic_3,
  isr_generic_4,isr_generic_5,isr_generic_6,isr_generic_7
};

int mode_from_byte(uint8_t m) {
  switch (m) {
    case 0: return LOW;
    case 1: return HIGH;
    case 2: return CHANGE;
    case 3: return RISING;
    case 4: return FALLING;
  }
  return CHANGE;
}

// ===== Simple periodic timer using TC2 ch2 (TC8) =====
volatile uint32_t timer_ticks = 0;
void TC8_Handler(void) {
  TC2->TC_CHANNEL[2].TC_SR; // clear
  timer_ticks++;
}
void timer_start(uint32_t period_us) {
  pmc_enable_periph_clk(ID_TC8);
  TcChannel *ch = &TC2->TC_CHANNEL[2];
  ch->TC_CCR = TC_CCR_CLKDIS;
  ch->TC_IDR = 0xFFFFFFFF;
  (void)ch->TC_SR;

  uint32_t presc_sel = TC_CMR_TCCLKS_TIMER_CLOCK3; // MCK/32
  uint32_t presc_div = 32;
  uint32_t rc = ((VARIANT_MCK / presc_div) / (1000000UL / period_us));
  if (rc > 0xFFFF) { presc_sel = TC_CMR_TCCLKS_TIMER_CLOCK4; presc_div = 128; rc = ((VARIANT_MCK / presc_div) / (1000000UL / period_us)); }
  if (rc < 2) rc = 2;

  ch->TC_CMR = presc_sel | TC_CMR_WAVE | TC_CMR_WAVSEL_UP_RC;
  ch->TC_RC  = rc;
  ch->TC_IER = TC_IER_CPCS;
  timer_ticks = 0;

  NVIC_EnableIRQ(TC8_IRQn);
  ch->TC_CCR = TC_CCR_CLKEN | TC_CCR_SWTRG;
}
void timer_stop() {
  TC2->TC_CHANNEL[2].TC_CCR = TC_CCR_CLKDIS;
  NVIC_DisableIRQ(TC8_IRQn);
}

// ===== Helpers =====
bool read_exact(uint8_t *dst, size_t n) {
  size_t got = 0;
  unsigned long t0 = millis();
  while (got < n) {
    if (SerialUSB.available()) {
      dst[got++] = (uint8_t)SerialUSB.read();
    } else if (millis() - t0 > 150) { // increased to 150 ms
      return false;
    }
  }
  return true;
}

void send_u32(uint32_t v) {
  SerialUSB.write((uint8_t)(v & 0xFF));
  SerialUSB.write((uint8_t)((v >> 8) & 0xFF));
  SerialUSB.write((uint8_t)((v >> 16) & 0xFF));
  SerialUSB.write((uint8_t)((v >> 24) & 0xFF));
}

// ===== UART read (buffered) =====
void handleUartRead(uint8_t port, uint8_t n) {
  HardwareSerial* s = getUart(port);
  uint8_t buf[255];
  uint8_t k = 0;
  if (s) {
    while (k < n && s->available()) {
      buf[k++] = (uint8_t)s->read();
    }
  }
  SerialUSB.write(k);
  if (k) SerialUSB.write(buf, k);
}

// ===== Command handler (payload without CRC) =====
void handleCommand(uint8_t *buf, int len) {
  uint8_t cmd = buf[0];

  switch (cmd) {
    case CMD_DIGITAL_WRITE: {
      uint8_t pin = buf[1];
      uint8_t state = buf[2];
      pinMode(pin, OUTPUT);
      digitalWrite(pin, state ? HIGH : LOW);
    } break;

    case CMD_DIGITAL_READ: {
      uint8_t pin = buf[1];
      pinMode(pin, INPUT);
      uint8_t state = digitalRead(pin);
      SerialUSB.write(state ? (uint8_t)1 : (uint8_t)0);
    } break;

    case CMD_ANALOG_WRITE: {
      uint8_t pin = buf[1];
      uint16_t value = (uint16_t)buf[2] | ((uint16_t)buf[3] << 8);
      analogWrite(pin, value); // affects PWM and DAC with current write resolution
    } break;

    case CMD_ANALOG_READ: {
      uint8_t pin = normalizeAnalogPin(buf[1]);
      uint16_t v = analogRead(pin);
      SerialUSB.write((uint8_t)(v & 0xFF));
      SerialUSB.write((uint8_t)((v >> 8) & 0xFF));
    } break;

    case CMD_BATCH_WRITE: {
      uint8_t mask = buf[1];
      fastWrite22_29(mask);
    } break;

    case CMD_ADC_RES: { // NEW
      uint8_t bits = buf[1];
      analogReadResolution(bits);
    } break;

    // ===== I2C =====
    case CMD_I2C_BEGIN: {
      uint8_t bus  = buf[1];
      uint16_t khz = (uint16_t)buf[2] | ((uint16_t)buf[3] << 8);
      TwoWire* w = getWire(bus);
      w->begin();
#if ARDUINO >= 10600
      w->setClock((uint32_t)khz * 1000UL);
#endif
    } break;

    case CMD_I2C_WRITE: {
      uint8_t bus  = buf[1];
      uint8_t addr = buf[2];
      uint8_t n    = buf[3];
      TwoWire* w = getWire(bus);
      w->beginTransmission(addr);
      for (uint8_t i=0;i<n;i++) w->write(buf[4+i]);
      w->endTransmission();
    } break;

    case CMD_I2C_READ: {
      uint8_t bus  = buf[1];
      uint8_t addr = buf[2];
      uint8_t n    = buf[3];
      TwoWire* w = getWire(bus);
      w->requestFrom((int)addr, (int)n);
      for (uint8_t i=0;i<n;i++) {
        uint8_t b = w->available() ? w->read() : 0;
        SerialUSB.write(b);
      }
    } break;

    // ===== SPI =====
    case CMD_SPI_BEGIN: {
      uint8_t mode = buf[1];
      uint8_t bitOrder = buf[2];
      uint8_t clkDiv = buf[3];
      SPI.begin();
      switch (mode & 3) {
        case 0: SPI.setDataMode(SPI_MODE0); break;
        case 1: SPI.setDataMode(SPI_MODE1); break;
        case 2: SPI.setDataMode(SPI_MODE2); break;
        case 3: SPI.setDataMode(SPI_MODE3); break;
      }
      SPI.setBitOrder(bitOrder ? LSBFIRST : MSBFIRST);
      switch (clkDiv & 7) {
        case 0: SPI.setClockDivider(2); break;
        case 1: SPI.setClockDivider(4); break;
        case 2: SPI.setClockDivider(8); break;
        case 3: SPI.setClockDivider(16); break;
        case 4: SPI.setClockDivider(32); break;
        case 5: SPI.setClockDivider(64); break;
        case 6: SPI.setClockDivider(128); break;
        default: SPI.setClockDivider(255); break;
      }
    } break;

    case CMD_SPI_TRANSFER: {
      uint8_t cs = buf[1];
      uint8_t n  = buf[2];
      pinMode(cs, OUTPUT);
      digitalWrite(cs, HIGH);
      digitalWrite(cs, LOW);
      for (uint8_t i=0;i<n;i++) {
        uint8_t rx = SPI.transfer(buf[3+i]);
        SerialUSB.write(rx);
      }
      digitalWrite(cs, HIGH);
    } break;

    // ===== LCD =====
    case CMD_LCD_INIT: {
      uint8_t addr = buf[1];
      uint8_t cols = buf[2];
      uint8_t rows = buf[3];
      if (lcd) { delete lcd; lcd = nullptr; }
      lcd = new LiquidCrystal_I2C(addr, cols, rows);
      lcd->init();
      lcd->backlight();
    } break;

    case CMD_LCD_PRINT: {
      if (!lcd) break;
      uint8_t col = buf[1];
      uint8_t row = buf[2];
      uint8_t n   = buf[3];
      lcd->setCursor(col, row);
      for (uint8_t i=0;i<n;i++) lcd->print((char)buf[4+i]);
    } break;

    case CMD_LCD_CLEAR: {
      if (lcd) lcd->clear();
    } break;

    // ===== Servo =====
    case CMD_SERVO_ATTACH: {
      uint8_t ch = buf[1];
      uint8_t pin = buf[2];
      if (ch < 8) {
        servos[ch].attach(pin);
        servo_used[ch] = true;
      }
    } break;

    case CMD_SERVO_WRITE: {
      uint8_t ch = buf[1];
      uint8_t ang = buf[2];
      if (ch < 8 && servo_used[ch]) servos[ch].write(ang);
    } break;

    case CMD_SERVO_DETACH: {
      uint8_t ch = buf[1];
      if (ch < 8 && servo_used[ch]) { servos[ch].detach(); servo_used[ch]=false; }
    } break;

    // ===== Tone =====
    case CMD_TONE: {
      uint8_t pin = buf[1];
      uint16_t f = (uint16_t)buf[2] | ((uint16_t)buf[3] << 8);
      uint16_t d = (uint16_t)buf[4] | ((uint16_t)buf[5] << 8);
      tone(pin, f, d);
    } break;

    case CMD_NOTONE: {
      uint8_t pin = buf[1];
      (void)pin;
      noTone(pin);
    } break;

    // ===== PWM/DAC =====
    case CMD_PWM_RES: {
      uint8_t bits = buf[1];
      analogWriteResolution(bits); // affects PWM and DAC
    } break;

    case CMD_PWM_WRITE: {
      uint8_t pin = buf[1];
      uint16_t v = (uint16_t)buf[2] | ((uint16_t)buf[3] << 8);
      analogWrite(pin, v);
    } break;

    // ===== UARTs =====
    case CMD_UART_BEGIN: {
      uint8_t port = buf[1];
      uint32_t baud = (uint32_t)buf[2] | ((uint32_t)buf[3] << 8) | ((uint32_t)buf[4] << 16) | ((uint32_t)buf[5] << 24);
      HardwareSerial* s = getUart(port);
      if (s) s->begin(baud);
    } break;

    case CMD_UART_WRITE: {
      uint8_t port = buf[1];
      uint8_t n = buf[2];
      HardwareSerial* s = getUart(port);
      uint8_t written = 0;
      if (s) written = (uint8_t)s->write(&buf[3], n);
      SerialUSB.write(written);
    } break;

    case CMD_UART_READ: {
      // handled in loop() via handleUartRead for proper [count][bytes]
      // (no action here)
    } break;

    // ===== External Interrupts =====
    case CMD_INT_ATTACH: {
      uint8_t slot = buf[1];
      uint8_t pin  = buf[2];
      uint8_t mode = buf[3];
      if (slot < 8) {
        detachInterrupt(digitalPinToInterrupt(pin));
        pinMode(pin, INPUT);
        isr_pin[slot] = pin;
        isr_count[slot] = 0;
        isr_bound[slot] = true;
        attachInterrupt(digitalPinToInterrupt(pin), isr_table[slot], mode_from_byte(mode));
      }
    } break;

    case CMD_INT_DETACH: {
      uint8_t slot = buf[1];
      if (slot < 8 && isr_bound[slot]) {
        detachInterrupt(digitalPinToInterrupt(isr_pin[slot]));
        isr_bound[slot] = false;
      }
    } break;

    case CMD_INT_QUERY: {
      uint8_t slot = buf[1];
      uint32_t c = (slot < 8) ? isr_count[slot] : 0;
      send_u32(c);
    } break;

    // ===== Timer =====
    case CMD_TIMER_START: {
      uint32_t per = (uint32_t)buf[1] | ((uint32_t)buf[2] << 8) | ((uint32_t)buf[3] << 16) | ((uint32_t)buf[4] << 24);
      timer_start(per);
    } break;

    case CMD_TIMER_STOP: {
      timer_stop();
    } break;

    case CMD_TIMER_COUNT: {
      send_u32(timer_ticks);
    } break;
  }
}

// Determine expected packet length (without CRC) given leading bytes
int expected_len(const uint8_t *hdr, int have) {
  uint8_t cmd = hdr[0];

  switch (cmd) {
    case CMD_DIGITAL_WRITE: return 3;
    case CMD_DIGITAL_READ:  return 2;
    case CMD_ANALOG_WRITE:  return 4;
    case CMD_ANALOG_READ:   return 2;
    case CMD_BATCH_WRITE:   return 2;
    case CMD_ADC_RES:       return 2;

    case CMD_I2C_BEGIN:     return 4;
    case CMD_I2C_READ:      return 4;

    case CMD_SPI_BEGIN:     return 4;

    case CMD_LCD_INIT:      return 4;
    case CMD_LCD_CLEAR:     return 1;

    case CMD_SERVO_ATTACH:  return 3;
    case CMD_SERVO_WRITE:   return 3;
    case CMD_SERVO_DETACH:  return 2;

    case CMD_TONE:          return 6;
    case CMD_NOTONE:        return 2;

    case CMD_PWM_RES:       return 2;
    case CMD_PWM_WRITE:     return 4;

    case CMD_UART_BEGIN:    return 6;
    case CMD_UART_READ:     return 3;

    case CMD_INT_ATTACH:    return 4;
    case CMD_INT_DETACH:    return 2;
    case CMD_INT_QUERY:     return 2;

    case CMD_TIMER_START:   return 5;
    case CMD_TIMER_STOP:    return 1;
    case CMD_TIMER_COUNT:   return 1;
  }

  // Variable-length commands
  if (cmd == CMD_I2C_WRITE || cmd == CMD_SPI_TRANSFER || cmd == CMD_LCD_PRINT || cmd == CMD_UART_WRITE) {
    if (cmd == CMD_I2C_WRITE) {
      if (have >= 4) return 4 + hdr[3];
    } else if (cmd == CMD_SPI_TRANSFER) {
      if (have >= 3) return 3 + hdr[2];
    } else if (cmd == CMD_LCD_PRINT) {
      if (have >= 4) return 4 + hdr[3];
    } else if (cmd == CMD_UART_WRITE) {
      if (have >= 3) return 3 + hdr[2];
    }
  }

  return 0;
}

void setup() {
  SerialUSB.begin(2000000);
  analogReadResolution(12);     // ensure 12-bit ADC
  analogWriteResolution(12);    // ensure 12-bit DAC/PWM default
}

void loop() {
  if (!SerialUSB.available()) return;

  uint8_t hdr[260];
  if (!read_exact(hdr, 2)) return;

  int need = expected_len(hdr, 2);
  if (need == 0) {
    int have = 2;
    while (have < 4 && need == 0) {
      if (!read_exact(&hdr[have], 1)) return;
      have++;
      need = expected_len(hdr, have);
      if (need > 0) break;
    }
    if (need == 0) return;
    if (!read_exact(&hdr[have], need - have)) return;
  } else {
    if (!read_exact(&hdr[2], need - 2)) return;
  }

  uint8_t crc;
  if (!read_exact(&crc, 1)) return;

  if (crc8(hdr, need) == crc) {
    SerialUSB.write(ACK);

    if (hdr[0] == CMD_UART_READ) {
      handleUartRead(hdr[1], hdr[2]);
    } else {
      handleCommand(hdr, need);
    }

  } else {
    SerialUSB.write(ERR);
  }
}
