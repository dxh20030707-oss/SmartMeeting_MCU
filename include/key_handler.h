#pragma once
#include <Arduino.h>

typedef void (*BtnActionCallback)();

void key_init();
void key_register_talk(BtnActionCallback on_click);
void key_register_vol_up(BtnActionCallback on_click, BtnActionCallback on_repeat);
void key_register_vol_down(BtnActionCallback on_click, BtnActionCallback on_repeat);
void key_scan();