#pragma once
#include <Arduino.h>

enum UiState {
    UI_STATE_IDLE,
    UI_STATE_SPEAKING
};

void ui_init();
void ui_set_state(UiState state);
void ui_set_params(int volume, bool is_locked, bool wifi_connected, bool ws_connected, const String& ip_str);
void ui_trigger_lock_warning();
void ui_render();