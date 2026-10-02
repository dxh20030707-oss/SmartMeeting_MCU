import os
import sys
import time
import math
import wave
import threading
import logging
from datetime import datetime
import numpy as np
import cv2
import sounddevice as sd
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s.%(msecs)03d] %(message)s",
    datefmt="%H:%M:%S"
)

# 录音存储目录
RECORD_DIR = os.path.join(os.path.dirname(__file__), "recordings")
os.makedirs(RECORD_DIR, exist_ok=True)

# 全局视频帧缓存与锁
latest_video_frame = None
frame_lock = threading.Lock()

# ==================== 1. 主持人音频录制类 ====================
class HostAudioRecorder:
    def __init__(self, sample_rate=16000, channels=1):
        self.sample_rate = sample_rate
        self.channels = channels
        self.is_recording = False
        self.stream = None
        self.wav_file = None
        self.current_filename = ""
        self.start_time = 0
        self.current_rms = 0.0
        self.current_db = -96.0

    def _audio_callback(self, indata, frames, time_info, status):
        """sounddevice 实时音频流回调"""
        if not self.is_recording:
            return

        # 1. 写入 PCM 数据到 WAV 文件 (转为 16-bit 整数)
        pcm_int16 = (indata * 32767).astype(np.int16)
        if self.wav_file:
            self.wav_file.writeframes(pcm_int16.tobytes())

        # 2. 计算当前块的 RMS 均方根电平与 dBFS
        rms = np.sqrt(np.mean(indata**2))
        self.current_rms = rms
        if rms > 1e-5:
            # 以满量程 1.0 为基准计算分贝
            db = 20 * math.log10(rms)
            self.current_db = max(-96.0, db)
        else:
            self.current_db = -96.0

    def start(self):
        if self.is_recording:
            return
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_filename = os.path.join(RECORD_DIR, f"host_00_{timestamp}.wav")
        
        # 初始化标准 WAV 文件 (16kHz, 16bit, 单声道)
        self.wav_file = wave.open(self.current_filename, 'wb')
        self.wav_file.setnchannels(self.channels)
        self.wav_file.setsampwidth(2) # 16-bit = 2 bytes
        self.wav_file.setframerate(self.sample_rate)

        self.is_recording = True
        self.start_time = time.time()

        # 启动 sounddevice 输入流
        self.stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype='float32',
            blocksize=1600, # 100ms 一块
            callback=self._audio_callback
        )
        self.stream.start()
        logging.info(f"🎙️ [主持人席位] 启动录音 -> {os.path.basename(self.current_filename)}")

    def stop(self):
        if not self.is_recording:
            return
        
        self.is_recording = False
        duration = time.time() - self.start_time

        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        if self.wav_file:
            self.wav_file.close()
            self.wav_file = None

        file_size_kb = os.path.getsize(self.current_filename) / 1024.0
        logging.info("💾 [主持人席位] 录音已保存并封包!")
        print(f"   ├─ 绝对路径: {self.current_filename}")
        print(f"   ├─ 音频规格: {self.sample_rate}Hz | 16-bit | 单声道 WAV")
        print(f"   └─ 文件大小: {file_size_kb:.1f} KB | 录音时长: {duration:.2f} 秒\n")

    def get_meter_bar(self):
        """生成 ASCII 音量跳动条"""
        # 将 -60dB ~ 0dB 映射到 0~10 个格
        clamped_db = max(-60.0, min(0.0, self.current_db))
        num_blocks = int((clamped_db + 60.0) / 6.0)
        bar = "█" * num_blocks + "░" * (10 - num_blocks)
        return f"[{bar}] {self.current_db:6.1f} dB"

# ==================== 2. 摄像头采集与 HTTP MJPEG 推流 ====================
def camera_thread_func():
    global latest_video_frame
    cap = cv2.VideoCapture(0)

    use_synthetic = False
    if not cap.isOpened():
        logging.warning("⚠️ 未检测到物理摄像头，已自动切换为【模拟测试视频源】")
        use_synthetic = True
    else:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)
        logging.info("📷 物理摄像头已激活 (640x480 @ 30FPS)")

    while True:
        if not use_synthetic:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.03)
                continue
        else:
            # 自动生成测试卡画面
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            frame[:] = (40, 40, 40)
            cv2.putText(frame, "Smart Meeting Camera Test", (80, 200),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 200, 255), 2)

        # 叠加绿色时间戳水印
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cv2.rectangle(frame, (10, 10), (430, 45), (0, 0, 0), -1)
        cv2.putText(frame, f"REC: {time_str}", (15, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)

        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        with frame_lock:
            latest_video_frame = jpeg.tobytes()

        time.sleep(0.03)

class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/video_feed':
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            while True:
                with frame_lock:
                    frame = latest_video_frame
                if frame:
                    self.wfile.write(b'--frame\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b'\r\n')
                time.sleep(0.04)
        else:
            self.send_response(404)
            self.end_headers()

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def start_http_server():
    server = ThreadedHTTPServer(('0.0.0.0', 8080), StreamHandler)
    logging.info("🌐 视频推流 HTTP 接口: http://127.0.0.1:8080/video_feed")
    server.serve_forever()

# ==================== 3. 主程序与终端交互 ====================
def main():
    print("\n" + "=" * 60)
    print("      PC 多媒体与主持人录音引擎 (阶段一独立验证)")
    print("=" * 60)

    # 1. 打印默认音频输入设备
    try:
        default_dev = sd.query_devices(kind='input')
        print(f"🎙️ 默认音频输入设备: [{default_dev['name']}] (采样率: {int(default_dev['default_samplerate'])}Hz)")
    except Exception as e:
        print(f"⚠️ 音频输入设备检测警告: {e}")

    # 2. 启动摄像头与视频推流
    t_cam = threading.Thread(target=camera_thread_func, daemon=True)
    t_cam.start()

    t_http = threading.Thread(target=start_http_server, daemon=True)
    t_http.start()

    recorder = HostAudioRecorder(sample_rate=16000, channels=1)

    # 3. 后台电平动态打印线程
    def meter_display_thread():
        while True:
            if recorder.is_recording:
                elapsed = int(time.time() - recorder.start_time)
                mins = elapsed // 60
                secs = elapsed % 60
                bar = recorder.get_meter_bar()
                sys.stdout.write(f"\r📊 [主持人录音中 {mins:02d}:{secs:02d}] 实时电平: {bar}   ")
                sys.stdout.flush()
            time.sleep(0.1)

    t_meter = threading.Thread(target=meter_display_thread, daemon=True)
    t_meter.start()

    time.sleep(0.5)
    print("\n" + "-" * 60)
    print("💡 操作指南:")
    print("   1. 打开浏览器访问: http://127.0.0.1:8080/video_feed 检查画面")
    print("   2. 直接按 [Enter 回车键] -> 开始/停止主持人录音")
    print("   3. 输入 [q + 回车] -> 退出测试")
    print("-" * 60 + "\n")

    try:
        while True:
            cmd = input()
            if cmd.strip().lower() == 'q':
                if recorder.is_recording:
                    recorder.stop()
                break
            
            # 按回车切换录音状态
            if not recorder.is_recording:
                recorder.start()
            else:
                sys.stdout.write("\r" + " " * 60 + "\r") # 清空电平行
                recorder.stop()
    except KeyboardInterrupt:
        pass
    finally:
        if recorder.is_recording:
            recorder.stop()
        print("测试服务已安全退出。")

if __name__ == "__main__":
    main()