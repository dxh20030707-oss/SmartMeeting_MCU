#include "key_handler.h"
#include "config.h"

struct KeyEntity {
    uint8_t pin;
    int last_reading;
    int stable_state;
    unsigned long last_debounce_time;
    unsigned long press_start_time;
    unsigned long last_repeat_time;
    bool is_pressed;
    BtnActionCallback on_click;
    BtnActionCallback on_repeat;
};

static KeyEntity keys[3] = {
    {PIN_KEY_TALK,     HIGH, HIGH, 0, 0, 0, false, nullptr, nullptr},
    {PIN_KEY_VOL_UP,   HIGH, HIGH, 0, 0, 0, false, nullptr, nullptr},
    {PIN_KEY_VOL_DOWN, HIGH, HIGH, 0, 0, 0, false, nullptr, nullptr}
};

void key_init() {
    for (int i = 0; i < 3; i++) {
        pinMode(keys[i].pin, INPUT_PULLUP);
    }
}

void key_register_talk(BtnActionCallback on_click) {
    keys[0].on_click = on_click;
}

void key_register_vol_up(BtnActionCallback on_click, BtnActionCallback on_repeat) {
    keys[1].on_click = on_click;
    keys[1].on_repeat = on_repeat;
}

void key_register_vol_down(BtnActionCallback on_click, BtnActionCallback on_repeat) {
    keys[2].on_click = on_click;
    keys[2].on_repeat = on_repeat;
}

static void poll_single_key(KeyEntity &k) {
    int reading = digitalRead(k.pin);

    if (reading != k.last_reading) {
        k.last_debounce_time = millis();
    }

    if ((millis() - k.last_debounce_time) > DEBOUNCE_DELAY_MS) {
        if (reading != k.stable_state) {
            k.stable_state = reading;
            if (k.stable_state == LOW) {
                k.is_pressed = true;
                k.press_start_time = millis();
                k.last_repeat_time = millis();
                if (k.on_click) k.on_click();
            } else {
                k.is_pressed = false;
            }
        }
    }
    k.last_reading = reading;

    if (k.is_pressed && k.on_repeat) {
        if ((millis() - k.press_start_time) >= LONG_PRESS_HOLD_MS) {
            if ((millis() - k.last_repeat_time) >= REPEAT_INTERVAL_MS) {
                k.last_repeat_time = millis();
                k.on_repeat();
            }
        }
    }
}

void key_scan() {
    for (int i = 0; i < 3; i++) {
        poll_single_key(keys[i]);
    }
}