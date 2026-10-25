# chess_taunt

# ♟️ 嘴砲西洋棋 (Chess Taunt)

你 vs. Stockfish 的西洋棋小遊戲。電腦會**在關鍵時刻**用 [Groq](https://groq.com/) 的大型語言模型即時產生嘲諷台詞,再用 TTS 語音唸出來。

> 吃掉皇后、你走出大失誤、局勢逆轉、被將軍……電腦都有話要說,但不會吵個不停。

## ✨ 特色

- **圖形介面**:使用 `pygame-ce`,滑鼠點選即可下棋(你執白,電腦執黑)
- **Stockfish 引擎**:可調整思考時間與棋力 (Skill Level 0~20)
- **聰明的「何時說話」機制**:根據引擎評估分數的變化判斷事件(失誤、好棋、吃后、吃城堡、將軍、逆轉、即將被將死……),並有冷卻、開局安靜期與呼叫間隔,避免太吵與 API 限流
- **LLM 即時嘲諷**:把棋步、局勢、語氣、切入角度整理成提示詞丟給 Groq,產生一兩句台灣繁體中文台詞
- **多層語音後備**:`edge-tts`(自然語音)→ `pyttsx3`(離線語音)→ 純文字;兩者同時合成,誰先好就用誰
- **不卡棋**:台詞若來不及產生,棋子先走、語音稍後補上;太晚到的會自動丟棄
- **沒有 API Key 也能玩**:自動改用內建台詞

## 📦 安裝

需要 Python 3.9+。

```bash
pip install pygame-ce python-chess groq edge-tts pyttsx3
```

> `groq`、`edge-tts`、`pyttsx3` 皆為可選,沒裝的功能會自動降級。

另外請安裝 **Stockfish** 引擎:<https://stockfishchess.org/download/>

- macOS:`brew install stockfish`
- Ubuntu / Debian:`sudo apt install stockfish`
- Windows:下載後解壓縮,並設定 `STOCKFISH_PATH`

## 🔑 環境變數

| 變數 | 說明 | 必填 |
|------|------|------|
| `GROQ_API_KEY` | Groq API Key(到 <https://console.groq.com/> 免費申請) | 否(沒有則用內建台詞) |
| `STOCKFISH_PATH` | Stockfish 執行檔路徑(已在 PATH 中可省略) | 否 |
| `GROQ_MODEL` | 使用的模型,預設 `openai/gpt-oss-20b` | 否 |

設定範例:

```bash
# macOS / Linux
export GROQ_API_KEY="你的key"
export STOCKFISH_PATH="/usr/local/bin/stockfish"

# Windows (PowerShell)
$env:GROQ_API_KEY="你的key"
$env:STOCKFISH_PATH="C:\stockfish\stockfish.exe"
```

⚠️ **請勿把 API Key 寫進程式碼或提交到 GitHub。**

## ▶️ 執行

```bash
python chess_taunt.py
```

## 🎮 操作

| 操作 | 功能 |
|------|------|
| 滑鼠左鍵 | 點選棋子,再點目標格移動(兵升變一律升后) |
| `R` | 重新開始 |
| `M` | 靜音 / 取消靜音 |
| `↑` / `↓` | 調整音量 |
| `ESC` | 離開 |

## ⚙️ 設定

所有參數都在 `chess_taunt.py` 開頭的「設定區」:

```python
# 模型與語音
GROQ_MODEL = "openai/gpt-oss-20b"      # 想更聰明可改 openai/gpt-oss-120b
TTS_VOICE  = "zh-TW-YunJheNeural"      # 女聲:zh-TW-HsiaoChenNeural
TTS_RATE   = "+25%"                    # 語速
TTS_MODE   = "auto"                    # auto / edge / offline

# 電腦棋力
ENGINE_THINK_TIME = 0.6                # 每步思考秒數,越大越強
ENGINE_SKILL      = 12                 # 0~20

# 說話時機
BLUNDER_CP = 100                       # 你讓局勢偏向電腦 1 個兵以上 → 失誤
CRITICAL_CP = 300                      # 偏移 3 個兵以上 → 重大事件,無視冷卻
COOLDOWN_MOVES = 2                     # 說話後至少隔幾步
MIN_SECONDS_BETWEEN_CALLS = 20         # 兩次呼叫 Groq 至少隔幾秒
```

### TTS 模式

- `auto`(預設):先試 edge-tts(限時),失敗立刻改用離線語音
- `edge`:只用 edge-tts(會重試)
- `offline`:只用離線語音(最快最穩,但音質較機械)

> Windows 若離線語音沒聲音,請到「設定 → 時間與語言 → 語音」安裝**中文(台灣)**語音。

## 🧠 運作原理

```
你下一步 ──► Stockfish 回應並給出評估分數
                    │
                    ▼
          TauntPolicy 判斷事件
   (失誤 / 好棋 / 吃后 / 逆轉 / 將軍 / 即將將死…)
                    │ 要說話?
                    ▼
     build_info() 組成局勢描述 ──► Groq 產生台詞
                                      │
                                      ▼
                          edge-tts / pyttsx3 合成語音
                                      │
                                      ▼
                       棋子移動 + 顯示文字 + 播放語音
```

## 🛠️ 疑難排解

- **找不到 Stockfish**:安裝後設定 `STOCKFISH_PATH`,或確認 `stockfish` 在 PATH 中
- **Groq 429 限流**:程式會自動暫停呼叫 60 秒並改用內建台詞,也可調大 `MIN_SECONDS_BETWEEN_CALLS`
- **沒聲音**:檢查是否按了 `M` 靜音、音量是否為 0,並確認已安裝 `edge-tts` 或 `pyttsx3`(以及系統有中文語音)
- **棋子顯示成方塊 / 中文亂碼**:系統缺少對應字型,請安裝 Noto Sans CJK 或 DejaVu Sans

## 📄 授權

本專案採用 [MIT License](LICENSE) 授權。

## 🙏 致謝

[python-chess](https://github.com/niklasf/python-chess) · [Stockfish](https://stockfishchess.org/) · [Groq](https://groq.com/) · [edge-tts](https://github.com/rany2/edge-tts) · [pygame-ce](https://pyga.me/)
