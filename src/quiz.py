"""每日答题。

流程：拉取今日题目 → 用题库对上选项 → 解 Turnstile → 提交。
题库没有这道题，或今天的选项对不上时，不提交。答错会扣大米。

题库见 questions.json，版权说明见 questions.LICENSE。
仓库根目录的 questions.local.json 会覆盖同名题目，用来补题库里没有的题。
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from loguru import logger

from .client import P3ASession
from .signer import SITEKEY, solve_turnstile

QUESTION_URL = "https://api.1point3acres.com/api/daily_questions"
QUESTION_PAGE = "https://www.1point3acres.com/next/daily-question"
_BANK_PATH = Path(__file__).with_name("questions.json")
_LOCAL_BANKS = (
    Path("questions.local.json"),
    Path(__file__).with_name("questions.local.json"),
)

AnswerSet = Tuple[str, ...]


def _norm(text: str) -> str:
    """去掉空白和句末标点，方便题目、选项和题库对得上。"""
    text = unicodedata.normalize("NFKC", text).strip().casefold()
    text = re.sub(r"\s+", "", text)
    return text.rstrip("?.!。！？")


def _as_answers(value: Any) -> AnswerSet:
    if isinstance(value, str):
        answers = (value.strip(),)
    elif isinstance(value, list):
        answers = tuple(str(item).strip() for item in value)
    else:
        return ()
    return tuple(item for item in answers if item)


def load_question_bank(*extra_paths: Path) -> Dict[str, AnswerSet]:
    """读内置题库，再用本地文件覆盖。同一道题只保留最后一份答案。"""
    paths: List[Path] = [_BANK_PATH, *extra_paths]
    for path in _LOCAL_BANKS:
        if path not in paths:
            paths.append(path)

    merged: Dict[str, Any] = {}
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            raise ValueError(f"题库必须是 JSON 对象: {path}")
        merged.update(data)
        logger.debug(f"载入题库 {path}，累计 {len(merged)} 题")

    bank: Dict[str, AnswerSet] = {}
    for question, value in merged.items():
        answers = _as_answers(value)
        if not str(question).strip() or not answers:
            continue
        key = _norm(str(question))
        # 规范化后撞车时，后加载的文件（本地覆盖）胜出。
        bank[key] = answers
    if not bank:
        raise RuntimeError("题库为空，无法答题")
    return bank


def match_choice(
    question: str,
    options: Mapping[int, str],
    bank: Mapping[str, AnswerSet],
) -> Optional[int]:
    """返回唯一对上的选项编号（1 起）。对不上或不止一个，返回 None。"""
    accepted = bank.get(_norm(question))
    if not accepted:
        return None
    accepted_norm = {_norm(answer) for answer in accepted}
    hits = [
        index
        for index, text in options.items()
        if text and _norm(text) in accepted_norm
    ]
    if len(hits) == 1:
        return hits[0]
    return None


def _headers() -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.1point3acres.com",
        "Referer": QUESTION_PAGE,
    }


def _loads(text: str, what: str) -> Dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{what}不是 JSON:\n{text[:200]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{what}格式异常: {data!r}")
    return data


def _options_from(question: Mapping[str, Any]) -> Dict[int, str]:
    options: Dict[int, str] = {}
    for index in range(1, 7):
        text = question.get(f"a{index}")
        if isinstance(text, str) and text.strip():
            options[index] = text.strip()
    return options


def _format_options(options: Mapping[int, str]) -> str:
    return " | ".join(f"{index}. {text}" for index, text in options.items())


def answer_today(
    sess: P3ASession,
    captcha_api_key: Optional[str] = None,
    bank: Optional[Mapping[str, AnswerSet]] = None,
) -> str:
    """回答今日题目。已答过视为成功。题库没有则抛错，不猜。"""
    if bank is None:
        bank = load_question_bank()

    raw = sess.get(QUESTION_URL, headers=_headers())
    data = _loads(raw, "题目响应")
    message = str(data.get("msg") or "")
    if "已经答" in message:
        logger.info(f"今日已答题：{message}")
        return "已答题 ✓"

    question = data.get("question")
    if data.get("errno") not in (0, None) or not isinstance(question, dict):
        raise RuntimeError(f"获取题目失败：{data}")

    question_id = question.get("id")
    prompt = str(question.get("qc") or "").strip()
    options = _options_from(question)
    if not question_id or not prompt or not options:
        raise RuntimeError(f"题目字段不完整：{question}")

    choice = match_choice(prompt, options, bank)
    if choice is None:
        raise RuntimeError(
            "题库没有这道题，或今天的选项对不上，已跳过提交。\n"
            f"题目：{prompt}\n"
            f"选项：{_format_options(options)}\n"
            "确认答案后可写入仓库根目录的 questions.local.json："
            '{"题目原文": "正确选项原文"}'
        )

    if not captcha_api_key:
        raise RuntimeError("缺少 captcha_api_key，无法获取 Turnstile token")

    logger.info(f"今日题目：{prompt}")
    logger.info(f"提交选项 {choice}：{options[choice]}")
    token = solve_turnstile(SITEKEY, captcha_api_key, QUESTION_PAGE)
    payload = {
        "qid": question_id,
        "answer": choice,
        "captcha_response": token,
        "hashkey": "",
        "version": 2,
    }
    resp_text = sess.post(
        QUESTION_URL,
        data=json.dumps(payload),
        headers=_headers(),
    )
    result = _loads(resp_text, "答题响应")
    result_msg = str(result.get("msg") or "")
    if "人机验证出错" in result_msg:
        raise RuntimeError(f"答题人机验证失败：{result}")
    if result.get("errno") == 0 or "已经答" in result_msg:
        logger.success(f"✅ {result_msg or '答题成功'}")
        return result_msg or "答题成功"
    raise RuntimeError(f"答题失败：{result}")
