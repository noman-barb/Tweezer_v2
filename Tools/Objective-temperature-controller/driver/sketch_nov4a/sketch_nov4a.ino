// === 62.5 kHz PWM on Pin 9 (Timer1) + Pin 7 Control via Serial ===
// Works on Arduino Uno / Nano / Pro Mini (ATmega328P)

const int PWM_PIN = 9;    // OC1A
const int DIG_PIN = 7;    // Regular digital pin
const unsigned int PWM_TOP = 255;   // For 62.5 kHz at 16 MHz, no prescaler

void setup() {
  Serial.begin(9600);
  pinMode(PWM_PIN, OUTPUT);
  pinMode(DIG_PIN, OUTPUT);

  // --- Configure Timer1 for Fast PWM, TOP = ICR1 ---
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1  = 0;

  // Fast PWM mode 14: WGM13:0 = 14 (ICR1 as TOP)
  // Non-inverting output on OC1A (Pin 9)
  TCCR1A |= (1 << COM1A1) | (1 << WGM11);
  TCCR1B |= (1 << WGM13) | (1 << WGM12) | (1 << CS10); // No prescaler

  ICR1 = PWM_TOP;  // Set TOP → controls frequency
  setPWM9(0.0);    // Start with 0% duty cycle

  Serial.println("=== 62.5 kHz PWM (Pin 9, Timer1) + Pin 7 Control Ready ===");
  Serial.println("Commands:");
  Serial.println("  PWM <value>    (0.0 to 1.0)");
  Serial.println("  PIN7 HIGH");
  Serial.println("  PIN7 LOW");
  Serial.println();
}

// === Function to set PWM duty cycle (0.0 – 1.0) ===
void setPWM9(float duty) {
  if (duty < 0.0) duty = 0.0;
  if (duty > 1.0) duty = 1.0;
  OCR1A = (unsigned int)(duty * PWM_TOP);
}

// === Process serial commands ===
void loop() {
  if (Serial.available() > 0) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();  // Remove spaces and newline

    if (cmd.startsWith("PWM")) {
      cmd.remove(0, 3);
      cmd.trim();
      float duty = cmd.toFloat();
      setPWM9(duty);
      Serial.print("PWM duty set to ");
      Serial.println(duty, 3);
    }

    else if (cmd.equalsIgnoreCase("PIN7 HIGH")) {
      digitalWrite(DIG_PIN, HIGH);
      Serial.println("Pin 7 set HIGH");
    } 
    else if (cmd.equalsIgnoreCase("PIN7 LOW")) {
      digitalWrite(DIG_PIN, LOW);
      Serial.println("Pin 7 set LOW");
    }

    else {
      Serial.println("Unknown command. Try: PWM <0-1>, PIN7 HIGH, PIN7 LOW");
    }
  }
}
