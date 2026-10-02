#pragma once
#include <Arduino.h>

// 初始化 INMP441 硬件驱动与 FreeRTOS 专用音频推流任务
void audio_init();

// 控制音频推流启停
void audio_start_record();
void audio_stop_record();

// 查询音频运行状态
bool audio_is_recording();
int audio_get_volume_level();