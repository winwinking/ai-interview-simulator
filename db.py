# -*- coding: utf-8 -*-
"""MySQL 持久化层：连接配置 + SQLAlchemy ORM 模型 + 建库建表。

连接信息只从项目根目录的 .env 读，绝不硬编码。
.env 需要：DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD
"""
import os

from dotenv import load_dotenv
from sqlalchemy import (create_engine, text, Column, Integer, String, Text, Date,
                        DateTime, Boolean, UniqueConstraint)
from sqlalchemy.engine.url import URL
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306") or 3306)
DB_NAME = os.getenv("DB_NAME", "interview_db")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")


def _url(with_db=True):
    return URL.create(
        "mysql+pymysql", username=DB_USER, password=DB_PASSWORD,
        host=DB_HOST, port=DB_PORT,
        database=DB_NAME if with_db else None,
        query={"charset": "utf8mb4"},
    )


engine = create_engine(_url(), pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


class WrongAnswer(Base):
    __tablename__ = "wrong_answers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    question = Column(Text, nullable=False)
    user_answer = Column(Text, default="")
    correct_answer = Column(Text, default="")
    scores_accuracy = Column(Integer, default=0)
    scores_completeness = Column(Integer, default=0)
    scores_clarity = Column(Integer, default=0)
    weak_points = Column(Text, default="")
    weak_tag = Column(String(100), default="")
    module = Column(String(100), default="", index=True)
    error_count = Column(Integer, default=1)
    followup_rounds = Column(Integer, default=0)
    first_wrong_at = Column(DateTime, nullable=True)
    timestamp = Column(DateTime, nullable=True)
    history = Column(Text, default="[]")          # JSON 数组字符串 [{at, event}]


class MasteredAnswer(Base):
    """已掌握 = 错题的全部字段 + mastered_at + original_error_count，
    这样 /unmaster 能把行无损搬回错题本。"""
    __tablename__ = "mastered_answers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    question = Column(Text, nullable=False)
    user_answer = Column(Text, default="")
    correct_answer = Column(Text, default="")
    scores_accuracy = Column(Integer, default=0)
    scores_completeness = Column(Integer, default=0)
    scores_clarity = Column(Integer, default=0)
    weak_points = Column(Text, default="")
    weak_tag = Column(String(100), default="")
    module = Column(String(100), default="", index=True)
    error_count = Column(Integer, default=1)
    followup_rounds = Column(Integer, default=0)
    first_wrong_at = Column(DateTime, nullable=True)
    timestamp = Column(DateTime, nullable=True)
    history = Column(Text, default="[]")
    mastered_at = Column(DateTime, nullable=True)
    original_error_count = Column(Integer, default=1)


class StudyTime(Base):
    __tablename__ = "study_time"
    id = Column(Integer, primary_key=True, autoincrement=True)
    module = Column(String(100), default="")
    duration_seconds = Column(Integer, default=0)
    date = Column(Date)
    __table_args__ = (UniqueConstraint("date", "module", name="uq_studytime_date_module"),)


class DailyLog(Base):
    __tablename__ = "daily_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True)
    summary = Column(Text, default="")
    encouragement = Column(Text, default="")
    modules_studied = Column(Text, default="")       # 逗号分隔
    study_minutes = Column(Integer, default=0)
    total_questions = Column(Integer, default=0)     # = 答题数 answered
    q_correct = Column(Integer, default=0)
    q_wrong = Column(Integer, default=0)
    q_wrong_added = Column(Integer, default=0)
    q_mastered = Column(Integer, default=0)
    shown = Column(Boolean, default=False)


class Note(Base):
    __tablename__ = "notes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(20), default="interview")
    major = Column(String(50), default="")
    minor = Column(String(100), default="")
    content = Column(Text, default="")
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class Activity(Base):
    """对应 activity.json：日期 -> 模块 -> 各类计数。"""
    __tablename__ = "activity"
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date)
    module = Column(String(100), default="未指定")
    answered = Column(Integer, default=0)
    correct = Column(Integer, default=0)
    wrong_added = Column(Integer, default=0)
    wrong_reviewed = Column(Integer, default=0)
    mastered = Column(Integer, default=0)
    __table_args__ = (UniqueConstraint("date", "module", name="uq_activity_date_module"),)


ACTIVITY_FIELDS = ("answered", "correct", "wrong_added", "wrong_reviewed", "mastered")


def init_db():
    """建库（不存在则创建）+ 建表（不存在则创建，存在跳过）。"""
    boot = create_engine(_url(with_db=False), pool_pre_ping=True)
    with boot.connect() as conn:
        conn.execute(text(
            "CREATE DATABASE IF NOT EXISTS `{}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci".format(DB_NAME)))
        conn.commit()
    boot.dispose()
    Base.metadata.create_all(engine)
