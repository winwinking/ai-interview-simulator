# -*- coding: utf-8 -*-
"""数据访问层：把 MySQL 的读写包成和原来 JSON 版一样的数据形状，
app.py / 模板几乎不用改。

设计原则：
- 读接口返回的 dict / 结构跟原来的 *.json 完全一致（scores 嵌套、history 是数组、
  times_wrong 仍在、activity/study 仍是 {日期: {模块: ...}}）。
- 写接口内部用事务；错题去重按 question 文本匹配（和原逻辑一致）。
"""
import json
from contextlib import contextmanager
from datetime import datetime, date

from sqlalchemy import func, or_

from db import (SessionLocal, WrongAnswer, MasteredAnswer, StudyTime, DailyLog,
                Note, Activity, ACTIVITY_FIELDS)

_ANSWER_COPY_FIELDS = (
    "question", "user_answer", "correct_answer", "scores_accuracy",
    "scores_completeness", "scores_clarity", "weak_points", "weak_tag",
    "module", "error_count", "followup_rounds", "first_wrong_at",
    "timestamp", "history",
)


@contextmanager
def _sess():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ---------- 时间 ----------
def _parse_dt(v):
    if not v or isinstance(v, datetime):
        return v or None
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "").replace("T", " "))
    except ValueError:
        return None


def _fmt_dt(v):
    if not v:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat(timespec="seconds") if isinstance(v, datetime) else v.isoformat()
    return str(v)


def _parse_date(v):
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


# ---------- 错题 / 已掌握：行 <-> 旧 JSON 条目 ----------
def _row_to_answer_dict(r):
    try:
        hist = json.loads(r.history) if r.history else []
    except (ValueError, TypeError):
        hist = []
    acc, comp, clar = (r.scores_accuracy or 0, r.scores_completeness or 0,
                       r.scores_clarity or 0)
    d = {
        "question": r.question,
        "user_answer": r.user_answer or "",
        "correct_answer": r.correct_answer or "",
        "weak_points": r.weak_points or "",
        "weak_tag": r.weak_tag or "",
        "module": r.module or "",
        "followup_rounds": r.followup_rounds or 0,
        "error_count": r.error_count or 1,
        "times_wrong": r.error_count or 1,
        "first_wrong_at": _fmt_dt(r.first_wrong_at),
        "timestamp": _fmt_dt(r.timestamp),
        "history": hist,
    }
    if acc or comp or clar:   # 旧的「学习模式记的错题」没有评分，保持不显示评分行
        d["scores"] = {"accuracy": acc, "completeness": comp, "clarity": clar}
    if isinstance(r, MasteredAnswer):
        d["mastered_at"] = _fmt_dt(r.mastered_at)
        d["original_error_count"] = r.original_error_count or (r.error_count or 1)
    return d


def wrong_list():
    with _sess() as s:
        rows = s.query(WrongAnswer).order_by(WrongAnswer.id).all()
        return [_row_to_answer_dict(r) for r in rows]


def mastered_list():
    with _sess() as s:
        rows = s.query(MasteredAnswer).order_by(MasteredAnswer.id).all()
        return [_row_to_answer_dict(r) for r in rows]


def wrong_count_by_module():
    with _sess() as s:
        q = (s.query(WrongAnswer.module, func.count(WrongAnswer.id))
             .group_by(WrongAnswer.module).all())
        return {m or "": n for m, n in q}


def mastered_count_by_module():
    with _sess() as s:
        q = (s.query(MasteredAnswer.module, func.count(MasteredAnswer.id))
             .group_by(MasteredAnswer.module).all())
        return {m or "": n for m, n in q}


def record_wrong(question, user_answer, correct_answer, weak_points, module,
                 weak_tag="", scores=None, followup_rounds=0):
    """按 question 去重：已存在 -> error_count+1 并更新；不存在 -> 新建。
    返回 (is_new: bool, error_count: int, total: int)。"""
    scores = scores or {}
    acc = int(scores.get("accuracy", 0) or 0)
    comp = int(scores.get("completeness", 0) or 0)
    clar = int(scores.get("clarity", 0) or 0)
    key = (question or "").strip()
    now = datetime.now().replace(microsecond=0)
    with _sess() as s:
        row = s.query(WrongAnswer).filter(WrongAnswer.question == key).first()
        if row:
            row.error_count = (row.error_count or 1) + 1
            row.user_answer = user_answer
            row.weak_points = weak_points or row.weak_points
            if acc or comp or clar:
                row.scores_accuracy, row.scores_completeness, row.scores_clarity = acc, comp, clar
            row.followup_rounds = followup_rounds
            row.timestamp = now
            if correct_answer:
                row.correct_answer = correct_answer
            if module:
                row.module = module
            if not row.weak_tag:
                row.weak_tag = weak_tag or ""
            try:
                hist = json.loads(row.history) if row.history else []
            except (ValueError, TypeError):
                hist = []
            hist.append({"at": now.isoformat(timespec="seconds"),
                         "event": "第{}次答错".format(row.error_count)})
            row.history = json.dumps(hist, ensure_ascii=False)
            ec = row.error_count
            is_new = False
        else:
            row = WrongAnswer(
                question=key, user_answer=user_answer,
                correct_answer=correct_answer, weak_points=weak_points,
                weak_tag=weak_tag or "", module=module or "",
                scores_accuracy=acc, scores_completeness=comp, scores_clarity=clar,
                error_count=1, followup_rounds=followup_rounds,
                first_wrong_at=now, timestamp=now,
                history=json.dumps([{"at": now.isoformat(timespec="seconds"),
                                     "event": "首次答错"}], ensure_ascii=False),
            )
            s.add(row)
            ec = 1
            is_new = True
        s.flush()
        total = s.query(func.count(WrongAnswer.id)).scalar()
    return is_new, ec, total


def _copy_answer(src, dst_cls, **extra):
    data = {f: getattr(src, f) for f in _ANSWER_COPY_FIELDS}
    data.update(extra)
    return dst_cls(**data)


def move_to_mastered(question):
    """错题 -> 已掌握（事务）。返回该题的 module（找不到返回 None）。"""
    key = (question or "").strip()
    now = datetime.now().replace(microsecond=0)
    with _sess() as s:
        row = s.query(WrongAnswer).filter(WrongAnswer.question == key).first()
        if row is None:
            return None
        module = row.module or ""
        try:
            hist = json.loads(row.history) if row.history else []
        except (ValueError, TypeError):
            hist = []
        hist.append({"at": now.isoformat(timespec="seconds"), "event": "标记为已掌握"})
        exists = (s.query(MasteredAnswer)
                  .filter(MasteredAnswer.question == key).first())
        if exists is None:
            m = _copy_answer(row, MasteredAnswer,
                             history=json.dumps(hist, ensure_ascii=False),
                             mastered_at=now,
                             original_error_count=row.error_count or 1)
            s.add(m)
        s.delete(row)
        return module


def move_to_wrong(question):
    """已掌握 -> 错题（事务，/unmaster）。返回 bool。"""
    key = (question or "").strip()
    now = datetime.now().replace(microsecond=0)
    with _sess() as s:
        row = s.query(MasteredAnswer).filter(MasteredAnswer.question == key).first()
        if row is None:
            return False
        try:
            hist = json.loads(row.history) if row.history else []
        except (ValueError, TypeError):
            hist = []
        hist.append({"at": now.isoformat(timespec="seconds"), "event": "重新放回错题本"})
        exists = s.query(WrongAnswer).filter(WrongAnswer.question == key).first()
        if exists is None:
            w = _copy_answer(row, WrongAnswer,
                             history=json.dumps(hist, ensure_ascii=False))
            s.add(w)
        s.delete(row)
        return True


# ---------- activity ----------
def bump_activity(module, field, n=1, day=None):
    if field not in ACTIVITY_FIELDS:
        return
    module = module or "未指定"
    d = _parse_date(day) or date.today()
    with _sess() as s:
        row = (s.query(Activity)
               .filter(Activity.date == d, Activity.module == module).first())
        if row is None:
            row = Activity(date=d, module=module)
            s.add(row)
        setattr(row, field, (getattr(row, field) or 0) + n)


def activity_map():
    """{ '2026-09-03': { 'RAG技术': {'answered': 6, 'wrong_added': 6}, ... } }"""
    out = {}
    with _sess() as s:
        for r in s.query(Activity).all():
            day = out.setdefault(r.date.isoformat(), {})
            counts = {f: getattr(r, f) or 0 for f in ACTIVITY_FIELDS
                      if (getattr(r, f) or 0)}
            day[r.module] = counts
    return out


# ---------- study_time ----------
def add_study_time(module, seconds, day=None):
    if not module or seconds <= 0:
        return
    d = _parse_date(day) or date.today()
    inc = round(seconds)
    with _sess() as s:
        row = (s.query(StudyTime)
               .filter(StudyTime.date == d, StudyTime.module == module).first())
        if row is None:
            row = StudyTime(date=d, module=module, duration_seconds=0)
            s.add(row)
        row.duration_seconds = (row.duration_seconds or 0) + inc


def study_map():
    """{ '2026-09-03': { 'RAG技术': 4201, ... } }"""
    out = {}
    with _sess() as s:
        for r in s.query(StudyTime).all():
            out.setdefault(r.date.isoformat(), {})[r.module] = r.duration_seconds or 0
    return out


# ---------- daily_log ----------
def daily_log_dates():
    with _sess() as s:
        return {d.isoformat() for (d,) in s.query(DailyLog.date).all() if d}


def add_daily_log(entry):
    """entry 用旧 JSON 形状：{date, summary, study_minutes, modules_touched(list),
    stats{answered,correct,wrong,wrong_added,mastered}, encouragement, shown}"""
    st = entry.get("stats", {}) or {}
    d = _parse_date(entry.get("date"))
    with _sess() as s:
        if s.query(DailyLog).filter(DailyLog.date == d).first():
            return
        s.add(DailyLog(
            date=d, summary=entry.get("summary", ""),
            encouragement=entry.get("encouragement", ""),
            modules_studied=",".join(entry.get("modules_touched", []) or []),
            study_minutes=entry.get("study_minutes", 0) or 0,
            total_questions=st.get("answered", 0) or 0,
            q_correct=st.get("correct", 0) or 0,
            q_wrong=st.get("wrong", 0) or 0,
            q_wrong_added=st.get("wrong_added", 0) or 0,
            q_mastered=st.get("mastered", 0) or 0,
            shown=bool(entry.get("shown", False)),
        ))


def _daily_to_dict(r):
    return {
        "date": r.date.isoformat() if r.date else "",
        "summary": r.summary or "",
        "encouragement": r.encouragement or "",
        "study_minutes": r.study_minutes or 0,
        "modules_touched": [m for m in (r.modules_studied or "").split(",") if m],
        "stats": {"answered": r.total_questions or 0, "correct": r.q_correct or 0,
                  "wrong": r.q_wrong or 0, "wrong_added": r.q_wrong_added or 0,
                  "mastered": r.q_mastered or 0},
        "shown": bool(r.shown),
    }


def pending_daily():
    with _sess() as s:
        rows = (s.query(DailyLog)
                .filter(or_(DailyLog.shown == False, DailyLog.shown.is_(None)))  # noqa: E712
                .order_by(DailyLog.date).all())
        return [_daily_to_dict(r) for r in rows]


def mark_daily_seen(dates):
    ds = {_parse_date(x) for x in (dates or [])}
    ds.discard(None)
    if not ds:
        return
    with _sess() as s:
        for r in s.query(DailyLog).filter(DailyLog.date.in_(ds)).all():
            r.shown = True


# ---------- notes ----------
def _note_to_dict(r):
    return {"id": str(r.id), "text": r.content or "",
            "created": _fmt_dt(r.created_at), "updated": _fmt_dt(r.updated_at)}


def _norm_mode(mode):
    return "learn" if (mode or "") == "learn" else "interview"


def notes_all():
    """{ 'learn::面试八股文::RAG技术': [ {id,text,created,updated}, ... ] }"""
    out = {}
    with _sess() as s:
        for r in s.query(Note).order_by(Note.id).all():
            key = "{}::{}::{}".format(r.mode, r.major or "", r.minor or "")
            out.setdefault(key, []).append(_note_to_dict(r))
    return out


def notes_for(mode, major, minor):
    mode = _norm_mode(mode)
    with _sess() as s:
        rows = (s.query(Note)
                .filter(Note.mode == mode, Note.major == (major or "").strip(),
                        Note.minor == (minor or "").strip())
                .order_by(Note.id).all())
        return [_note_to_dict(r) for r in rows]


def note_save(mode, major, minor, text, nid=None):
    mode = _norm_mode(mode)
    now = datetime.now().replace(microsecond=0)
    with _sess() as s:
        row = None
        if nid and str(nid).isdigit():
            row = s.get(Note, int(nid))
        if row is None:
            row = Note(mode=mode, major=(major or "").strip(),
                       minor=(minor or "").strip(), created_at=now)
            s.add(row)
        row.content = text or ""
        row.updated_at = now
        s.flush()
        return str(row.id), now.isoformat(timespec="seconds")


def note_delete(nid):
    if not (nid and str(nid).isdigit()):
        return
    with _sess() as s:
        row = s.get(Note, int(nid))
        if row is not None:
            s.delete(row)
