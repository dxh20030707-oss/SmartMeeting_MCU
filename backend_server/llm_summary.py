import json
import urllib.request

# ==================== 大模型配置 (以兼容 OpenAI 格式为例，支持 DeepSeek 等) ====================
LLM_API_KEY = "sk-257c3a8a9a8f443e98d9567e77fcc036"
LLM_API_BASE = "https://api.deepseek.com/chat/completions"
LLM_MODEL = "deepseek-chat"

PROMPT_SYSTEM = """你是一位专业的智慧会议纪要秘书。
请根据提供的会议完整发言记录，整理出一份严谨、排版清晰的会议纪要 Markdown 文档。
包含以下模块：
1. 📌 会议主题与核心议题
2. 🗣️ 参会人员主要观点（主持人、各席位分别归纳）
3. ✅ 达成决议与共识
4. 📋 后续待办事项 (Action Items) 与跟进人
"""

def generate_meeting_summary(transcripts):
    """
    transcripts 结构为列表:
    [
        {"time": "09:30:00", "speaker": "主持人", "text": "开始讨论需求。"},
        {"time": "09:30:15", "speaker": "设备一", "text": "麦克风已就绪。"}
    ]
    """
    if not transcripts:
        return "当前会议尚无有效发言记录。"

    if not LLM_API_KEY or LLM_API_KEY == "你的_LLM_API_KEY":
        # 兜底生成简易文本总结，防止无 Key 时报错
        summary = "### 📝 会议简要记录汇总 (未配置大模型 API Key)\n\n"
        for item in transcripts:
            summary += f"- **[{item['time']}] {item['speaker']}**: {item['text']}\n"
        return summary

    history_text = "\n".join([f"[{t['time']}] {t['speaker']}: {t['text']}" for t in transcripts])
    
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": PROMPT_SYSTEM},
            {"role": "user", "content": f"以下是整场会议的发言记录：\n\n{history_text}\n\n请输出完整的结构化会议纪要："}
        ],
        "temperature": 0.3
    }

    try:
        req = urllib.request.Request(
            LLM_API_BASE,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            return res_data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"大模型纪要提炼失败: {e}"