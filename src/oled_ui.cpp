#include "oled_ui.h"  // OLED 显示界面管理
#include "config.h"  // 配置文件
#include <U8g2lib.h>  // U8g2 OLED 显示库

// 恢复为确保 100% 能够点亮屏幕的 U8g2 构造方式
static U8G2_SSD1306_128X64_NONAME_F_SW_I2C u8g2(U8G2_R0, PIN_OLED_SCL, PIN_OLED_SDA, U8X8_PIN_NONE); // 使用软件 I2C，避免与 I2S 冲突

static UiState g_state = UI_STATE_IDLE; // 当前显示状态
static int g_volume = 80;  // 当前音量百分比 (0-100)
static bool g_locked = false;  // 当前是否被管理员锁定
static bool g_wifi_ok = false;  // 当前 Wi-Fi 连接状态
static bool g_ws_ok = false;  // 当前 WebSocket 连接状态
static String g_ip = "Connecting...";  // 当前 Wi-Fi IP 地址
static unsigned long g_warn_start_time = 0;
static unsigned long g_last_render_tick = 0;

void ui_init() {
    u8g2.begin();
}

void ui_set_state(UiState state) {
    g_state = state;
}

void ui_set_params(int volume, bool is_locked, bool wifi_connected, bool ws_connected, const String& ip_str) {
    g_volume = volume;
    g_locked = is_locked;
    g_wifi_ok = wifi_connected;
    g_ws_ok = ws_connected;
    g_ip = ip_str;
}

void ui_trigger_lock_warning() {
    g_warn_start_time = millis();
}

void ui_render() {
    // 关键节流：限制为 15 FPS (约 66ms 刷新一帧)
    // 之前 OLED 导致 Wi-Fi 连不上的原因就是没有节流，死循环刷屏吃满了 CPU
    if (millis() - g_last_render_tick < 66) {
        return;
    }
    g_last_render_tick = millis();

    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_6x12_tr);

    // 1. 顶部状态栏：席位号 + Wi-Fi 状态 + WebSocket 状态
    char top_buf[32];
    snprintf(top_buf, sizeof(top_buf), "SEAT %02d  %s %s", 
             SEAT_ID, 
             g_wifi_ok ? "WF:OK" : "WF:--", 
             g_ws_ok ? "WS:OK" : "WS:--");
    u8g2.drawStr(0, 11, top_buf);
    u8g2.drawHLine(0, 14, 128);

    // 2. 中间核心业务区
    if (millis() - g_warn_start_time < 1200) {
        // 锁定拦截警示
        u8g2.drawStr(12, 34, "! LOCKED BY ADMIN !");
        u8g2.drawStr(16, 46, "Keys Prohibited");
    } else {
        // 正常状态显示
        if (g_state == UI_STATE_SPEAKING) {
            u8g2.drawBox(0, 18, 128, 16);
            u8g2.setDrawColor(0);
            u8g2.drawStr(22, 30, ">>> SPEAKING <<<");
            u8g2.setDrawColor(1);
        } else {
            u8g2.drawStr(26, 30, "[ IDLE / MUTE ]");
        }

        // 音量推子与锁定标记
        char vol_buf[32];
        snprintf(vol_buf, sizeof(vol_buf), "VOL:%3d%%  %s", g_volume, g_locked ? "[LOCKED]" : "[OPEN]");
        u8g2.drawStr(0, 46, vol_buf);

        // 音量进度条
        u8g2.drawFrame(0, 50, 128, 4);
        int bar_w = (g_volume * 124) / 100;
        if (bar_w > 0) u8g2.drawBox(2, 52, bar_w, 2);
    }

    // 3. 底部状态栏：网络连接信息
    u8g2.drawHLine(0, 55, 128);
    u8g2.drawStr(0, 64, g_ip.c_str());

    u8g2.sendBuffer();
}