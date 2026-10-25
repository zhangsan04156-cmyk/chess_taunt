# -*- coding: utf-8 -*-
"""
嘴砲西洋棋:你 vs Stockfish,電腦「在必要時」用 Groq 產生嘲諷台詞,並用 TTS 唸出來

安裝:
    pip install pygame-ce python-chess groq edge-tts pyttsx3
    並安裝 Stockfish(免費西洋棋引擎):https://stockfishchess.org/download/

環境變數:
    GROQ_API_KEY=你的key
    STOCKFISH_PATH=引擎路徑     (若 stockfish 已在 PATH 中可省略)
    GROQ_MODEL=模型名稱         (可省略)

操作:滑鼠點選棋子再點目標格 | R 重新開始 | M 靜音 | 上下鍵調音量 | ESC 離開
"""
import asyncio
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass

import chess
import chess.engine
import pygame

try:
    from groq import Groq
except ImportError:
    Groq = None
try:
    import edge_tts
except ImportError:
    edge_tts = None
try:
    import pyttsx3  # 離線語音後備(可選)
except ImportError:
    pyttsx3 = None

# =====================================================================
#  設定區
# =====================================================================
# --- 模型與語音 ---
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")   # 20b 比 120b 快;想更聰明可改 openai/gpt-oss-120b
TTS_VOICE = "zh-TW-YunJheNeural"      # 男聲;女聲:zh-TW-HsiaoChenNeural
TTS_RATE = "+25%"                     # 語速:"+0%" 正常,"+25%" 較快,"+40%" 很快
VOLUME = 0.35                         # 音量 0.0~1.0(遊戲中可用上下鍵調)
TTS_MODE = "auto"                     # "auto"=先試 edge-tts(限時),失敗立刻改離線 / "edge"=只靠 edge-tts(會重試) / "offline"=只用離線語音(最快最穩)
EDGE_TTS_TIMEOUT = 4                  # auto 模式下,edge-tts 最多等幾秒
OFFLINE_RATE = 220                    # 離線語音語速(預設約 200)
OFFLINE_TIMEOUT = 10                  # 離線語音單句最長等待秒數,超過就強制終止

# --- 電腦棋力 ---
ENGINE_THINK_TIME = 0.6               # 每步思考秒數(越大越強)
ENGINE_SKILL = 12                     # Stockfish Skill Level 0~20

# --- 什麼時候才說話(避免太吵、避免 429)---
BLUNDER_CP = 100          # 你這步讓局勢往電腦偏 1 個兵以上(100 厘兵)→ 算「失誤」
GREAT_CP = 100            # 你這步讓局勢往你偏 1 個兵以上 → 算「好棋」
CRITICAL_CP = 300         # 偏移超過 3 個兵 → 重大事件,無視冷卻一定說話
COOLDOWN_MOVES = 2        # 說話後,至少隔幾步才能再說(重大事件除外)
OPENING_SILENT_MOVES = 2  # 前幾回合保持安靜(重大事件除外)
MIN_SECONDS_BETWEEN_CALLS = 20   # 兩次呼叫 Groq 之間至少隔幾秒
EDGE_TTS_RETRIES = 3       # edge-tts 偶爾會隨機失敗,失敗時最多重試幾次(每次間隔會拉長)
TAUNT_MAX_WAIT = 2.5      # 說話的那一步,最多讓棋子多等幾秒;超過就先走棋、語音稍後補上
LATE_TAUNT_MAX_AGE = 8    # 語音晚到時,走棋後幾秒內到的還是會播(即使你已經下了下一步),超過才丟掉
RATE_LIMIT_BACKOFF = 60          # 遇到 429 後,暫停呼叫 Groq 幾秒

# --- 畫面 ---
SQ = 80
BOARD_PX = SQ * 8
PANEL_W = 340
WIN_W, WIN_H = BOARD_PX + PANEL_W, BOARD_PX

LIGHT = (240, 217, 181)
DARK = (181, 136, 99)
SEL = (246, 246, 105)
LAST = (205, 210, 106)
HINT = (90, 160, 90)
BG = (38, 36, 33)

PIECE_GLYPH = {
    chess.KING: "\u265A", chess.QUEEN: "\u265B", chess.ROOK: "\u265C",
    chess.BISHOP: "\u265D", chess.KNIGHT: "\u265E", chess.PAWN: "\u265F",
}
PIECE_NAME_ZH = {
    chess.KING: "國王", chess.QUEEN: "皇后", chess.ROOK: "城堡",
    chess.BISHOP: "主教", chess.KNIGHT: "騎士", chess.PAWN: "小兵",
}
PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
               chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}

USER_COLOR = chess.WHITE
COMP_COLOR = chess.BLACK

SYSTEM_PROMPT = (
    "你是一個正在跟人類下西洋棋的電腦,個性高冷的男性人類,稱對方為\"人類\""
    "用台灣繁體中文反諷(就是不要直接罵)對手。規則:"
    "1) 只講一到兩句話,不超過30個字;"
    "2) 第一個字就直接進入重點,大多數時候不要用「唉呦」「哎呀」「喔」「哈」這類語助詞開頭;"
    "3) 如果想要,可以用提供的具體棋步細節(哪顆子、有沒有吃子)來講,不要空泛;"
    "4) 全程只用中文,絕對不要出現英文字母、數字或棋盤座標(如 a7、c1)。"
    "5) 依照指定的語氣與切入角度發揮;"
    "6) 可以諷刺棋藝,但不要髒話、人身攻擊、歧視或仇恨內容。"
    "直接輸出台詞,不要加引號、不要解釋。"
)


# =====================================================================
#  工具函式
# =====================================================================
def find_stockfish():
    path = os.environ.get("STOCKFISH_PATH") or shutil.which("stockfish")
    if not path:
        for p in ("/usr/games/stockfish", "/opt/homebrew/bin/stockfish",
                  "/usr/local/bin/stockfish"):
            if os.path.exists(p):
                return p
    return path


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(PIECE_VALUE[p.piece_type] for p in board.piece_map().values()
               if p.color == color)


def game_phase(board: chess.Board) -> str:
    if board.fullmove_number <= 10:
        return "開局"
    non_pawn = sum(PIECE_VALUE[p.piece_type] for p in board.piece_map().values()
                   if p.piece_type not in (chess.PAWN, chess.KING))
    return "殘局" if non_pawn <= 14 else "中局"


def describe_move(piece_type, frm, to, captured, check) -> str:
    s = f"{PIECE_NAME_ZH[piece_type]}從 {chess.square_name(frm)} 走到 {chess.square_name(to)}"
    if captured:
        s += f",吃掉了{PIECE_NAME_ZH[captured]}"
    if check:
        s += ",並且將軍"
    return s


def eval_text(cp: int, mate_in) -> str:
    """cp 是電腦視角的分數(正=電腦占優)"""
    if mate_in is not None:
        return (f"我 {mate_in} 步內可以將死對方" if mate_in > 0
                else f"我 {abs(mate_in)} 步內會被將死")
    pawns = abs(cp) / 100
    if abs(cp) < 30:
        return "雙方勢均力敵"
    return f"我領先約 {pawns:.1f} 個兵" if cp > 0 else f"我落後約 {pawns:.1f} 個兵"


# =====================================================================
#  「什麼時候說話」的決策
# =====================================================================
@dataclass
class Decision:
    event: str
    critical: bool
    delta: int      # 這步造成的局勢變化(電腦視角,正=對電腦有利=你失誤)


class TauntPolicy:
    """只在有意義的事件發生時才說話。沒有記憶,只追蹤幾個數字。"""

    def __init__(self):
        self.last_eval = 0                # 上一次的評估(電腦視角)
        self.since_talk = COOLDOWN_MOVES  # 距離上次說話過了幾步
        self.blunders = 0                 # 玩家本局失誤次數(只是一個數字)
        self.last_reason = ""
        self.last_cp = 0
        self.last_delta = 0

    def evaluate(self, board, user, comp, cp, mate_in):
        """回傳 Decision 或 None。board 是電腦走完之後的盤面。"""
        prev = self.last_eval
        delta = cp - prev
        self.last_eval = cp
        self.last_cp, self.last_delta = cp, delta
        if delta >= BLUNDER_CP:
            self.blunders += 1

        if board.is_game_over():
            ev = "game_over_win" if board.is_checkmate() else "game_over_draw"
            return Decision(ev, True, delta)

        c = []  # (優先級, 事件名, 是否重大)
        if mate_in is not None and 0 < mate_in <= 3:
            c.append((6, "mate_threat", True))
        if comp["captured"] == chess.QUEEN:
            c.append((5, "comp_takes_queen", True))
        if user["captured"] == chess.QUEEN:
            c.append((5, "user_takes_queen", True))
        if delta >= BLUNDER_CP:
            c.append((4, "user_blunder", delta >= CRITICAL_CP))
        if delta <= -GREAT_CP:
            c.append((4, "user_great", delta <= -CRITICAL_CP))
        if prev >= 100 and cp <= -100:
            c.append((3, "reversal_bad", False))    # 電腦被逆轉
        if prev <= -100 and cp >= 100:
            c.append((3, "reversal_good", False))   # 電腦逆轉了
        if comp["captured"] == chess.ROOK:
            c.append((3, "comp_takes_rook", False))
        if user["captured"] == chess.ROOK:
            c.append((3, "user_takes_rook", False))
        if user["check"]:
            c.append((2, "user_check", False))
        if comp["check"]:
            c.append((2, "comp_check", False))
        if not c:
            return None
        _, ev, crit = max(c)
        return Decision(ev, crit, delta)

    def should_speak(self, d, board, groq_ready: bool) -> bool:
        ok, why = False, ""
        if d is None:
            why = "沒有觸發任何事件"
        elif d.critical:
            ok, why = True, "重大事件"
        elif board.fullmove_number <= OPENING_SILENT_MOVES:
            why = f"開局安靜期(前 {OPENING_SILENT_MOVES} 回合)"
        elif self.since_talk < COOLDOWN_MOVES:
            why = f"冷卻中(距上次說話 {self.since_talk} 步,需 {COOLDOWN_MOVES} 步)"
        elif not groq_ready:
            why = "Groq 間隔不足或正在限流"
        else:
            ok, why = True, "符合條件"
        self.last_reason = why
        self.since_talk = 0 if ok else self.since_talk + 1
        return ok


# =====================================================================
#  給 Groq 的局勢描述 (info)
# =====================================================================
# 事件 → (事件說明, 語氣)
EVENT_TEXT = {
    "game_over_win":   ("我把玩家將死了,比賽結束,我贏了", "囂張慶祝"),
    "game_over_draw":  ("比賽以和棋收場", "不屑、覺得沒打過癮"),
    "game_over_lose":  ("玩家把我將死了,我輸了", "不甘心、嘴硬找藉口"),
    "mate_threat":     ("我已經算出幾步內可以將死玩家", "勝券在握、慢條斯理"),
    "comp_takes_queen": ("我剛吃掉了玩家的皇后", "得意忘形"),
    "user_takes_queen": ("玩家剛吃掉了我的皇后", "震驚、心痛但嘴硬"),
    "comp_takes_rook": ("我剛吃掉了玩家的城堡", "得意"),
    "user_takes_rook": ("玩家剛吃掉了我的城堡", "不服氣"),
    "user_blunder":    ("玩家剛走出一步明顯的失誤,局勢大幅偏向我", "嘲笑、不敢置信"),
    "user_great":      ("玩家剛走出一步好棋,我的局勢變差了", "驚訝但嘴硬、假裝無所謂"),
    "reversal_good":   ("局勢逆轉,我從劣勢翻到了優勢", "囂張、反殺"),
    "reversal_bad":    ("局勢逆轉,我從優勢被翻成了劣勢", "心虛、裝鎮定"),
    "user_check":      ("玩家剛將了我一軍", "緊張但裝作不在乎"),
    "comp_check":      ("我剛將了玩家一軍", "挑釁"),
}
ANGLES = ["用比喻", "反問句", "假裝同情", "誇張驚嘆", "冷靜陳述事實",
          "假裝給建議", "裝無辜", "老師指導學生的口氣", "宣布戰況的球評口吻"]


def build_info(event, board, user=None, comp=None, cp=0, mate_in=None,
               delta=0, blunders=0) -> str:
    fact, tone = EVENT_TEXT[event]
    lines = [f"【事件】{fact}"]
    facts = []
    if user:
        facts.append("玩家(白棋)剛剛用" + describe_move(
            user["piece"], user["frm"], user["to"], user["captured"], user["check"]))
    if comp:
        facts.append("我(黑棋)接著用" + describe_move(
            comp["piece"], comp["frm"], comp["to"], comp["captured"], comp["check"]))
    if facts:
        lines.append("【棋步】" + ";".join(facts))

    diff = material(board, COMP_COLOR) - material(board, USER_COLOR)
    mat = "子力相當" if diff == 0 else (f"我的子力多 {diff} 分" if diff > 0
                                        else f"我的子力少 {-diff} 分")
    if event.startswith("game_over"):
        lines.append(f"【局勢】全局共 {board.fullmove_number} 回合。")
    else:
        lines.append(f"【局勢】第 {board.fullmove_number} 回合,{game_phase(board)}。"
                     f"{eval_text(cp, mate_in)};{mat}(兵1、騎士主教3、城堡5、皇后9)。")
    if blunders:
        lines.append(f"【玩家表現】本局玩家已經失誤 {blunders} 次。")
    lines.append(f"【語氣】{tone}")
    lines.append(f"【切入角度】{random.choice(ANGLES)}")
    lines.append("請根據以上資訊,用一兩句話嘲諷玩家。")
    return "\n".join(lines)


# =====================================================================
#  嘲諷產生 + 語音
# =====================================================================
FALLBACK = {
    "win": ["將死。這局到此為止。", "結束了,下次記得先學開局。"],
    "lose": ["……這局算我讓你。", "一時大意而已,再來!"],
    "draw": ["和棋?算你走運。"],
    "bad": ["這步棋,我的 CPU 都笑了。", "你確定要這樣走嗎?"],
    "good": ["哼,碰巧而已。", "這步還行,但別得意。"],
    "neutral": ["繼續啊,我在看。", "有趣的一步。"],
}


def fallback_line(event: str) -> str:
    if event in ("game_over_win", "mate_threat", "comp_takes_queen", "comp_takes_rook",
                 "reversal_good", "comp_check"):
        k = "win"
    elif event == "game_over_lose":
        k = "lose"
    elif event == "game_over_draw":
        k = "draw"
    elif event == "user_blunder":
        k = "bad"
    elif event in ("user_great", "user_takes_queen", "user_takes_rook", "reversal_bad"):
        k = "good"
    else:
        k = "neutral"
    return random.choice(FALLBACK[k])


_TTS_ALLOWED = re.compile(r"[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\x20-\x7e]")


def clean_for_tts(text: str) -> str:
    """移除 emoji、控制字元等 edge-tts 可能唸不出來的東西。"""
    text = text.replace("\n", " ").replace("…", "。")
    text = _TTS_ALLOWED.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


_OFFLINE_SCRIPT = r"""
import sys, pyttsx3
text = open(sys.argv[1], encoding="utf-8").read()
path, rate = sys.argv[2], int(sys.argv[3])
eng = pyttsx3.init()
eng.setProperty("rate", rate)
voices = eng.getProperty("voices")
def find(keys):
    for v in voices:
        if any(k in (v.name + " " + v.id).lower() for k in keys):
            return v
v = find(["zhiwei"]) or find(["zh-tw", "taiwan", "yating", "hanhan"]) or find(["zh-", "chinese", "中文"])
if v:
    eng.setProperty("voice", v.id)
print("VOICE:" + (v.name if v else "NONE"), flush=True)
eng.save_to_file(text, path)
eng.runAndWait()
"""


class Taunter:
    def __init__(self, out_queue: queue.Queue):
        self.q = out_queue
        key = os.environ.get("GROQ_API_KEY")
        # max_retries=0:遇到 429 不要自動重試(重試只會讓限流更嚴重)
        self.client = Groq(api_key=key, max_retries=0, timeout=15) if (Groq and key) else None
        self.tmpdir = tempfile.mkdtemp(prefix="chess_tts_")
        self.edge_fail_count = 0
        self.edge_disabled_until = 0.0
        self.offline_pool = ThreadPoolExecutor(max_workers=2)
        self.mode = TTS_MODE
        if self.mode == "offline" and not pyttsx3:
            self.mode = "auto"
        self.last_call = 0.0
        self.backoff_until = 0.0

    def groq_ready(self) -> bool:
        now = time.monotonic()
        return (self.client is not None and now >= self.backoff_until
                and now - self.last_call >= MIN_SECONDS_BETWEEN_CALLS)

    def _ask_groq(self, event: str, info: str) -> str:
        if not self.client or time.monotonic() < self.backoff_until:
            return fallback_line(event)
        self.last_call = time.monotonic()
        try:
            resp = self.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": info}],
                temperature=1.0,
                max_tokens=600,  # 推理型模型會先思考,token 要留多一點
                extra_body={"reasoning_effort": "low"},
            )
            text = (resp.choices[0].message.content or "").strip().strip('"「」')
            return text or fallback_line(event)
        except Exception as e:
            if getattr(e, "status_code", None) == 429:
                wait = RATE_LIMIT_BACKOFF
                try:
                    wait = max(wait, int(float(e.response.headers.get("retry-after", 0))) + 1)
                except Exception:
                    pass
                self.backoff_until = time.monotonic() + wait
            return fallback_line(event)

    def _tts_edge(self, text: str, attempts: int, timeout):
        """呼叫 edge-tts。timeout 是每次嘗試的時間上限(秒),None 表示不限時。"""
        async def run(path):
            comm = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE)
            await asyncio.wait_for(comm.save(path), timeout=timeout)

        for attempt in range(attempts):
            path = os.path.join(self.tmpdir, f"{uuid.uuid4().hex}.mp3")
            try:
                asyncio.run(run(path))
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    self.edge_fail_count = 0
                    return path
            except Exception:
                pass
            if attempt < attempts - 1:
                time.sleep(0.6 * (attempt + 1))
        self.edge_fail_count += 1
        if self.edge_fail_count >= 2:   # 連續兩句都失敗 → 暫停 edge-tts 一分鐘
            self.edge_disabled_until = time.monotonic() + 60
            self.edge_fail_count = 0
        return None

    def _offline_synth(self, text: str):
        """離線語音合成。每一句都開一個全新的子程序來做。

        pyttsx3 在同一個程式裡重複呼叫 runAndWait() 常常會卡死(第一次正常、之後全部卡住),
        所以改成「每句一個獨立子程序」:永遠都是第一次呼叫,卡住也能被強制終止。
        """
        if not pyttsx3:
            return None
        base = os.path.join(self.tmpdir, uuid.uuid4().hex)
        txt_path, wav_path = base + ".txt", base + ".wav"
        try:
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # 不要跳出黑視窗
            r = subprocess.run(
                [sys.executable, "-c", _OFFLINE_SCRIPT, txt_path, wav_path, str(OFFLINE_RATE)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=OFFLINE_TIMEOUT, **kwargs)
            return wav_path if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0 else None
        except subprocess.TimeoutExpired:
            return None
        except Exception as e:
            return None

    def _tts(self, text: str):
        speak_text = clean_for_tts(text)
        if not speak_text:
            return None

        # 離線版與 edge-tts「同時」開始合成,edge-tts 失敗或逾時時馬上就有離線版可用
        off_fut = None
        if self.mode in ("auto", "offline") and pyttsx3:
            off_fut = self.offline_pool.submit(self._offline_synth, speak_text)

        def offline_result():
            try:
                return off_fut.result(timeout=OFFLINE_TIMEOUT + 2) if off_fut else None
            except Exception:
                return None

        if self.mode == "offline":
            return offline_result()

        if edge_tts and time.monotonic() >= self.edge_disabled_until:
            if self.mode == "auto":
                path = self._tts_edge(speak_text, attempts=1, timeout=EDGE_TTS_TIMEOUT)
            else:
                path = self._tts_edge(speak_text, attempts=EDGE_TTS_RETRIES, timeout=None)
            if path:
                return path

        if off_fut is None and pyttsx3:
            off_fut = self.offline_pool.submit(self._offline_synth, speak_text)
        return offline_result()

    def make_taunt(self, event: str, info: str):
        """同步產生 (台詞, 音檔)。會花幾秒,請在背景執行緒呼叫。"""
        text = self._ask_groq(event, info)
        mp3 = self._tts(text)
        return text, mp3

    def taunt_async(self, event: str, info: str):
        def work():
            text, mp3 = self.make_taunt(event, info)
            self.q.put(("taunt", text, mp3))
        threading.Thread(target=work, daemon=True).start()


# =====================================================================
#  遊戲
# =====================================================================
class Game:
    def __init__(self):
        pygame.init()
        pygame.mixer.init()
        self.screen = pygame.display.set_mode((WIN_W, WIN_H))
        pygame.display.set_caption("嘴砲西洋棋 — 你 vs Stockfish")
        self.clock = pygame.time.Clock()
        self.piece_font = pygame.font.SysFont(
            "segoeuisymbol,applesymbols,dejavusans,notosanssymbols2,arialunicodems", 58)
        cjk = ("microsoftjhenghei,pingfangtc,notosanscjktc,notosanscjk,"
               "wenquanyimicrohei,arialunicodems,simhei")
        self.font = pygame.font.SysFont(cjk, 20)
        self.small = pygame.font.SysFont(cjk, 16)
        self.big = pygame.font.SysFont(cjk, 26, bold=True)

        path = find_stockfish()
        if not path:
            print("找不到 Stockfish,請安裝並設定 STOCKFISH_PATH。")
            sys.exit(1)
        self.engine = chess.engine.SimpleEngine.popen_uci(path)
        try:
            self.engine.configure({"Skill Level": ENGINE_SKILL})
        except Exception:
            pass
        self.engine_lock = threading.Lock()

        self.events = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.taunter = Taunter(self.events)
        self.muted = False
        self.volume = VOLUME
        pygame.mixer.music.set_volume(self.volume)
        self.reset()

    def reset(self):
        self.game_id = getattr(self, "game_id", 0) + 1
        self.board = chess.Board()
        self.selected = None
        self.targets = set()
        self.last_move = None
        self.thinking = False
        self.history = []
        self.over_announced = False
        self.policy = TauntPolicy()
        self.taunt_text = "來吧,讓我看看你的實力。"
        self.status = "輪到你(白棋)"

    # ----- 電腦回合 -----
    def computer_turn(self, user: dict):
        board = self.board.copy()
        self.thinking = True
        self.status = "電腦思考中…"

        def work():
            try:
                with self.engine_lock:
                    res = self.engine.play(
                        board, chess.engine.Limit(time=ENGINE_THINK_TIME),
                        info=chess.engine.INFO_SCORE)
                move = res.move
                san = board.san(move)
                piece = board.piece_at(move.from_square)
                if board.is_en_passant(move):
                    cap = chess.PAWN
                else:
                    t = board.piece_at(move.to_square)
                    cap = t.piece_type if t else None
                board.push(move)
                comp = dict(piece=piece.piece_type, frm=move.from_square,
                            to=move.to_square, captured=cap, check=board.is_check())

                # 評估分數(電腦視角)
                cp, mate_in = 0, None
                if "score" in res.info:
                    s = res.info["score"].pov(COMP_COLOR)
                    mate_in = s.mate() if s.is_mate() else None
                    cp = max(-1000, min(1000, s.score(mate_score=10000)))

                # 決定要不要說話
                decision = self.policy.evaluate(board, user, comp, cp, mate_in)
                speak = self.policy.should_speak(decision, board, self.taunter.groq_ready())

                if not speak:
                    self.events.put(("move", move, san, None, None))
                    return

                info = build_info(decision.event, board, user, comp, cp, mate_in,
                                  decision.delta, self.policy.blunders)
                gid = self.game_id
                fut = self.pool.submit(self.taunter.make_taunt, decision.event, info)
                try:
                    # 在 TAUNT_MAX_WAIT 秒內準備好 → 棋子移動和語音同時出現
                    text, mp3 = fut.result(timeout=TAUNT_MAX_WAIT)
                    self.events.put(("move", move, san, text, mp3))
                except FuturesTimeout:
                    # 來不及 → 先走棋,語音準備好再補上(若你已經下一步就丟棄)
                    self.events.put(("move", move, san, None, None))
                    t_move = time.monotonic()
                    text, mp3 = fut.result()
                    self.events.put(("late_taunt", text, mp3, gid, t_move))
            except Exception as e:
                self.events.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    # ----- 背景事件 -----
    def pump_events(self):
        while not self.events.empty():
            ev = self.events.get()
            if ev[0] == "move":
                _, move, san, text, mp3 = ev
                self.board.push(move)
                self.last_move = move
                self.history.append(san)
                self.thinking = False
                self.status = "輪到你(白棋)"
                if text:  # 沒說話的回合不會更動文字、也沒有語音
                    self.show_taunt(text, mp3)
                self.check_game_over()
            elif ev[0] == "taunt":
                _, text, mp3 = ev
                self.show_taunt(text, mp3)
            elif ev[0] == "late_taunt":
                _, text, mp3, gid, t_move = ev
                age = time.monotonic() - t_move
                if gid == self.game_id and age <= LATE_TAUNT_MAX_AGE:
                    self.show_taunt(text, mp3)
            elif ev[0] == "error":
                self.thinking = False
                self.status = "引擎錯誤:" + ev[1]

    def show_taunt(self, text, mp3):
        self.taunt_text = text
        if mp3 and not self.muted:
            try:
                pygame.mixer.music.set_volume(self.volume)
                pygame.mixer.music.load(mp3)
                pygame.mixer.music.play()
            except Exception:
                pass

    def check_game_over(self):
        if self.board.is_game_over() and not self.over_announced:
            self.over_announced = True
            out = self.board.outcome()
            if out and out.winner == chess.WHITE:
                self.status = "你贏了!(按 R 重來)"
                info = build_info("game_over_lose", self.board,
                                  cp=-1000, blunders=self.policy.blunders)
                self.taunter.taunt_async("game_over_lose", info)
            elif out and out.winner == chess.BLACK:
                self.status = "電腦獲勝!(按 R 重來)"
            else:
                self.status = "和棋(按 R 重來)"

    # ----- 滑鼠 -----
    def on_click(self, pos):
        if self.thinking or self.board.is_game_over() or self.board.turn != chess.WHITE:
            return
        x, y = pos
        if x >= BOARD_PX:
            return
        sq = chess.square(x // SQ, 7 - y // SQ)
        piece = self.board.piece_at(sq)

        if self.selected is not None and sq in self.targets:
            move = chess.Move(self.selected, sq)
            p = self.board.piece_at(self.selected)
            if p.piece_type == chess.PAWN and chess.square_rank(sq) == 7:
                move.promotion = chess.QUEEN
            san = self.board.san(move)
            if self.board.is_en_passant(move):
                cap = chess.PAWN
            else:
                t = self.board.piece_at(sq)
                cap = t.piece_type if t else None
            self.board.push(move)
            user = dict(piece=p.piece_type, frm=move.from_square, to=sq,
                        captured=cap, check=self.board.is_check())
            self.last_move = move
            self.history.append(san)
            self.selected, self.targets = None, set()
            if self.board.is_game_over():
                self.check_game_over()
            else:
                self.computer_turn(user)
            return

        if piece and piece.color == chess.WHITE:
            self.selected = sq
            self.targets = {m.to_square for m in self.board.legal_moves
                            if m.from_square == sq}
        else:
            self.selected, self.targets = None, set()

    # ----- 繪圖 -----
    def draw_board(self):
        for r in range(8):
            for f in range(8):
                sq = chess.square(f, r)
                rect = pygame.Rect(f * SQ, (7 - r) * SQ, SQ, SQ)
                color = LIGHT if (f + r) % 2 == 1 else DARK
                if self.last_move and sq in (self.last_move.from_square, self.last_move.to_square):
                    color = LAST
                if sq == self.selected:
                    color = SEL
                pygame.draw.rect(self.screen, color, rect)
                if sq in self.targets:
                    pygame.draw.circle(self.screen, HINT, rect.center, 10)

                piece = self.board.piece_at(sq)
                if piece:
                    g = PIECE_GLYPH[piece.piece_type]
                    fill = (250, 250, 250) if piece.color == chess.WHITE else (20, 20, 20)
                    edge = (20, 20, 20) if piece.color == chess.WHITE else (230, 230, 230)
                    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                        s = self.piece_font.render(g, True, edge)
                        self.screen.blit(s, s.get_rect(center=(rect.centerx + dx, rect.centery + dy)))
                    s = self.piece_font.render(g, True, fill)
                    self.screen.blit(s, s.get_rect(center=rect.center))

        if self.board.is_check():
            ks = self.board.king(self.board.turn)
            r = pygame.Rect(chess.square_file(ks) * SQ,
                            (7 - chess.square_rank(ks)) * SQ, SQ, SQ)
            pygame.draw.rect(self.screen, (220, 50, 50), r, 4)

    def wrap(self, text, font, width):
        lines, cur = [], ""
        for ch in text:
            if font.size(cur + ch)[0] > width:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        return lines

    def draw_panel(self):
        px = BOARD_PX
        pygame.draw.rect(self.screen, BG, (px, 0, PANEL_W, WIN_H))
        self.screen.blit(self.big.render("電腦說:", True, (255, 200, 80)), (px + 16, 14))
        y = 54
        for line in self.wrap(self.taunt_text, self.font, PANEL_W - 32):
            self.screen.blit(self.font.render(line, True, (240, 240, 240)), (px + 16, y))
            y += 28

        y = 300
        self.screen.blit(self.font.render(self.status, True, (130, 220, 130)), (px + 16, y))
        y += 34
        self.screen.blit(self.small.render("棋譜:", True, (170, 170, 170)), (px + 16, y))
        y += 24
        moves = []
        for i in range(0, len(self.history), 2):
            pair = self.history[i:i + 2]
            moves.append(f"{i // 2 + 1}. " + " ".join(pair))
        for line in moves[-9:]:
            self.screen.blit(self.small.render(line, True, (210, 210, 210)), (px + 16, y))
            y += 22
        hint = (f"R 重來 | M 靜音 | 上下鍵調音量 {int(self.volume * 100)}%"
                + (" (靜音中)" if self.muted else ""))
        self.screen.blit(self.small.render(hint, True, (150, 150, 150)),
                         (px + 16, WIN_H - 28))

    # ----- 主迴圈 -----
    def run(self):
        running = True
        while running:
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    running = False
                elif e.type == pygame.KEYDOWN:
                    if e.key == pygame.K_ESCAPE:
                        running = False
                    elif e.key == pygame.K_r and not self.thinking:
                        pygame.mixer.music.stop()
                        self.reset()
                    elif e.key == pygame.K_m:
                        self.muted = not self.muted
                        if self.muted:
                            pygame.mixer.music.stop()
                    elif e.key in (pygame.K_UP, pygame.K_DOWN):
                        step = 0.05 if e.key == pygame.K_UP else -0.05
                        self.volume = min(1.0, max(0.0, self.volume + step))
                        pygame.mixer.music.set_volume(self.volume)
                elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                    self.on_click(e.pos)
            self.pump_events()
            self.draw_board()
            self.draw_panel()
            pygame.display.flip()
            self.clock.tick(30)
        try:
            self.engine.quit()
        except Exception:
            pass
        pygame.quit()


if __name__ == "__main__":
    Game().run()