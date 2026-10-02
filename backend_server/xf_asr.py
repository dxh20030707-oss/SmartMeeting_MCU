import base64
import datetime
import hashlib
import hmac
import json
import ssl
import time
from urllib.parse import urlencode
import websocket

# ==================== 请填入你的讯飞凭证 ====================
XF_APPID = "1c824b08"
XF_API_KEY = "209c3350d7a4b8e410631b3d67d0a2fe"
XF_API_SECRET = "MDAzNTNhMmJjOTYwNWZhNzQ1MjVmMWYx"

class XfAsrClient:
    def __init__(self, wav_path):
        self.wav_path = wav_path
        self.result_text = ""
        self.ws = None

    def create_url(self):
        url = 'wss://iat-api.xfyun.cn/v2/iat'
        now = datetime.datetime.now(datetime.timezone.utc)
        date = now.strftime('%a, %d %b %Y %H:%M:%S GMT')

        signature_origin = f"host: iat-api.xfyun.cn\ndate: {date}\nGET /v2/iat HTTP/1.1"
        signature_sha = hmac.new(
            XF_API_SECRET.encode('utf-8'),
            signature_origin.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        signature_sha = base64.b64encode(signature_sha).decode('utf-8')

        authorization_origin = (
            f'api_key="{XF_API_KEY}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature_sha}"'
        )
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode('utf-8')

        v = {'authorization': authorization, 'date': date, 'host': 'iat-api.xfyun.cn'}
        return url + '?' + urlencode(v)

    def on_message(self, ws, message):
        try:
            msg = json.loads(message)
            code = msg.get("code")
            if code != 0:
                print(f"[ASR 错误码 {code}]: {msg.get('message')}")
                return

            data = msg.get("data", {}).get("result", {}).get("ws", [])
            for w in data:
                for cw in w.get("cw", []):
                    self.result_text += cw.get("w", "")
        except Exception as e:
            print(f"[ASR 解析异常]: {e}")

    def on_error(self, ws, error):
        print(f"[ASR 异常]: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        pass

    def on_open(self, ws):
        def run():
            frame_size = 8000  # 每次发送 8KB 音频数据
            status = 0
            with open(self.wav_path, "rb") as fp:
                # 跳过 44 字节的标准 WAV Header，只传原始 PCM
                fp.seek(44)
                while True:
                    buf = fp.read(frame_size)
                    if not buf:
                        status = 2  # 最后一帧
                    
                    if status == 0:
                        d = {
                            "common": {"app_id": XF_APPID},
                            "business": {
                                "language": "zh_cn",
                                "domain": "iat",
                                "accent": "mandarin",
                                "dwa": "wpgs",
                                "pd": "game"
                            },
                            "data": {
                                "status": 0,
                                "format": "audio/L16;rate=16000",
                                "encoding": "raw",
                                "audio": base64.b64encode(buf).decode('utf-8')
                            }
                        }
                        status = 1
                    elif status == 1:
                        d = {
                            "data": {
                                "status": 1,
                                "format": "audio/L16;rate=16000",
                                "encoding": "raw",
                                "audio": base64.b64encode(buf).decode('utf-8')
                            }
                        }
                    elif status == 2:
                        d = {
                            "data": {
                                "status": 2,
                                "format": "audio/L16;rate=16000",
                                "encoding": "raw",
                                "audio": ""
                            }
                        }
                    
                    ws.send(json.dumps(d))
                    if status == 2:
                        break
                    time.sleep(0.04)
            time.sleep(0.5)
            ws.close()

        import threading
        threading.Thread(target=run).start()

    def transcribe(self):
        """执行识别并返回识别出的文本"""
        if not XF_APPID or XF_APPID == "你的_APPID":
            return "[提示: 请在 xf_asr.py 填入有效的讯飞 API 凭证]"
            
        ws_url = self.create_url()
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        self.ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        return self.result_text.strip()