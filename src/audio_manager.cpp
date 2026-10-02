#include "audio_manager.h"
#include "config.h"
#include <driver/i2s.h>
#include <WiFiUdp.h>
#include <math.h>

#define I2S_PORT            I2S_NUM_0
#define I2S_SAMPLE_RATE     16000
#define SAMPLES_PER_PACKET  256     // 每次处理 256 个采样点 (512 字节 16-bit PCM，约 16ms 极低延迟)
#define UDP_AUDIO_PORT      8766    // PC 服务端音频接收端口

static WiFiUDP udp_client;
static IPAddress server_ip_addr;
static volatile bool is_streaming = false;
static int current_vol_level = 0;

static TaskHandle_t audio_task_handle = NULL;

// 简易一阶 DC-Block 高通滤波器状态，彻底滤除直流漂移引起的炸音
static int32_t dc_filter_w = 0;

static inline int16_t dc_block_filter(int32_t x) {
    // y[n] = x[n] - x[n-1] + 0.95 * y[n-1]
    int32_t w = x + ((dc_filter_w * 31) >> 5);
    int32_t y = w - dc_filter_w;
    dc_filter_w = w;
    if (y > 32767) y = 32767;
    if (y < -32768) y = -32768;
    return (int16_t)y;
}

// 专职音频采集与 UDP 低延迟推流任务
static void audio_stream_task(void *pvParameters) {
    int32_t raw_samples[SAMPLES_PER_PACKET];
    
    // UDP 待发数据包: 1 字节席位 ID + 512 字节 16-bit PCM
    uint8_t packet_buffer[1 + SAMPLES_PER_PACKET * sizeof(int16_t)];
    packet_buffer[0] = (uint8_t)SEAT_ID;
    int16_t* pcm_output = (int16_t*)(packet_buffer + 1);

    size_t bytes_read = 0;
    unsigned long last_log_tick = 0;

    while (true) {
        if (is_streaming) {
            // 采用短超时读取，杜绝任务调度引起的滞后
            esp_err_t result = i2s_read(I2S_PORT, 
                                        (char*)raw_samples, 
                                        sizeof(raw_samples), 
                                        &bytes_read, 
                                        pdMS_TO_TICKS(25));

            if (result == ESP_OK && bytes_read > 0) {
                int samples_count = bytes_read / sizeof(int32_t);
                int64_t sum_squares = 0;

                for (int i = 0; i < samples_count; i++) {
                    // INMP441 原生 24 位居于 32 位高位：右移 15 位得到标准 16 位动态范围，杜绝爆音截幅
                    int32_t val = raw_samples[i] >> 15;
                    
                    // 通过高通滤波消除直流零偏
                    int16_t pcm_sample = dc_block_filter(val);
                    pcm_output[i] = pcm_sample;

                    sum_squares += (int32_t)pcm_sample * (int32_t)pcm_sample;
                }

                // 立即推流，不增加任何多余缓冲
                udp_client.beginPacket(server_ip_addr, UDP_AUDIO_PORT);
                udp_client.write(packet_buffer, 1 + samples_count * sizeof(int16_t));
                udp_client.endPacket();

                // 计算 RMS 电平
                if (samples_count > 0) {
                    uint32_t rms = (uint32_t)sqrt(sum_squares / samples_count);
                    int blocks = map(constrain((int)rms, 100, 4000), 100, 4000, 0, 10);
                    current_vol_level = blocks * 10;

                    if (millis() - last_log_tick >= 300) {
                        last_log_tick = millis();
                        String bar = "";
                        for (int b = 0; b < 10; b++) {
                            bar += (b < blocks) ? "█" : "░";
                        }
                        Serial.printf("[AUDIO] 采集中 -> 电平: [%s] RMS=%-5u\n", bar.c_str(), rms);
                    }
                }
            }
        } else {
            vTaskDelay(pdMS_TO_TICKS(30));
        }
    }
}

void audio_init() {
    server_ip_addr.fromString(SERVER_IP);

    // 缩减 DMA 缓冲区数量，将硬件级固有延迟从 128ms 压缩到 32ms
    i2s_config_t i2s_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate = I2S_SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 3,       // 从 8 降低至 3，大幅压降延迟
        .dma_buf_len = 128,       // 减小单次 DMA 长度
        .use_apll = false,
        .tx_desc_auto_clear = false,
        .fixed_mclk = 0
    };

    i2s_pin_config_t pin_config = {
        .bck_io_num = 0,   // SCK -> GPIO 0
        .ws_io_num = 1,    // WS  -> GPIO 1
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num = 2   // SD  -> GPIO 2
    };

    esp_err_t err = i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
    if (err != ESP_OK) {
        Serial.printf("[AUDIO ERR] I2S 驱动安装失败: %d\n", err);
        return;
    }

    err = i2s_set_pin(I2S_PORT, &pin_config);
    if (err != ESP_OK) {
        Serial.printf("[AUDIO ERR] I2S 引脚设置失败: %d\n", err);
        return;
    }

    i2s_zero_dma_buffer(I2S_PORT);

    // 提升音频采集任务的优先级到 4，确保实时响应
    xTaskCreate(
        audio_stream_task,
        "audio_stream_task",
        4096,
        NULL,
        4,
        &audio_task_handle
    );

    Serial.println("[AUDIO] INMP441 低延迟防爆音引擎配置完成!");
}

void audio_start_record() {
    if (!is_streaming) {
        server_ip_addr.fromString(SERVER_IP);
        dc_filter_w = 0;
        i2s_zero_dma_buffer(I2S_PORT);
        is_streaming = true;
        Serial.println("[AUDIO] >>> 开启音频推流 <<<");
    }
}

void audio_stop_record() {
    if (is_streaming) {
        is_streaming = false;
        current_vol_level = 0;
        Serial.println("[AUDIO] >>> 停止音频推流 <<<");
    }
}

bool audio_is_recording() {
    return is_streaming;
}

int audio_get_volume_level() {
    return current_vol_level;
}