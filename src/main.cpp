#include <Arduino.h>
#include "config.h"
#include "oled_ui.h"
#include "key_handler.h"
#include "net_manager.h"
#include "audio_manager.h"

static UiState current_state = UI_STATE_IDLE;
static int volume_level = 80;
static bool is_locked = false;

static bool led_output_state = false;
static unsigned long last_led_toggle_tick = 0;
static unsigned long last_ui_tick = 0;
static unsigned long last_heartbeat_tick = 0;

void change_volume(int delta) {
    if (is_locked) {
        ui_trigger_lock_warning();
        return;
    }
    volume_level = constrain(volume_level + delta, 0, 100);
    ui_set_params(volume_level, is_locked, net_is_wifi_connected(), net_is_ws_connected(), net_get_ip());
    net_send_volume_report(volume_level);
}

void handle_talk_click() {
    if (current_state == UI_STATE_IDLE) {
        current_state = UI_STATE_SPEAKING;
        audio_start_record();
        net_send_speak_request();
    } else {
        current_state = UI_STATE_IDLE;
        audio_stop_record();
        net_send_speak_release();
    }
    ui_set_state(current_state);
}

void handle_vol_up()   { change_volume(5); }
void handle_vol_down() { change_volume(-5); }

void on_remote_status(const String& status, int vol) {
    if (status == "SPEAKING") {
        current_state = UI_STATE_SPEAKING;
        audio_start_record();
    } else {
        current_state = UI_STATE_IDLE;
        audio_stop_record();
    }
    if (vol >= 0) volume_level = vol;
    ui_set_state(current_state);
    ui_set_params(volume_level, is_locked, net_is_wifi_connected(), net_is_ws_connected(), net_get_ip());
}

void on_remote_lock(bool locked) {
    is_locked = locked;
    ui_set_params(volume_level, is_locked, net_is_wifi_connected(), net_is_ws_connected(), net_get_ip());
}

void on_remote_volume(int vol) {
    volume_level = vol;
    ui_set_params(volume_level, is_locked, net_is_wifi_connected(), net_is_ws_connected(), net_get_ip());
}

void update_led() {
    if (current_state == UI_STATE_SPEAKING) {
        if (millis() - last_led_toggle_tick >= LED_BLINK_HALF_PERIOD_MS) {
            last_led_toggle_tick = millis();
            led_output_state = !led_output_state;
            digitalWrite(PIN_LED_SPEAK, led_output_state ? HIGH : LOW);
        }
    } else {
        led_output_state = false;
        digitalWrite(PIN_LED_SPEAK, LOW);
    }
}

void setup() {
    Serial.begin(115200);

    pinMode(PIN_LED_SPEAK, OUTPUT);
    digitalWrite(PIN_LED_SPEAK, LOW);

    key_init();
    key_register_talk(handle_talk_click);
    key_register_vol_up(handle_vol_up, handle_vol_up);
    key_register_vol_down(handle_vol_down, handle_vol_down);

    ui_init();
    ui_set_state(current_state);
    ui_set_params(volume_level, is_locked, false, false, "Connecting...");
    ui_render();

    // 初始化硬件音频采样与推流任务
    audio_init();

    net_register_callbacks(on_remote_status, on_remote_lock, on_remote_volume);
    net_init();
}

void loop() {
    key_scan();
    net_loop();
    update_led();

    // 心跳上报 (1500ms 一次)
    if (millis() - last_heartbeat_tick >= 1500) {
        last_heartbeat_tick = millis();
        if (net_is_ws_connected()) {
            net_send_heartbeat((current_state == UI_STATE_SPEAKING) ? "SPEAKING" : "IDLE", volume_level, is_locked);
        }
    }

    // OLED 刷新 (150ms 一次，不再影响音频推流)
    if (millis() - last_ui_tick >= 150) {
        last_ui_tick = millis();
        ui_set_params(volume_level, is_locked, net_is_wifi_connected(), net_is_ws_connected(), net_get_ip());
        ui_render();
        net_loop();
    }
}