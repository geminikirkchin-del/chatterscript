# 手動搬移 / 匯出 Kimi Code 對話歷史完整指南

> 適用場景：你把項目資料夾搬了位置，想保留舊 session 的對話記錄；或者想把某個 session 的歷史另存成可讀 Markdown。

---

## 0. 事前準備

- Windows 10/11
- Python 3.10+（用來解析 `wire.jsonl`）
- 檔案總管或 Git Bash / PowerShell
- 知道你想找的 **session ID**，例如 `session_0c066302-f133-4fd7-bfa3-f1eb952ab8b7`

---

## 1. 定位 Kimi Code 的 session 存放位置

Kimi Code 把 session 存在你的使用者目錄下：

```text
C:\Users\<你的用戶名>\.kimi-code\sessions\
```

例如本機 Administrator 帳號就是：

```text
C:\Users\Administrator\.kimi-code\sessions\
```

### 1.1 用檔案總管打開

1. 按 `Win + R`
2. 貼上 `%USERPROFILE%\.kimi-code\sessions`
3. 按 Enter

你會看到類似這樣的資料夾：

```text
sessions/
├── wd_chatterscript-kw_56ad1637f1b7/
│   ├── session_0c066302-f133-4fd7-bfa3-f1eb952ab8b7/
│   └── session_xxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/
└── wd_some-other-project_xxxxxxxxxxxx/
    └── session_yyyyyyyy-.../
```

每個 `wd_...` 對應一個工作目錄（workspace），裡面有一個或多個 `session_...` 資料夾。

### 1.2 快速用命令列搜尋 session ID

打開 Git Bash，執行：

```bash
find ~/.kimi-code/sessions -maxdepth 2 -type d -name '*0c066302*'
```

把 `0c066302` 換成你要找的 session ID 前幾碼即可。

---

## 2. 確認這是否你要的 session

進入該 session 資料夾，打開 `state.json`。裡面會告訴你：

```json
{
  "createdAt": "2026-07-24T17:55:58.369Z",
  "updatedAt": "2026-08-15T09:12:46.858Z",
  "title": "chatterscript-tts-kw",
  "workDir": "D:/kimi-code-other-project/chatterscript",
  "lastPrompt": "i moved the folder to ..."
}
```

重點看三項：

| 欄位 | 意義 |
|---|---|
| `title` | 你在 Kimi 設定的 session 名稱 |
| `workDir` | 這個 session 綁定的工作目錄 |
| `lastPrompt` | 最後一則提示，通常最能確認內容 |

---

## 3. 把歷史檔案複製到新項目位置

假設：

- 舊 session：`C:\Users\Administrator\.kimi-code\sessions\wd_chatterscript-kw_56ad1637f1b7\session_0c066302-...`
- 新項目位置：`D:\kimi-code-other-project\chatterscript`

### 3.1 推薦複製的檔案

session 資料夾內主要關心以下檔案：

```text
session_0c066302-.../
├── state.json                 ← session 基本資料
├── logs/
│   └── kimi-code.log          ← 執行日誌
└── agents/
    ├── main/
    │   └── wire.jsonl         ← 主對話歷史（最大、最重要）
    ├── agent-0/wire.jsonl     ← 子 agent 對話
    ├── agent-1/wire.jsonl
    └── ...
```

> `wire.jsonl` 是 Kimi Code 的內部格式，人類無法直接閱讀，後面會教你轉成 Markdown。

### 3.2 複製方式 A：用檔案總管

1. 在新項目資料夾內建立 `session_history_0c066302/`
2. 把 `state.json` 拖進去
3. 建立 `logs/` 子資料夾，把 `kimi-code.log` 拖進去
4. 建立 `agents/` 子資料夾
5. 把 `agents/main/wire.jsonl` 拖進 `agents/`，可改名為 `main_wire.jsonl`
6. 依樣複製 `agent-0` 到 `agent-N` 的 `wire.jsonl`

### 3.3 複製方式 B：用 Git Bash

```bash
# 進入新項目資料夾
cd /d/kimi-code-other-project/chatterscript

# 建立目標資料夾
mkdir -p session_history_0c066302/agents session_history_0c066302/logs

OLD="/c/Users/Administrator/.kimi-code/sessions/wd_chatterscript-kw_56ad1637f1b7/session_0c066302-f133-4fd7-bfa3-f1eb952ab8b7"

# 複製核心檔案
cp "$OLD/state.json" session_history_0c066302/
cp "$OLD/logs/kimi-code.log" session_history_0c066302/logs/
cp "$OLD/agents/main/wire.jsonl" session_history_0c066302/agents/main_wire.jsonl

# 選擇性複製子 agent
cp "$OLD/agents/agent-0/wire.jsonl" session_history_0c066302/agents/agent-0_wire.jsonl
cp "$OLD/agents/agent-1/wire.jsonl" session_history_0c066302/agents/agent-1_wire.jsonl
# ... 依此類推
```

---

## 4. 把 `wire.jsonl` 轉成可讀 Markdown

`wire.jsonl` 是 JSON Lines 格式，一行一筆事件。你需要一個小腳本抽取出 `User` 輸入、`Assistant` 回覆，以及用過哪些工具。

### 4.1 建立解析腳本

在新項目的 `session_history_0c066302/` 內建立 `extract_conversation.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Kimi Code 的 wire.jsonl 轉成可讀 Markdown。"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def ms_to_iso(ms: int | None) -> str:
    if ms is None:
        return ""
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ms)


def get_text(blocks: list[dict]) -> str:
    parts = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "image":
                parts.append("[image]")
    return "".join(parts)


def clean_user_prompt(text: str) -> str:
    """去除 skill activation 包裝，只留下用戶真正輸入。"""
    if "<kimi-skill-loaded" in text:
        m = re.search(r'<kimi-skill-loaded[^>]*args="([^"]*)"', text)
        if m:
            return m.group(1).strip()
        m = re.search(r'<kimi-skill-loaded[^>]*>\s*', text)
        if m:
            return text[m.end():].strip()
    return text.strip()


def extract_conversation(wire_path: Path) -> dict:
    turns: dict[str, dict] = {}
    prompts: list[tuple[str, str, int]] = []

    with open(wire_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = obj.get("type")
            time_ms = obj.get("time")

            if t == "turn.prompt":
                text = get_text(obj.get("input", []))
                guess_id = str(len(prompts))
                prompts.append((guess_id, text, time_ms))
                turns.setdefault(guess_id, {
                    "turn_id": guess_id,
                    "user_prompt": "",
                    "assistant_texts": [],
                    "tool_calls": [],
                    "first_time": time_ms,
                    "last_time": time_ms,
                })
                turns[guess_id]["user_prompt"] = clean_user_prompt(text)

            elif t == "context.append_loop_event":
                event = obj.get("event", {})
                ev_type = event.get("type")
                turn_id = str(event.get("turnId"))

                if turn_id not in turns:
                    turns[turn_id] = {
                        "turn_id": turn_id,
                        "user_prompt": "",
                        "assistant_texts": [],
                        "tool_calls": [],
                        "first_time": time_ms,
                        "last_time": time_ms,
                    }
                turn = turns[turn_id]
                turn["last_time"] = time_ms

                if ev_type == "content.part":
                    part = event.get("part", {})
                    if part.get("type") == "text":
                        turn["assistant_texts"].append(part.get("text", ""))
                elif ev_type == "tool.call":
                    name = event.get("name", "")
                    if name:
                        turn["tool_calls"].append({
                            "name": name,
                            "description": event.get("description", ""),
                        })

    sorted_turns = sorted(
        turns.values(),
        key=lambda x: int(x["turn_id"]) if x["turn_id"].isdigit() else 999999,
    )
    return {"turns": sorted_turns, "total_turns": len(sorted_turns),
            "total_prompts": len(prompts)}


def render_markdown(data: dict, out_path: Path) -> None:
    lines = []
    lines.append("# Session Conversation Summary")
    lines.append("")
    lines.append(f"- Total turns: {data['total_turns']}")
    lines.append(f"- User prompts: {data['total_prompts']}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for turn in data["turns"]:
        tid = turn["turn_id"]
        first = ms_to_iso(turn["first_time"])
        lines.append(f"## Turn {tid}  ({first})")
        lines.append("")

        if turn["user_prompt"]:
            lines.append("**User:**")
            lines.append("")
            lines.append("```")
            lines.append(turn["user_prompt"])
            lines.append("```")
            lines.append("")

        if turn["tool_calls"]:
            tool_names = [t["name"] for t in turn["tool_calls"]]
            lines.append(f"_Tools: {', '.join(tool_names)}_")
            lines.append("")

        if turn["assistant_texts"]:
            lines.append("**Assistant:**")
            lines.append("")
            lines.append("\n\n".join(turn["assistant_texts"]))
            lines.append("")

        lines.append("---")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def render_outline(data: dict, out_path: Path) -> None:
    lines = ["# Session Outline", ""]
    for turn in data["turns"]:
        prompt = turn["user_prompt"].replace("\n", " ").strip()
        if len(prompt) > 120:
            prompt = prompt[:117] + "..."
        tool_names = ", ".join(t["name"] for t in turn["tool_calls"])
        lines.append(f"- **Turn {turn['turn_id']}:** {prompt}")
        if tool_names:
            lines.append(f"  - Tools: {tool_names}")
    out_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    base = Path(__file__).parent
    wire = base / "agents" / "main_wire.jsonl"
    if not wire.exists():
        wire = base / "agents" / "main" / "wire.jsonl"

    data = extract_conversation(wire)
    render_markdown(data, base / "conversation_summary.md")
    render_outline(data, base / "conversation_outline.md")

    print(f"Wrote conversation_summary.md ({data['total_turns']} turns)")
    print(f"Wrote conversation_outline.md")
```

### 4.2 執行腳本

```bash
cd session_history_0c066302
python extract_conversation.py
```

輸出：

```text
Wrote conversation_summary.md (120 turns)
Wrote conversation_outline.md
```

你會得到：

| 檔案 | 用途 |
|---|---|
| `conversation_summary.md` | 完整對話，適合逐行回顧 |
| `conversation_outline.md` | 精簡大綱，適合快速掃描主題 |

---

## 5. （可選）更新 session 的 workspace 指向

如果你不是複製歷史，而是想把舊 session 直接對應到新資料夾位置，可以修改舊 session 的 `state.json`：

```json
{
  "workDir": "D:/kimi-code-other-project/chatterscript"
}
```

步驟：

1. 關閉該 session 的 Kimi Code 視窗（避免寫入衝突）
2. 用 VS Code / Notepad++ 打開舊 session 的 `state.json`
3. 把 `workDir` 改成新路徑
4. 存檔
5. 重新打開 Kimi Code，選擇該 session

> ⚠️ 注意：只改 `workDir` 並不會自動把 `agents/` 裡的快取路徑、task log 裡的絕對路徑全部更新。如果舊 session 裡很多工具結果都記錄了舊路徑，建議還是開新 session 比較乾淨。

---

## 6. 常見問題

### Q1: 為什麼 `wire.jsonl` 這麼大？

`wire.jsonl` 記錄了每一次思維鏈（thinking）、每一個 tool call / result、每一段 assistant 回覆。對於長 session，幾十 MB 很正常。轉成 Markdown 後只保留用戶輸入與 assistant 最終回覆，體積會小很多。

### Q2: 子 agent 的 `wire.jsonl` 要不要複製？

- 如果你只想看「你跟 Kimi 的主對話」，只複製 `agents/main/wire.jsonl` 就夠。
- 如果你曾經使用 `Agent` / `AgentSwarm` 做並行分析，那些結果會存在 `agent-0` 到 `agent-N`。想完整保留就一併複製。

### Q3: 可以只搬 session 不複製嗎？

可以，但不建議新手這樣做：

- 直接把 `session_<UUID>` 資料夾剪下貼到新 workspace 目錄下，然後改 `state.json` 的 `workDir`。
- 風險：如果 Kimi Code 正在執行該 session，剪下會導致資料損壞。**務必先關閉對應視窗。**

### Q4: 如何知道目前這個 session 的 ID？

目前 Kimi Code 介面不一定直接顯示。最快的方法：

1. 看 `state.json` 的 parent 資料夾名稱
2. 或用 Git Bash 下指令，根據 `workDir` 反查：

```bash
find ~/.kimi-code/sessions -maxdepth 3 -name state.json -exec grep -l 'D:/kimi-code-other-project/chatterscript' {} \;
```

---

## 7. 快速檢查清單

- [ ] 確認舊 session ID 與路徑
- [ ] 關閉舊 session 的 Kimi Code 視窗
- [ ] 在新項目建立 `session_history_<id>/`
- [ ] 複製 `state.json`、`logs/kimi-code.log`、`agents/main/wire.jsonl`
- [ ] 執行 `extract_conversation.py`
- [ ] 檢查 `conversation_summary.md` 與 `conversation_outline.md`
- [ ] （可選）修改 `workDir` 並重新載入舊 session

---

## 參考路徑（以本機為例）

```text
舊 session:
C:\Users\Administrator\.kimi-code\sessions\wd_chatterscript-kw_56ad1637f1b7\session_0c066302-f133-4fd7-bfa3-f1eb952ab8b7

新項目:
D:\kimi-code-other-project\chatterscript

產出:
D:\kimi-code-other-project\chatterscript\session_history_0c066302\conversation_summary.md
D:\kimi-code-other-project\chatterscript\session_history_0c066302\conversation_outline.md
```
