#pragma once
#include <Arduino.h>

typedef void (*OnRemoteStatusCallback)(const String& status, int vol);
typedef void (*OnRemoteLockCallback)(bool is_locked);
typedef void (*OnRemoteVolumeCallback)(int vol);

void net_register_callbacks(OnRemoteStatusCallback on_status,
                            OnRemoteLockCallback on_lock,
                            OnRemoteVolumeCallback on_volume);

void net_init();
void net_loop();

bool net_is_wifi_connected();
bool net_is_ws_connected();
String net_get_ip();

void net_send_speak_request();
void net_send_speak_release();
void net_send_volume_report(int vol);
void net_send_heartbeat(const String& status, int vol, bool is_locked);