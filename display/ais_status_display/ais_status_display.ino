/*
  AIS station status display for the LilyGO T-Dongle-S3.

  The Pi runs display/status_display.py, which checks the collector every few
  seconds and sends one status line over USB serial. This sketch draws that
  line and sets the LED to match. All the decisions about what to show live on
  the Pi, so changing the screen never needs a reflash.

  Protocol: one line of plain ASCII, fields separated by '|', ending in '\n':

      S|<level>|<title>|<corner>|<line 1>|<line 2>|<line 3>

    level   OK, WARN or ERR (sets the header colour and the LED)
    title   header text, e.g. "AIS OK"
    corner  small text on the right of the header, e.g. the time "14:32"
    Lines that don't start with "S|" are ignored. Missing fields are blank.

  If nothing arrives for 30 seconds the screen turns red and says so: the Pi,
  or the status script on it, has stopped.

  BOOT button: short press turns the screen off or on (the LED keeps showing
  the status). Hold it for 2 seconds to turn the picture upside down; the
  choice is remembered.

  Arduino IDE settings (Tools menu):
    Board: ESP32S3 Dev Module        USB CDC On Boot: Enabled
    USB Mode: Hardware CDC and JTAG  Flash Size: 16MB (128Mb)
    Partition Scheme: 16M Flash (3MB APP/9.9MB FATFS)   PSRAM: Disabled
  Needs LilyGO's copy of the TFT_eSPI library (from the lib folder of
  github.com/Xinyuan-LilyGO/T-Dongle-S3), which is preconfigured for this
  screen. The Library Manager version is set up for a different screen and
  would leave this one blank.
*/

#include <Arduino.h>
#include <Preferences.h>
#include <TFT_eSPI.h>

#define FIRMWARE_VERSION "1.0.0"

// T-Dongle-S3 pins, from LilyGO's examples
#define PIN_BACKLIGHT  38   // screen backlight, LOW = on
#define PIN_LED_DATA   40   // APA102 RGB LED
#define PIN_LED_CLOCK  39
#define PIN_BUTTON     0    // BOOT button, LOW = pressed

const uint32_t STALE_AFTER_MS = 30000;
const uint32_t LONG_PRESS_MS  = 2000;
const uint8_t  LED_BRIGHTNESS = 3;      // APA102 global brightness, 0-31
const size_t   MAX_LINE       = 240;
const int      FIELDS         = 7;      // S, level, title, corner, 3 lines
const int      W = 160, H = 80;         // landscape

enum Level { LEVEL_WAIT, LEVEL_OK, LEVEL_WARN, LEVEL_ERR };

struct Status {
  Level  level = LEVEL_WAIT;
  String title;
  String corner;
  String lines[3];
};

TFT_eSPI    tft;
TFT_eSprite frame(&tft);      // draw off-screen, then show in one go: no flicker
bool        spriteOk = false;
Preferences prefs;

Status   current;
char     rxBuf[MAX_LINE + 1];
size_t   rxLen = 0;
bool     rxOverflow = false;

bool     everUpdated  = false;
uint32_t lastUpdateMs = 0;
bool     beat         = false;  // flips on every update: the "alive" dot
bool     screenOn     = true;
uint8_t  rotation     = 1;      // 1 or 3 (upside down)
bool     needRedraw   = true;
uint32_t ledShown     = 0xFFFFFFFF;

// ------------------------------------------------------------------ LED

// The APA102 is driven directly (bit-banged), so no LED library is needed.
static void ledByte(uint8_t b) {
  for (int i = 7; i >= 0; i--) {
    digitalWrite(PIN_LED_DATA, (b >> i) & 1);
    digitalWrite(PIN_LED_CLOCK, HIGH);
    digitalWrite(PIN_LED_CLOCK, LOW);
  }
}

static void setLed(uint32_t rgb) {
  if (rgb == ledShown) return;
  ledShown = rgb;
  for (int i = 0; i < 4; i++) ledByte(0x00);        // start frame
  ledByte(0xE0 | (rgb ? LED_BRIGHTNESS : 0));       // brightness
  ledByte(rgb & 0xFF);                              // blue
  ledByte((rgb >> 8) & 0xFF);                       // green
  ledByte((rgb >> 16) & 0xFF);                      // red
  for (int i = 0; i < 4; i++) ledByte(0xFF);        // end frame
}

// ------------------------------------------------------------------ drawing

static String fitText(TFT_eSPI *g, String s, uint8_t font, int maxWidth) {
  while (s.length() > 0 && g->textWidth(s, font) > maxWidth) s.remove(s.length() - 1);
  return s;
}

static String ageText(uint32_t ms) {
  uint32_t s = ms / 1000;
  if (s < 120)  return String(s) + "s ago";
  if (s < 7200) return String(s / 60) + "m ago";
  return String(s / 3600) + "h ago";
}

static void render(bool stale, uint32_t now) {
  TFT_eSPI *g = spriteOk ? (TFT_eSPI *)&frame : &tft;

  Level  level = current.level;
  String title = current.title, corner = current.corner;
  String l0 = current.lines[0], l1 = current.lines[1], l2 = current.lines[2];

  if (!everUpdated) {
    title = "AIS DISPLAY";
    corner = "fw " FIRMWARE_VERSION;
    l0 = "Waiting for status";
    l1 = "from the Pi...";
    l2 = "";
  } else if (stale) {
    level = LEVEL_ERR;
    title = "NO DATA FROM PI";
    corner = "";
    l0 = "Last update " + ageText(now - lastUpdateMs);
    l1 = "Is the Pi on, and the";
    l2 = "display service running?";
  }

  uint16_t bg, fg;
  switch (level) {
    case LEVEL_OK:   bg = TFT_DARKGREEN; fg = TFT_WHITE; break;
    case LEVEL_WARN: bg = TFT_ORANGE;    fg = TFT_BLACK; break;
    case LEVEL_ERR:  bg = TFT_RED;       fg = TFT_WHITE; break;
    default:         bg = TFT_NAVY;      fg = TFT_WHITE; break;
  }
  if (stale && ((now / 500) % 2)) bg = TFT_MAROON;   // slow blink when stale

  g->fillRect(0, 0, W, H, TFT_BLACK);
  g->fillRect(0, 0, W, 18, bg);

  // alive dot at the top right: filled and hollow on alternate updates
  if (everUpdated && !stale) {
    if (beat) g->fillCircle(W - 6, 9, 2, fg);
    else      g->drawCircle(W - 6, 9, 2, fg);
  }

  int cornerRight = W - 12;
  int cornerWidth = corner.length() ? g->textWidth(corner, 1) : 0;
  g->setTextColor(fg, bg);
  g->setTextDatum(ML_DATUM);
  g->drawString(fitText(g, title, 2, cornerRight - cornerWidth - 8), 3, 9, 2);
  if (cornerWidth) {
    g->setTextDatum(MR_DATUM);
    g->drawString(corner, cornerRight, 9, 1);
  }

  g->setTextColor(TFT_WHITE, TFT_BLACK);
  g->setTextDatum(TL_DATUM);
  g->drawString(fitText(g, l0, 2, W - 6), 3, 21, 2);
  g->drawString(fitText(g, l1, 2, W - 6), 3, 41, 2);
  g->drawString(fitText(g, l2, 2, W - 6), 3, 61, 2);

  if (spriteOk) frame.pushSprite(0, 0);
}

static void updateLed(bool stale, uint32_t now) {
  uint32_t rgb;
  if (!everUpdated)                  rgb = 0x0000FF;                          // blue: waiting
  else if (stale)                    rgb = ((now / 500) % 2) ? 0xFF0000 : 0;  // blinking red
  else if (current.level == LEVEL_OK)   rgb = 0x00FF00;
  else if (current.level == LEVEL_WARN) rgb = 0xFF6000;
  else                                  rgb = 0xFF0000;
  setLed(rgb);
}

// ------------------------------------------------------------------ input

static void handleLine(char *line) {
  char *fields[FIELDS];
  int n = 0;
  fields[n++] = line;
  for (char *p = line; *p && n < FIELDS; p++) {
    if (*p == '|') { *p = '\0'; fields[n++] = p + 1; }
  }
  if (strcmp(fields[0], "S") != 0 || n < 2) return;      // not a status line

  Status s;
  if      (strcmp(fields[1], "OK") == 0)   s.level = LEVEL_OK;
  else if (strcmp(fields[1], "WARN") == 0) s.level = LEVEL_WARN;
  else                                     s.level = LEVEL_ERR;
  s.title  = n > 2 ? fields[2] : "";
  s.corner = n > 3 ? fields[3] : "";
  for (int i = 0; i < 3; i++) s.lines[i] = n > 4 + i ? fields[4 + i] : "";

  current = s;
  everUpdated = true;
  lastUpdateMs = millis();
  beat = !beat;
  needRedraw = true;
}

static void pollSerial() {
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\r') continue;
    if (c == '\n') {
      if (!rxOverflow) { rxBuf[rxLen] = '\0'; handleLine(rxBuf); }
      rxLen = 0;
      rxOverflow = false;
    } else if (rxLen < MAX_LINE) {
      rxBuf[rxLen++] = (c >= 32 && c <= 126) ? (char)c : '?';   // the fonts are ASCII only
    } else {
      rxOverflow = true;                                       // drop over-long lines
    }
  }
}

static void pollButton() {
  static bool     wasDown = false;
  static bool     longDone = false;
  static uint32_t downAt = 0;
  bool down = digitalRead(PIN_BUTTON) == LOW;
  uint32_t now = millis();

  if (down && !wasDown) { downAt = now; longDone = false; }
  if (down && !longDone && now - downAt >= LONG_PRESS_MS) {      // hold: flip the picture
    longDone = true;
    rotation = (rotation == 1) ? 3 : 1;
    prefs.putUChar("rotation", rotation);
    tft.setRotation(rotation);
    needRedraw = true;
  }
  if (!down && wasDown && !longDone && now - downAt >= 30) {     // short press: screen on/off
    screenOn = !screenOn;
    digitalWrite(PIN_BACKLIGHT, screenOn ? LOW : HIGH);
  }
  wasDown = down;
}

// ------------------------------------------------------------------ main

void setup() {
  Serial.begin(115200);

  pinMode(PIN_BACKLIGHT, OUTPUT);
  digitalWrite(PIN_BACKLIGHT, HIGH);          // dark until the first frame is ready
  pinMode(PIN_LED_DATA, OUTPUT);
  pinMode(PIN_LED_CLOCK, OUTPUT);
  digitalWrite(PIN_LED_CLOCK, LOW);
  pinMode(PIN_BUTTON, INPUT_PULLUP);

  prefs.begin("aisdisplay", false);
  rotation = prefs.getUChar("rotation", 1);
  if (rotation != 1 && rotation != 3) rotation = 1;

  tft.init();
  tft.setRotation(rotation);
  tft.fillScreen(TFT_BLACK);
  frame.setColorDepth(16);
  spriteOk = frame.createSprite(W, H) != nullptr;

  uint32_t now = millis();
  render(false, now);
  updateLed(false, now);
  digitalWrite(PIN_BACKLIGHT, LOW);

  Serial.println("AIS status display " FIRMWARE_VERSION " ready");
}

void loop() {
  pollSerial();
  pollButton();

  uint32_t now = millis();
  bool stale = everUpdated && (now - lastUpdateMs > STALE_AFTER_MS);

  static uint32_t lastTick = 0;
  if (stale && now - lastTick >= 500) {      // keep the age counting and the blink going
    lastTick = now;
    needRedraw = true;
  }
  static bool wasStale = false;
  if (stale != wasStale) { wasStale = stale; needRedraw = true; }

  if (needRedraw) {
    render(stale, now);
    needRedraw = false;
  }
  updateLed(stale, now);
  delay(10);
}
