#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "config.h"

// 内部网络对象与状态
static WebSocketsClient webSocket;
static bool s_is_ws_connected = false;

// 回调函数指针，由 main.cpp 注册进来
static void (*s_on_status_cb)(const String& status, int remaining_sec) = nullptr;
static void (*s_on_lock_cb)(bool is_locked) = nullptr;
static void (*s_on_volume_cb)(int volume) = nullptr;

// ==================== 供 main.cpp 调用的接口实现 ====================

bool net_is_wifi_connected() {
    return WiFi.status() == WL_CONNECTED;
}

bool net_is_ws_connected() {
    return s_is_ws_connected;
}

String net_get_ip() {
    if (WiFi.status() == WL_CONNECTED) {
        return WiFi.localIP().toString();
    }
    return "0.0.0.0";
}

void net_register_callbacks(void (*on_status)(const String&, int),
                            void (*on_lock)(bool),
                            void (*on_volume)(int)) {
    s_on_status_cb = on_status;
    s_on_lock_cb = on_lock;
    s_on_volume_cb = on_volume;
}

// 终端申请开麦
void net_send_speak_request() {
    if (!s_is_ws_connected) return;
    StaticJsonDocument<128> doc;
    doc["type"] = "request_speak";
    doc["seat_id"] = SEAT_ID;
    String out;
    serializeJson(doc, out);
    webSocket.sendTXT(out);
}

// 终端释放开麦
void net_send_speak_release() {
    if (!s_is_ws_connected) return;
    StaticJsonDocument<128> doc;
    doc["type"] = "release_speak";
    doc["seat_id"] = SEAT_ID;
    String out;
    serializeJson(doc, out);
    webSocket.sendTXT(out);
}

// 上报音量变更
void net_send_volume_report(int volume) {
    if (!s_is_ws_connected) return;
    StaticJsonDocument<128> doc;
    doc["type"] = "set_volume";
    doc["seat_id"] = SEAT_ID;
    doc["volume"] = volume;
    String out;
    serializeJson(doc, out);
    webSocket.sendTXT(out);
}

// 上报心跳状态
void net_send_heartbeat(const String& status, int volume, bool is_locked) {
    if (!s_is_ws_connected) return;
    StaticJsonDocument<256> doc;
    doc["type"] = "heartbeat";
    doc["seat_id"] = SEAT_ID;
    doc["status"] = status;
    doc["volume"] = volume;
    doc["is_locked"] = is_locked;
    String out;
    serializeJson(doc, out);
    webSocket.sendTXT(out);
}

// ==================== WebSocket 事件处理 ====================

static void webSocketEvent(WStype_t type, uint8_t * payload, size_t length) {
    switch (type) {
        case WStype_DISCONNECTED:
            s_is_ws_connected = false;
            Serial.println("[WS] 断开连接");
            break;

        case WStype_CONNECTED:
            s_is_ws_connected = true;
            Serial.printf("[WS] 已连接到中控服务器: %s\n", payload);
            break;

        case WStype_TEXT: {
            StaticJsonDocument<512> doc;
            DeserializationError err = deserializeJson(doc, payload, length);
            if (err) {
                Serial.printf("[WS] JSON 解析失败: %s\n", err.c_str());
                return;
            }

            const char* msg_type = doc["type"];
            if (!msg_type) return;

            // 过滤席位：若是席位定向指令，只处理目标为本席位的包
            if (doc.containsKey("seat_id")) {
                int target_seat = doc["seat_id"];
                if (target_seat != SEAT_ID) {
                    return;
                }
            }

            // 1. 处理设置状态指令 / seat_update 广播
            if (strcmp(msg_type, "set_status") == 0 || strcmp(msg_type, "seat_update") == 0) {
                if (doc.containsKey("status") && s_on_status_cb) {
                    const char* st = doc["status"];
                    int remain = doc.containsKey("remaining_sec") ? doc["remaining_sec"].as<int>() : 0;
                    s_on_status_cb(String(st), remain);
                }
            }

            // 2. 处理下发音量调节指令
            if (strcmp(msg_type, "set_volume") == 0 || doc.containsKey("volume")) {
                if (doc.containsKey("volume") && s_on_volume_cb) {
                    int vol = doc["volume"];
                    s_on_volume_cb(vol);
                }
            }

            // 3. 处理锁定/禁言指令
            if (strcmp(msg_type, "set_lock") == 0 || doc.containsKey("is_locked")) {
                if (doc.containsKey("is_locked") && s_on_lock_cb) {
                    bool lock_state = doc["is_locked"];
                    s_on_lock_cb(lock_state);
                }
            }
            break;
        }

        default:
            break;
    }
}

// 初始化网络
void net_init() {
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.printf("[WiFi] 正在连接: %s ...\n", WIFI_SSID);

    unsigned long start_attempt = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start_attempt < 10000) {
        delay(300);
        Serial.print(".");
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\n[WiFi] 连接成功! IP: %s\n", WiFi.localIP().toString().c_str());
    } else {
        Serial.println("\n[WiFi] 连接超时，继续尝试连接...");
    }

    // 启动 WebSocket 客户端
    webSocket.begin(SERVER_IP, SERVER_PORT, "/");
    webSocket.onEvent(webSocketEvent);
    webSocket.setReconnectInterval(2000);
}

// 主循环网络刷新
void net_loop() {
    webSocket.loop();
}