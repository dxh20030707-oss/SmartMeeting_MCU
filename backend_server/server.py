import asyncio
import json
import logging
import math
import os
import queue
import socket
import threading
import time
import wave
from datetime import datetime
import numpy as np
import cv2
import sounddevice as sd
import websockets
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# 引入 ASR 和 LLM 模块
from xf_asr import XfAsrClient
from llm_summary import generate_meeting_summary

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s.%(msecs)03d] %(message)s",
    datefmt="%H:%M:%S"
)

RECORD_DIR = os.path.join(os.path.dirname(__file__), "recordings")
os.makedirs(RECORD_DIR, exist_ok=True)

CONNECTED_CLIENTS = set()
MEETING_TRANSCRIPTS = []

SEAT_STATES = {
    1: {"name": "设备一", "online": False, "status": "IDLE", "volume": 80, "is_locked": False, "last_seen": 0},
    2: {"name": "设备二", "online": False, "status": "IDLE", "volume": 80, "is_locked": False, "last_seen": 0}
}

HOST_STATE = {
    "name": "主持人",
    "status": "IDLE"
}

latest_video_frame = None
frame_lock = threading.Lock()

active_recorders = {}
recorder_lock = threading.Lock()
last_udp_audio_time = {}

async_loop = None

# ==================== 0. 超低延迟准实时音频播放引擎 ====================
class JitterBufferPlayer:
    def __init__(self, sample_rate=16000, block_size=256):
        self.sample_rate = sample_rate
        self.block_size = block_size
        # 极度压缩队列上限至 6 帧（~96ms），坚决不给声音堆积排队的机会
        self.audio_queue = queue.Queue(maxsize=6)
        self.stream = None
        self.init_player()

    def init_player(self):
        def callback(outdata, frames, time_info, status):
            try:
                # 若积压包过多，快速丢弃老数据，只播放最新一包（追帧防延迟）
                while self.audio_queue.qsize() > 2:
                    self.audio_queue.get_nowait()

                data = self.audio_queue.get_nowait()
                expected_bytes = frames * 2
                if len(data) == expected_bytes:
                    outdata[:] = np.frombuffer(data, dtype=np.int16).reshape(-1, 1)
                elif len(data) > 0:
                    raw_samples = np.frombuffer(data, dtype=np.int16)
                    if len(raw_samples) >= frames:
                        outdata[:] = raw_samples[:frames].reshape(-1, 1)
                    else:
                        padded = np.zeros(frames, dtype=np.int16)
                        padded[:len(raw_samples)] = raw_samples
                        outdata[:] = padded.reshape(-1, 1)
                else:
                    outdata.fill(0)
            except queue.Empty:
                outdata.fill(0)

        try:
            self.stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype='int16',
                blocksize=self.block_size,
                latency='low', # 启用声卡底层低延迟驱动模式
                callback=callback
            )
            self.stream.start()
            logging.info("🔊 极低延迟平滑扩音引擎已就绪 (16kHz 低延迟硬件链路)")
        except Exception as e:
            logging.error(f"⚠️ 扬声器启动失败: {e}")

    def push_pcm(self, pcm_bytes, volume_percent=100):
        try:
            if volume_percent != 100:
                gain = volume_percent / 100.0
                samples = np.frombuffer(pcm_bytes, dtype=np.int16)
                adjusted = np.clip(samples * gain, -32768, 32767).astype(np.int16)
                pcm_bytes = adjusted.tobytes()

            if self.audio_queue.full():
                try: self.audio_queue.get_nowait()
                except queue.Empty: pass
            self.audio_queue.put_nowait(pcm_bytes)
        except Exception:
            pass

    def clear(self):
        while not self.audio_queue.empty():
            try: self.audio_queue.get_nowait()
            except queue.Empty: break

audio_player = JitterBufferPlayer(sample_rate=16000)

# ==================== 1. 标准 WAV 录音落盘与自动 ASR 转写 ====================
class WavRecorder:
    def __init__(self, seat_id, name):
        self.seat_id = seat_id
        self.name = name
        now = datetime.now()
        timestamp_str = now.strftime("%Y%m%d_%H时%M分%S秒")
        self.time_tag = now.strftime("%H:%M:%S")
        self.filename = f"{self.name}_{self.seat_id:02d}_{timestamp_str}.wav"
        self.filepath = os.path.join(RECORD_DIR, self.filename)
        
        self.wav_file = wave.open(self.filepath, 'wb')
        self.wav_file.setnchannels(1)
        self.wav_file.setsampwidth(2)
        self.wav_file.setframerate(16000)
        self.is_open = True
        self.start_time = time.time()
        self.total_bytes = 0
        self.packet_count = 0
        logging.info(f"🎙️ [{self.time_tag}] [{self.name} | 席位{self.seat_id:02d}] 启动录音 -> {self.filename}")

    def write_pcm(self, pcm_bytes):
        if self.is_open and self.wav_file:
            self.wav_file.writeframes(pcm_bytes)
            self.total_bytes += len(pcm_bytes)
            self.packet_count += 1

    def close(self):
        if self.is_open and self.wav_file:
            self.wav_file.close()
            self.is_open = False
            duration = time.time() - self.start_time
            size_kb = self.total_bytes / 1024.0
            logging.info(f"💾 [{self.name} | 席位{self.seat_id:02d}] 录音完成: {os.path.basename(self.filepath)} (时长: {duration:.1f}s, 大小: {size_kb:.1f}KB)")
            
            if duration > 0.3:
                threading.Thread(target=self._async_asr_task, args=(self.filepath, self.name, self.seat_id, self.time_tag), daemon=True).start()

    def _async_asr_task(self, wav_path, name, seat_id, time_str):
        try:
            client = XfAsrClient(wav_path)
            text = client.transcribe()
            if text:
                logging.info(f"📝 [讯飞识别] [{name}]: {text}")
                record_item = {
                    "time": time_str,
                    "seat_id": seat_id,
                    "speaker": name,
                    "text": text
                }
                MEETING_TRANSCRIPTS.append(record_item)
                
                if async_loop and CONNECTED_CLIENTS:
                    asyncio.run_coroutine_threadsafe(
                        broadcast({
                            "type": "transcript_append",
                            "item": record_item
                        }),
                        async_loop
                    )
        except Exception as e:
            logging.error(f"ASR 转写流程异常: {e}")

def start_seat_recorder(seat_id, name):
    with recorder_lock:
        if active_recorders.get(seat_id) is None:
            audio_player.clear()
            last_udp_audio_time[seat_id] = time.time()
            active_recorders[seat_id] = WavRecorder(seat_id, name)

def stop_seat_recorder(seat_id):
    with recorder_lock:
        rec = active_recorders.get(seat_id)
        if rec:
            rec.close()
            active_recorders[seat_id] = None

# ==================== 2. 主持人 (PC 麦克风) ====================
class HostAudioService:
    def __init__(self):
        self.is_recording = False
        self.stream = None
        self.recorder = None
        self.pcm_slice_buffer = bytearray()
        try:
            dev_info = sd.query_devices(kind='input')
            self.native_rate = int(dev_info.get('default_samplerate', 44100))
        except Exception:
            self.native_rate = 44100

    def _audio_callback(self, indata, frames, time_info, status):
        if self.is_recording and self.recorder:
            mono_data = indata[:, 0]
            step = self.native_rate / 16000.0
            indices = (np.arange(0, frames, step)).astype(int)
            indices = indices[indices < frames]
            resampled = mono_data[indices]

            pcm16 = (np.clip(resampled, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            self.recorder.write_pcm(pcm16)

            # 主持人本地实时耳返推送 (若使用外放音箱产生回音，可将下方三行注释)
            self.pcm_slice_buffer.extend(pcm16)
            while len(self.pcm_slice_buffer) >= 512:
                chunk = bytes(self.pcm_slice_buffer[:512])
                del self.pcm_slice_buffer[:512]
                audio_player.push_pcm(chunk, 100)

    def start(self):
        if self.is_recording: return
        audio_player.clear()
        self.pcm_slice_buffer.clear()
        self.recorder = WavRecorder(0, HOST_STATE["name"])
        self.is_recording = True
        try:
            self.stream = sd.InputStream(
                samplerate=self.native_rate,
                channels=1,
                dtype='float32',
                blocksize=512, # 缩小采集块，压紧输入延迟
                callback=self._audio_callback
            )
            self.stream.start()
            logging.info(f"🎙️ 主持人开麦发言 (原生采样率: {self.native_rate}Hz)")
        except Exception as e:
            logging.error(f"⚠️ 主持人麦克风启动异常: {e}")

    def stop(self):
        if not self.is_recording: return
        self.is_recording = False
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception: pass
            self.stream = None
        if self.recorder:
            self.recorder.close()
            self.recorder = None
        self.pcm_slice_buffer.clear()

host_audio = HostAudioService()

# ==================== 3. UDP 席位音频流服务 ====================
def udp_audio_server_thread():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # 扩大内核 UDP 接收缓冲区，杜绝丢包断续
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
    sock.bind(("0.0.0.0", 8766))
    logging.info("🔊 UDP 席位音频推流服务就绪 (端口: 8766)")

    while True:
        try:
            data, _ = sock.recvfrom(2048)
            if len(data) > 1:
                seat_id = data[0]
                pcm_payload = data[1:]
                last_udp_audio_time[seat_id] = time.time()

                with recorder_lock:
                    rec = active_recorders.get(seat_id)
                    if rec is None or not rec.is_open:
                        seat_name = SEAT_STATES.get(seat_id, {}).get("name", f"设备{seat_id}")
                        start_seat_recorder(seat_id, seat_name)
                        rec = active_recorders.get(seat_id)
                    if rec and rec.is_open:
                        rec.write_pcm(pcm_payload)

                vol = SEAT_STATES.get(seat_id, {}).get("volume", 80)
                is_locked = SEAT_STATES.get(seat_id, {}).get("is_locked", False)
                if not is_locked:
                    audio_player.push_pcm(pcm_payload, vol)

        except Exception as e:
            logging.error(f"UDP 接收异常: {e}")

def audio_silence_watchdog():
    while True:
        time.sleep(0.5)
        now = time.time()
        for sid in list(active_recorders.keys()):
            if sid > 0:
                last_t = last_udp_audio_time.get(sid, 0)
                if last_t > 0 and (now - last_t > 1.5):
                    stop_seat_recorder(sid)
                    last_udp_audio_time[sid] = 0

# ==================== 4. 摄像头推流 ====================
def camera_thread_func():
    global latest_video_frame
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened(): cap = cv2.VideoCapture(0)

    use_synthetic = not cap.isOpened()
    if not use_synthetic:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        logging.info("📷 摄像头模式就绪 (1280x720)")

    while True:
        if not use_synthetic:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.03)
                continue
        else:
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            frame[:] = (30, 30, 30)
            cv2.putText(frame, "Smart Conference HD Screen", (340, 360),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 200), 2)

        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.rectangle(frame, (20, 20), (390, 65), (0, 0, 0), -1)
        cv2.putText(frame, f"LIVE: {time_str}", (30, 52),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 0), 2)

        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        with frame_lock:
            latest_video_frame = jpeg.tobytes()
        time.sleep(0.03)

# ==================== 5. HTTP 服务 ====================
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
                    try:
                        self.wfile.write(b'--frame\r\n')
                        self.send_header('Content-Type', 'image/jpeg')
                        self.send_header('Content-Length', len(frame))
                        self.end_headers()
                        self.wfile.write(frame)
                        self.wfile.write(b'\r\n')
                    except Exception:
                        break
                time.sleep(0.03)
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b"Smart Meeting Backend Running")

    def log_message(self, format, *args):
        return

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def start_http_server():
    server = ThreadedHTTPServer(('0.0.0.0', 8080), StreamHandler)
    logging.info("🌐 高清视频推流接口: http://127.0.0.1:8080/video_feed")
    server.serve_forever()

# ==================== 6. WebSocket 信令总线 ====================
async def safe_send(client, msg_str):
    try:
        await client.send(msg_str)
    except Exception:
        pass

async def broadcast(message_dict):
    if not CONNECTED_CLIENTS:
        return
    msg_str = json.dumps(message_dict)
    clients_snapshot = list(CONNECTED_CLIENTS)
    tasks = [safe_send(c, msg_str) for c in clients_snapshot]
    await asyncio.gather(*tasks, return_exceptions=True)

async def handle_message(websocket, message_str):
    try:
        data = json.loads(message_str)
    except Exception:
        return

    msg_type = data.get("type")
    
    raw_seat_id = data.get("seat_id")
    seat_id = None
    if raw_seat_id is not None:
        try:
            seat_id = int(raw_seat_id)
        except ValueError:
            seat_id = None

    if msg_type == "heartbeat":
        if seat_id in SEAT_STATES:
            s = SEAT_STATES[seat_id]
            s["online"] = True
            s["last_seen"] = time.time()
            s["status"] = data.get("status", s["status"])
            s["volume"] = data.get("volume", s["volume"])
            s["is_locked"] = data.get("is_locked", s["is_locked"])
            await broadcast({
                "type": "seat_update",
                "seat_id": seat_id,
                "name": s["name"],
                "online": True,
                "status": s["status"],
                "volume": s["volume"],
                "is_locked": s["is_locked"]
            })

    elif msg_type == "request_speak":
        if seat_id in SEAT_STATES:
            s = SEAT_STATES[seat_id]
            s["status"] = "SPEAKING"
            s["online"] = True
            start_seat_recorder(seat_id, s["name"])
            await broadcast({
                "type": "seat_update",
                "seat_id": seat_id,
                "name": s["name"],
                "online": True,
                "status": "SPEAKING",
                "volume": s["volume"],
                "is_locked": s["is_locked"]
            })

    elif msg_type == "release_speak":
        if seat_id in SEAT_STATES:
            s = SEAT_STATES[seat_id]
            s["status"] = "IDLE"
            s["online"] = True
            stop_seat_recorder(seat_id)
            await broadcast({
                "type": "seat_update",
                "seat_id": seat_id,
                "name": s["name"],
                "online": True,
                "status": "IDLE",
                "volume": s["volume"],
                "is_locked": s["is_locked"]
            })

    elif msg_type == "host_control":
        action = data.get("action")
        if action == "start":
            HOST_STATE["status"] = "SPEAKING"
            host_audio.start()
        elif action == "stop":
            HOST_STATE["status"] = "IDLE"
            host_audio.stop()
        await broadcast({"type": "host_update", **HOST_STATE})

    elif msg_type == "set_name":
        if seat_id in SEAT_STATES and "name" in data:
            SEAT_STATES[seat_id]["name"] = data["name"]
            logging.info(f"🏷️ [席位 {seat_id:02d}] 别名更新为 -> {data['name']}")
            await broadcast({
                "type": "seat_update",
                "seat_id": seat_id,
                "name": data["name"],
                "online": SEAT_STATES[seat_id]["online"],
                "status": SEAT_STATES[seat_id]["status"],
                "volume": SEAT_STATES[seat_id]["volume"],
                "is_locked": SEAT_STATES[seat_id]["is_locked"]
            })

    elif msg_type in ["set_status", "set_volume", "set_lock"]:
        if seat_id in SEAT_STATES:
            s = SEAT_STATES[seat_id]
            if "status" in data:
                s["status"] = data["status"]
                if s["status"] == "SPEAKING":
                    start_seat_recorder(seat_id, s["name"])
                else:
                    stop_seat_recorder(seat_id)
            if "volume" in data:
                try: s["volume"] = int(data["volume"])
                except ValueError: pass
            if "is_locked" in data:
                s["is_locked"] = bool(data["is_locked"])

            control_packet = {
                "type": msg_type,
                "seat_id": seat_id,
                "status": s["status"],
                "volume": s["volume"],
                "is_locked": s["is_locked"]
            }
            await broadcast(control_packet)
            
            await broadcast({
                "type": "seat_update",
                "seat_id": seat_id,
                "name": s["name"],
                "online": s["online"],
                "status": s["status"],
                "volume": s["volume"],
                "is_locked": s["is_locked"]
            })

    elif msg_type == "request_summary":
        logging.info("🤖 收到大模型会议纪要提炼请求...")
        def _async_summary():
            summary_md = generate_meeting_summary(MEETING_TRANSCRIPTS)
            if async_loop and CONNECTED_CLIENTS:
                asyncio.run_coroutine_threadsafe(
                    broadcast({
                        "type": "meeting_summary_result",
                        "summary": summary_md
                    }),
                    async_loop
                )
        threading.Thread(target=_async_summary, daemon=True).start()

    elif msg_type == "clear_transcripts":
        MEETING_TRANSCRIPTS.clear()
        await broadcast({"type": "transcripts_cleared"})

async def ws_handler(websocket):
    CONNECTED_CLIENTS.add(websocket)
    remote_ip = getattr(websocket, "remote_address", ["未知"])[0]
    logging.info(f"🌐 客户端已接入: {remote_ip}")

    for sid, s in SEAT_STATES.items():
        await safe_send(websocket, json.dumps({
            "type": "seat_update",
            "seat_id": sid,
            "name": s["name"],
            "online": s["online"],
            "status": s["status"],
            "volume": s["volume"],
            "is_locked": s["is_locked"]
        }))
    await safe_send(websocket, json.dumps({"type": "host_update", **HOST_STATE}))
    
    if MEETING_TRANSCRIPTS:
        await safe_send(websocket, json.dumps({
            "type": "transcripts_history",
            "list": MEETING_TRANSCRIPTS
        }))

    try:
        async for message in websocket:
            await handle_message(websocket, message)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        CONNECTED_CLIENTS.discard(websocket)
        logging.info(f"🔌 客户端断开: {remote_ip}")

def start_ws_server():
    global async_loop
    async def main():
        global async_loop
        async_loop = asyncio.get_running_loop()
        logging.info("==========================================")
        logging.info("   PC 智慧会议中控总服务 (ASR + LLM 集成版)   ")
        logging.info("👉 WebSocket 端口: 8765 | UDP 端口: 8766")
        logging.info("==========================================")
        async with websockets.serve(ws_handler, "0.0.0.0", 8765, ping_interval=20, ping_timeout=20):
            await asyncio.Future()

    asyncio.run(main())

# ==================== 7. 主入口 ====================
if __name__ == "__main__":
    t_udp = threading.Thread(target=udp_audio_server_thread, daemon=True)
    t_udp.start()

    t_watchdog = threading.Thread(target=audio_silence_watchdog, daemon=True)
    t_watchdog.start()

    t_cam = threading.Thread(target=camera_thread_func, daemon=True)
    t_cam.start()

    t_http = threading.Thread(target=start_http_server, daemon=True)
    t_http.start()

    try:
        start_ws_server()
    except (KeyboardInterrupt, SystemExit):
        logging.info("收到退出信号，正在安全释放硬件资源...")
    finally:
        try:
            if audio_player and audio_player.stream:
                audio_player.stream.stop()
                audio_player.stream.close()
        except Exception: pass
        try: host_audio.stop()
        except Exception: pass
        logging.info("中控服务已彻底退出。")
        os._exit(0)