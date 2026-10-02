#pragma once
#include <Arduino.h>

// 席位身份配置 (终端 2 时修改为 2)
#define SEAT_ID             1

// Wi-Fi 接入点配置
#define WIFI_SSID           "9828"
#define WIFI_PASSWORD       "12345678"
#define WIFI_PASS           WIFI_PASSWORD // 兼容旧宏定义

// PC 服务端配置 (填入电脑实际获取到的 WLAN IPv4)
#define SERVER_IP           "192.168.157.223"
#define SERVER_PORT         8765

// 硬件引脚映射
#define PIN_OLED_SDA        8
#define PIN_OLED_SCL        9
#define PIN_LED_SPEAK       7   // 外接发言指示灯

#define PIN_KEY_TALK        3   // 发言切换按键
#define PIN_KEY_VOL_UP      4   // 音量加
#define PIN_KEY_VOL_DOWN    5   // 音量减

// 时序参数 (ms)
#define DEBOUNCE_DELAY_MS        25
#define LONG_PRESS_HOLD_MS       400
#define REPEAT_INTERVAL_MS       80
#define LED_BLINK_HALF_PERIOD_MS 250