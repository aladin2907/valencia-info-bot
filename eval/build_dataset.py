#!/usr/bin/env python3
"""Сборка эталонного датасета: реальные вопросы про школы и документы.

Берём настоящие треды из чатов, где корневое сообщение — вопрос по теме,
и просим модель переформулировать его так, как человек спросил бы у бота.
Переформулировка важна: иначе поиск найдёт тред по дословному совпадению,
и замер ничего не покажет.

На выходе: вопрос → тред-источник (ground truth) → что в нём есть по сути.
"""
import json, os, re, pathlib, random
from concurrent.futures import ThreadPoolExecutor
import urllib.request

ROOT = pathlib.Path("/Users/macbook/PetProjects/valencia_info_bot")
OUT = pathlib.Path(__file__).parent
MODEL = "stealth/ox-alpha"
KEY = next(l.split("=", 1)[1].strip() for l in open(ROOT / ".env")
           if l.startswith("OPENROUTER_API_KEY="))

SCHOOL = r"школ|школь|colegio|коллехио|садик|детсад|guarder|infantil|primaria|eso|комедор|comedor|учител|клас|запис.{0,15}школ|зачислен"
DOCS = r"nie|ние\b|tie\b|прописк|empadron|падрон|cita\s*previa|сита\b|резиденц|residencia|внж|документ|апостил|перевод.{0,20}документ|присяжн|extranjer|консульств|гражданств|nomina|autonomo"


def call(prompt: str, system: str = "", max_tokens: int = 1200, temperature: float = 0.3):
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    body = json.dumps({"model": MODEL, "messages": msgs,
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read())
            c = d["choices"][0]["message"]["content"]
            if c:
                return c
        except Exception as e:
            if attempt == 3:
                return f"__ERROR__ {e}"
    return "__ERROR__ empty"


def parse_json(text: str):
    if not text or text.startswith("__ERROR__"):
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def load_candidates():
    """Треды по темам школ/документов, где корень — вопрос."""
    out = []
    for group in ("valencia_parents_kids_schools", "it_ua_valencia", "matusi_valencia"):
        p = ROOT / "data" / group / "threads.json"
        if not p.exists():
            continue
        for t in json.load(open(p, encoding="utf-8")):
            txt = (t.get("text") or "").strip()
            if not (400 < len(txt) < 6000):
                continue
            root = txt.split("\n")[0]
            low = txt.lower()
            topic = "school" if re.search(SCHOOL, low) else ("docs" if re.search(DOCS, low) else None)
            if not topic or "?" not in root:
                continue
            out.append({"group": group, "thread_id": t["thread_id"],
                        "date": (t.get("thread_date") or "")[:10],
                        "topic": topic, "text": txt})
    return out


SYS = """Ты готовишь эталонный набор для проверки Q&A-бота по чатам экспатов в Валенсии.
Тебе дан реальный тред из чата. Верни СТРОГО JSON:
{
 "usable": true/false,           // годится ли тред: есть содержательный вопрос И полезный ответ в обсуждении
 "question": "...",              // как человек спросил бы это у бота: своими словами, БЕЗ копирования фраз из треда
 "topic": "school"|"docs",
 "gold_points": ["...", "..."],  // 2-5 ключевых фактов из ОБСУЖДЕНИЯ, которые обязан содержать хороший ответ
 "language": "ru"|"uk"
}
Правила: question не должен дословно повторять текст треда — перефразируй.
Если в треде нет полезного ответа (только вопрос без ответов, флуд, оффтоп) — usable: false."""


def process(item):
    res = parse_json(call(f"Тред:\n{item['text'][:5000]}", system=SYS, max_tokens=1500))
    if not res or not res.get("usable") or not res.get("question"):
        return None
    return {"question": res["question"].strip(), "topic": res.get("topic", item["topic"]),
            "language": res.get("language", "ru"), "gold_points": res.get("gold_points", []),
            "source_thread_id": item["thread_id"], "source_group": item["group"],
            "source_date": item["date"], "source_text": item["text"]}


def main():
    random.seed(42)
    cands = load_candidates()
    by_topic = {"school": [c for c in cands if c["topic"] == "school"],
                "docs": [c for c in cands if c["topic"] == "docs"]}
    print(f"кандидатов: школы={len(by_topic['school'])} документы={len(by_topic['docs'])}")

    picked = random.sample(by_topic["school"], min(45, len(by_topic["school"]))) + \
             random.sample(by_topic["docs"], min(45, len(by_topic["docs"])))
    print(f"отправляю на разметку: {len(picked)}")

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = [r for r in ex.map(process, picked) if r]

    json.dump(rows, open(OUT / "dataset.json", "w"), ensure_ascii=False, indent=1)
    print(f"годных вопросов: {len(rows)}")
    for r in rows[:5]:
        print(f"  [{r['topic']}/{r['language']}] {r['question'][:90]}")


if __name__ == "__main__":
    main()
