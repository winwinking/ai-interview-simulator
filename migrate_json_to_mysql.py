# -*- coding: utf-8 -*-
"""一次性迁移：把旧版本（JSON 文件存储）留下的数据灌进 MySQL。只有从那个版本升级才需要跑这个脚本。

用法：
    python migrate_json_to_mysql.py            # 任一目标表已有数据就中止
    python migrate_json_to_mysql.py --force    # 先清空 6 张表再导入

跑之前先在 .env 里配好 LEGACY_JSON_DIR（旧版本那 6 个 JSON 文件所在目录）。
跑完打印每张表插入了多少条。JSON 文件不会被删除，留着当备份。
"""
import json
import os
import sys
from datetime import datetime, date

from db import (SessionLocal, init_db, WrongAnswer, MasteredAnswer, StudyTime,
                DailyLog, Note, Activity, ACTIVITY_FIELDS)

JSON_DIR = os.getenv("LEGACY_JSON_DIR", "").strip()
if not JSON_DIR:
    print("没有配置 LEGACY_JSON_DIR。这个脚本只有从旧的 JSON 文件版本升级才需要跑，"
         "先在 .env 里加一行 LEGACY_JSON_DIR=旧 JSON 文件所在目录，再重新运行。")
    sys.exit(1)
F_WRONG = os.path.join(JSON_DIR, "wrong_answers.json")
F_MASTERED = os.path.join(JSON_DIR, "mastered_answers.json")
F_STUDY = os.path.join(JSON_DIR, "study_time.json")
F_DAILY = os.path.join(JSON_DIR, "daily_log.json")
F_NOTES = os.path.join(JSON_DIR, "notes.json")
F_ACTIVITY = os.path.join(JSON_DIR, "activity.json")

ALL_TABLES = [WrongAnswer, MasteredAnswer, StudyTime, DailyLog, Note, Activity]


def _load(path, default):
    if not os.path.exists(path):
        print("  跳过（文件不存在）：", path)
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or default
    except (ValueError, OSError) as e:
        print("  读取失败 {}：{}".format(path, e))
        return default


def _dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "").replace("T", " "))
    except ValueError:
        return None


def _d(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _answer_kwargs(it):
    sc = it.get("scores", {}) or {}
    ec = it.get("error_count") or it.get("times_wrong") or 1
    hist = it.get("history", []) or []
    return dict(
        question=(it.get("question") or "").strip(),
        user_answer=it.get("user_answer", "") or "",
        correct_answer=it.get("correct_answer", "") or "",
        scores_accuracy=int(sc.get("accuracy", 0) or 0),
        scores_completeness=int(sc.get("completeness", 0) or 0),
        scores_clarity=int(sc.get("clarity", 0) or 0),
        weak_points=it.get("weak_points", "") or "",
        weak_tag=it.get("weak_tag", "") or "",
        module=it.get("module", "") or "",
        error_count=int(ec),
        followup_rounds=int(it.get("followup_rounds", 0) or 0),
        first_wrong_at=_dt(it.get("first_wrong_at")) or _dt(it.get("timestamp")),
        timestamp=_dt(it.get("timestamp")) or _dt(it.get("first_wrong_at")),
        history=json.dumps(hist, ensure_ascii=False),
    )


def main():
    force = "--force" in sys.argv
    print("建库 / 建表 …")
    init_db()

    s = SessionLocal()
    try:
        non_empty = [t.__tablename__ for t in ALL_TABLES if s.query(t).first()]
        if non_empty and not force:
            print("\n以下表已有数据：{}".format("、".join(non_empty)))
            print("加 --force 会先清空这 6 张表再导入。已中止，未改动任何数据。")
            return
        if force and non_empty:
            print("--force：清空", "、".join(non_empty))
            for t in reversed(ALL_TABLES):
                s.query(t).delete()
            s.commit()

        counts = {}

        # 1. wrong_answers
        for it in _load(F_WRONG, []):
            s.add(WrongAnswer(**_answer_kwargs(it)))
        s.flush()
        counts["wrong_answers"] = s.query(WrongAnswer).count()

        # 2. mastered_answers
        for it in _load(F_MASTERED, []):
            kw = _answer_kwargs(it)
            kw["mastered_at"] = _dt(it.get("mastered_at"))
            kw["original_error_count"] = int(
                it.get("original_error_count") or it.get("error_count")
                or it.get("times_wrong") or 1)
            s.add(MasteredAnswer(**kw))
        s.flush()
        counts["mastered_answers"] = s.query(MasteredAnswer).count()

        # 3. study_time  { 日期: { 模块: 秒 } }
        for day, mods in (_load(F_STUDY, {}) or {}).items():
            dd = _d(day)
            if dd is None:
                continue
            for module, secs in (mods or {}).items():
                s.add(StudyTime(date=dd, module=module,
                                duration_seconds=int(round(secs or 0))))
        s.flush()
        counts["study_time"] = s.query(StudyTime).count()

        # 4. daily_log  [ {date, summary, stats{...}, ...} ]
        for e in _load(F_DAILY, []):
            st = e.get("stats", {}) or {}
            dd = _d(e.get("date"))
            if dd is None:
                continue
            s.add(DailyLog(
                date=dd, summary=e.get("summary", "") or "",
                encouragement=e.get("encouragement", "") or "",
                modules_studied=",".join(e.get("modules_touched", []) or []),
                study_minutes=int(e.get("study_minutes", 0) or 0),
                total_questions=int(st.get("answered", 0) or 0),
                q_correct=int(st.get("correct", 0) or 0),
                q_wrong=int(st.get("wrong", 0) or 0),
                q_wrong_added=int(st.get("wrong_added", 0) or 0),
                q_mastered=int(st.get("mastered", 0) or 0),
                shown=bool(e.get("shown", False)),
            ))
        s.flush()
        counts["daily_log"] = s.query(DailyLog).count()

        # 5. notes  { "mode::major::minor": [ {id,text,created,updated} ] }
        for key, items in (_load(F_NOTES, {}) or {}).items():
            parts = key.split("::")
            mode = "learn" if parts[0] == "learn" else "interview"
            major = parts[1].strip() if len(parts) > 1 else ""
            minor = parts[2].strip() if len(parts) > 2 else ""
            for n in (items or []):
                s.add(Note(
                    mode=mode, major=major, minor=minor,
                    content=n.get("text", "") or "",
                    created_at=_dt(n.get("created")) or _dt(n.get("updated")),
                    updated_at=_dt(n.get("updated")) or _dt(n.get("created")),
                ))
        s.flush()
        counts["notes"] = s.query(Note).count()

        # 6. activity  { 日期: { 模块: { field: 计数 } } }
        for day, mods in (_load(F_ACTIVITY, {}) or {}).items():
            dd = _d(day)
            if dd is None:
                continue
            for module, fields in (mods or {}).items():
                row = Activity(date=dd, module=module or "未指定")
                for f in ACTIVITY_FIELDS:
                    setattr(row, f, int((fields or {}).get(f, 0) or 0))
                s.add(row)
        s.flush()
        counts["activity"] = s.query(Activity).count()

        s.commit()
        print("\n迁移完成：")
        for name, n in counts.items():
            print("  {:<18} {} 条".format(name, n))
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


if __name__ == "__main__":
    main()
