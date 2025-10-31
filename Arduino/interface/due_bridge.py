# due_bridge.py 

import serial
import struct
import time
from typing import Optional

ACK = 0xAA
ERR = 0xEE

CMD_DIGITAL_WRITE = 0x01
CMD_DIGITAL_READ  = 0x02
CMD_ANALOG_WRITE  = 0x03
CMD_ANALOG_READ   = 0x04
CMD_BATCH_WRITE   = 0x05
CMD_ADC_RES       = 0x06 

CMD_I2C_BEGIN     = 0x10
CMD_I2C_WRITE     = 0x11
CMD_I2C_READ      = 0x12

CMD_SPI_BEGIN     = 0x20
CMD_SPI_TRANSFER  = 0x21

CMD_LCD_INIT      = 0x30
CMD_LCD_PRINT     = 0x31
CMD_LCD_CLEAR     = 0x32

CMD_SERVO_ATTACH  = 0x40
CMD_SERVO_WRITE   = 0x41
CMD_SERVO_DETACH  = 0x42

CMD_TONE          = 0x50
CMD_NOTONE        = 0x51

CMD_PWM_RES       = 0x60
CMD_PWM_WRITE     = 0x61

CMD_UART_BEGIN    = 0x70
CMD_UART_WRITE    = 0x71
CMD_UART_READ     = 0x72

CMD_INT_ATTACH    = 0x80
CMD_INT_DETACH    = 0x81
CMD_INT_QUERY     = 0x82

CMD_TIMER_START   = 0x90
CMD_TIMER_STOP    = 0x91
CMD_TIMER_COUNT   = 0x92


def crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            if c & 0x80:
                c = ((c << 1) & 0xFF) ^ 0x07
            else:
                c = (c << 1) & 0xFF
    return c


class DueError(Exception):
    pass


class Due:
    """
    Voltage-mode behavior (default):
      - analog_read(pin)  -> float volts (0..vref_adc) assuming 12-bit ADC
      - analog_write(pin, volts) -> DAC0/DAC1 only (pins 66/67)
    Raw helpers:
      - analog_read_raw(pin)  -> int
      - analog_write_raw(pin, value) -> None
    """

    def __init__(self, port: str, baud: int = 2_000_000, timeout: float = 0.35):
        self.ser = serial.Serial(
            port=port,
            baudrate=baud,
            timeout=timeout,
            write_timeout=timeout,
            inter_byte_timeout=timeout,
        )
        self.voltage_mode: bool = True
        self.vref_adc: float = 3.3
        self.vref_dac: float = 3.3
        self.adc_bits: int = 12
        self.dac_bits: int = 12
        time.sleep(0.25)
        self.flush()

        # Try to enforce 12-bit on both sides (older firmware may ignore CMD_ADC_RES)
        try:
            self.pwm_resolution(12)   # affects DAC & PWM on Due
        except Exception:
            pass
        try:
            self.adc_resolution(12)   # NEW; safe no-op if firmware older (will raise ERR -> caught below)
        except Exception:
            pass

    # Preferences
    def set_voltage_mode(self, enabled: bool):
        self.voltage_mode = bool(enabled)

    def set_vref(self, adc_vref: Optional[float] = None, dac_vref: Optional[float] = None):
        if adc_vref is not None:
            self.vref_adc = float(adc_vref)
        if dac_vref is not None:
            self.vref_dac = float(dac_vref)

    # Serial plumbing
    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def flush(self):
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def _write_packet(self, payload: bytes):
        self.ser.write(payload + bytes([crc8(payload)]))

    def _read_exact(self, n: int) -> bytes:
        out = bytearray()
        t0 = time.time()
        while len(out) < n:
            chunk = self.ser.read(n - len(out))
            if chunk:
                out.extend(chunk)
            elif time.time() - t0 > (self.ser.timeout or 0.35):
                break
        if len(out) != n:
            raise DueError(f"Timeout reading {n} bytes (got {len(out)})")
        return bytes(out)

    def _expect_ack(self):
        b = self._read_exact(1)[0]
        if b == ERR:
            raise DueError("Device replied ERR (CRC or command error)")
        if b != ACK:
            raise DueError(f"Expected ACK 0xAA, got 0x{b:02X}")

    def _tx(self, payload: bytes, rx_len: int = 0) -> bytes:
        self._write_packet(payload)
        self._expect_ack()
        return self._read_exact(rx_len) if rx_len else b""

    # Conversions
    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return hi if x > hi else lo if x < lo else x

    def _raw_to_volts(self, raw: int, bits: int, vref: float) -> float:
        full = (1 << bits) - 1
        return (float(raw) / float(full)) * float(vref)

    def _volts_to_raw(self, volts: float, bits: int, vref: float) -> int:
        volts = self._clamp(volts, 0.0, vref)
        full = (1 << bits) - 1
        return int(round((volts / float(vref)) * full))

    # Core GPIO / ADC/DAC
    def digital_write(self, pin: int, state: int):
        self._tx(bytes([CMD_DIGITAL_WRITE, pin & 0xFF, 1 if state else 0]))

    def digital_read(self, pin: int) -> int:
        r = self._tx(bytes([CMD_DIGITAL_READ, pin & 0xFF]), rx_len=1)
        return 1 if r[0] else 0

    def analog_read_raw(self, pin: int) -> int:
        r = self._tx(bytes([CMD_ANALOG_READ, pin & 0xFF]), rx_len=2)
        return r[0] | (r[1] << 8)

    def analog_write_raw(self, pin: int, value: int):
        self._tx(bytes([CMD_ANALOG_WRITE, pin & 0xFF, value & 0xFF, (value >> 8) & 0xFF]))

    def analog_read(self, pin: int):
        raw = self.analog_read_raw(pin)
        if self.voltage_mode:
            return self._raw_to_volts(raw, self.adc_bits, self.vref_adc)
        return raw

    def analog_write(self, pin: int, value):
        if self.voltage_mode:
            if pin not in (66, 67):
                raise DueError("analog_write(volts) in voltage_mode only for DAC0/DAC1 (pins 66/67). Use pwm_write() or disable voltage_mode for raw writes.")
            raw = self._volts_to_raw(float(value), self.dac_bits, self.vref_dac)
            self.analog_write_raw(pin, raw)
        else:
            self.analog_write_raw(pin, int(value))

    def batch_write_22_29(self, mask: int):
        self._tx(bytes([CMD_BATCH_WRITE, mask & 0xFF]))

    # I2C
    def i2c_begin(self, bus: int, khz: int):
        self._tx(bytes([CMD_I2C_BEGIN, bus & 0xFF, khz & 0xFF, (khz >> 8) & 0xFF]))

    def i2c_write(self, bus: int, addr: int, data: bytes):
        if len(data) > 255:
            raise ValueError("I2C write length must be <=255")
        self._tx(bytes([CMD_I2C_WRITE, bus & 0xFF, addr & 0x7F, len(data)]) + data)

    def i2c_read(self, bus: int, addr: int, n: int) -> bytes:
        if not (1 <= n <= 255):
            raise ValueError("I2C read length must be 1..255")
        return self._tx(bytes([CMD_I2C_READ, bus & 0xFF, addr & 0x7F, n]), rx_len=n)

    # SPI
    def spi_begin(self, mode: int = 0, lsb_first: bool = False, clk_div_code: int = 0):
        self._tx(bytes([CMD_SPI_BEGIN, mode & 0x03, 1 if lsb_first else 0, clk_div_code & 0x07]))

    def spi_transfer(self, cs_pin: int, data: bytes) -> bytes:
        if len(data) > 255:
            raise ValueError("SPI transfer length must be <=255")
        return self._tx(bytes([CMD_SPI_TRANSFER, cs_pin & 0xFF, len(data)]) + data, rx_len=len(data))

    # LCD
    def lcd_init(self, addr: int = 0x27, cols: int = 16, rows: int = 2):
        self._tx(bytes([CMD_LCD_INIT, addr & 0x7F, cols & 0xFF, rows & 0xFF]))

    def lcd_print(self, col: int, row: int, text: str):
        data = text.encode("utf-8")[:255]
        self._tx(bytes([CMD_LCD_PRINT, col & 0xFF, row & 0xFF, len(data)]) + data)

    def lcd_clear(self):
        self._tx(bytes([CMD_LCD_CLEAR]))

    # Servo
    def servo_attach(self, ch: int, pin: int):
        if not (0 <= ch <= 7):
            raise ValueError("Servo channel must be 0..7")
        self._tx(bytes([CMD_SERVO_ATTACH, ch & 0xFF, pin & 0xFF]))

    def servo_write(self, ch: int, angle: int):
        self._tx(bytes([CMD_SERVO_WRITE, ch & 0xFF, max(0, min(180, angle)) & 0xFF]))

    def servo_detach(self, ch: int):
        self._tx(bytes([CMD_SERVO_DETACH, ch & 0xFF]))

    # Tone
    def tone(self, pin: int, freq_hz: int, duration_ms: int = 0):
        self._tx(bytes([
            CMD_TONE, pin & 0xFF,
            freq_hz & 0xFF, (freq_hz >> 8) & 0xFF,
            duration_ms & 0xFF, (duration_ms >> 8) & 0xFF
        ]))

    def no_tone(self, pin: int = 0):
        self._tx(bytes([CMD_NOTONE, pin & 0xFF]))

    # PWM / DAC
    def pwm_resolution(self, bits: int):
        self._tx(bytes([CMD_PWM_RES, bits & 0xFF]))

    def pwm_write(self, pin: int, value: int):
        self._tx(bytes([CMD_PWM_WRITE, pin & 0xFF, value & 0xFF, (value >> 8) & 0xFF]))

    # UART
    def uart_begin(self, port: int, baud: int):
        self._tx(bytes([CMD_UART_BEGIN, port & 0xFF]) + struct.pack("<I", baud))

    def uart_write(self, port: int, data: bytes) -> int:
        if len(data) > 255:
            raise ValueError("UART write length must be <=255")
        r = self._tx(bytes([CMD_UART_WRITE, port & 0xFF, len(data)]) + data, rx_len=1)
        return r[0]

    def uart_read(self, port: int, n: int) -> bytes:
        if not (1 <= n <= 255):
            raise ValueError("UART read length must be 1..255")
        self._write_packet(bytes([CMD_UART_READ, port & 0xFF, n & 0xFF]))
        self._expect_ack()
        count = self._read_exact(1)[0]
        return self._read_exact(count) if count else b""

    # External INTs
    def int_attach(self, slot: int, pin: int, mode: str = "CHANGE"):
        m = {"LOW":0, "HIGH":1, "CHANGE":2, "RISING":3, "FALLING":4}[mode.upper()]
        self._tx(bytes([CMD_INT_ATTACH, slot & 0xFF, pin & 0xFF, m & 0xFF]))

    def int_detach(self, slot: int):
        self._tx(bytes([CMD_INT_DETACH, slot & 0xFF]))

    def int_query(self, slot: int) -> int:
        r = self._tx(bytes([CMD_INT_QUERY, slot & 0xFF]), rx_len=4)
        return struct.unpack("<I", r)[0]

    # Timer
    def timer_start(self, period_us: int):
        self._tx(bytes([CMD_TIMER_START]) + struct.pack("<I", period_us))

    def timer_stop(self):
        self._tx(bytes([CMD_TIMER_STOP]))

    def timer_count(self) -> int:
        r = self._tx(bytes([CMD_TIMER_COUNT]), rx_len=4)
        return struct.unpack("<I", r)[0]

    
    def adc_resolution(self, bits: int):
        self._tx(bytes([CMD_ADC_RES, bits & 0xFF]))

