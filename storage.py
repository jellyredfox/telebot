"""Persistent private attempts. v2 tables leave old group-game history untouched."""
import json
import random
import secrets
import sqlite3
from pathlib import Path

from questions import QUIZ_ID, GAMES_BY_ID, display_name

class Storage:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS quiz_groups (
                chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, token TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS pearl_attempts (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL REFERENCES quiz_groups(chat_id),
                user_id INTEGER NOT NULL, name TEXT NOT NULL, quiz_id TEXT NOT NULL,
                questions TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
                finished INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chat_id, user_id, quiz_id));
            CREATE TABLE IF NOT EXISTS pearl_answers (
                attempt_id INTEGER NOT NULL REFERENCES pearl_attempts(id),
                position INTEGER NOT NULL, choice TEXT NOT NULL, correct INTEGER NOT NULL,
                PRIMARY KEY(attempt_id, position));
            CREATE TABLE IF NOT EXISTS group_output (
                chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS private_context (
                user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES quiz_groups(chat_id));
        ''')

    def group(self, chat_id, title):
        with self.db:
            self.db.execute('INSERT INTO quiz_groups VALUES (?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title',
                            (chat_id, title, secrets.token_urlsafe(18)))
        return self.db.execute('SELECT * FROM quiz_groups WHERE chat_id=?', (chat_id,)).fetchone()

    def by_token(self, token):
        return self.db.execute('SELECT * FROM quiz_groups WHERE token=?', (token,)).fetchone()

    def context(self, user_id):
        return self.db.execute('SELECT g.* FROM quiz_groups g JOIN private_context c USING(chat_id) WHERE c.user_id=?', (user_id,)).fetchone()

    def set_context(self, chat_id, user_id):
        with self.db:
            self.db.execute('INSERT INTO private_context VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id', (user_id, chat_id))

    def start(self, chat_id, user_id, name, quiz_id=QUIZ_ID):
        source = GAMES_BY_ID[quiz_id]['questions']
        snapshot = []
        for original in random.sample(source, len(source)):
            q = dict(original)
            correct = q['options'][int(q['answer'])]
            q['options'] = random.sample(q['options'], len(q['options']))
            q['answer'] = str(q['options'].index(correct))
            snapshot.append(q)
        questions = json.dumps(snapshot, ensure_ascii=False)
        name = display_name(user_id, name)
        with self.db:
            self.db.execute('INSERT INTO pearl_attempts(chat_id,user_id,name,questions,quiz_id) VALUES (?,?,?,?,?) ON CONFLICT(chat_id,user_id,quiz_id) DO UPDATE SET name=excluded.name',
                            (chat_id, user_id, name, questions, quiz_id))
            self.db.execute('INSERT INTO private_context VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id', (user_id, chat_id))
        return self.attempt(chat_id, user_id, quiz_id)

    def attempt(self, chat_id, user_id, quiz_id=QUIZ_ID):
        return self.db.execute('SELECT * FROM pearl_attempts WHERE chat_id=? AND user_id=? AND quiz_id=?', (chat_id, user_id, quiz_id)).fetchone()

    def get(self, attempt_id, user_id):
        row = self.db.execute('SELECT * FROM pearl_attempts WHERE id=? AND user_id=?', (attempt_id, user_id)).fetchone()
        return row if row and row['quiz_id'] in GAMES_BY_ID else None

    def answer_at(self, attempt_id, position):
        return self.db.execute('SELECT * FROM pearl_answers WHERE attempt_id=? AND position=?', (attempt_id, position)).fetchone()

    def answer(self, attempt_id, user_id, position, choice):
        with self.db:
            row = self.get(attempt_id, user_id)
            if not row or row['finished'] or row['position'] != position or choice not in ('0', '1', '2', '3'):
                return False
            questions = json.loads(row['questions'])
            correct = choice == questions[position]['answer']
            inserted = self.db.execute('INSERT OR IGNORE INTO pearl_answers VALUES (?,?,?,?)',
                                      (attempt_id, position, choice, int(correct))).rowcount
            if inserted and position == len(questions) - 1:
                self.db.execute('UPDATE pearl_attempts SET finished=1 WHERE id=?', (attempt_id,))
            return bool(inserted)

    def advance(self, attempt_id, user_id, position):
        with self.db:
            row = self.get(attempt_id, user_id)
            if not row or row['finished'] or row['position'] != position or not self.answer_at(attempt_id, position):
                return False
            self.db.execute('UPDATE pearl_attempts SET position=position+1 WHERE id=?', (attempt_id,))
            return True

    def score(self, attempt_id):
        return self.db.execute('SELECT COALESCE(SUM(correct),0) AS points, COUNT(*) AS answers FROM pearl_answers WHERE attempt_id=?', (attempt_id,)).fetchone()

    def ranking(self, chat_id, quiz_id=QUIZ_ID):
        return self.db.execute('''SELECT p.user_id, p.name, SUM(a.correct) AS points
            FROM pearl_attempts p JOIN pearl_answers a ON a.attempt_id=p.id
            WHERE p.chat_id=? AND p.quiz_id=? AND p.finished=1 GROUP BY p.id
            ORDER BY points DESC, p.user_id ASC''', (chat_id, quiz_id)).fetchall()

    def previous_output(self, chat_id):
        row = self.db.execute('SELECT message_id FROM group_output WHERE chat_id=?', (chat_id,)).fetchone()
        return row[0] if row else None

    def remember_output(self, chat_id, message_id):
        with self.db:
            self.db.execute('INSERT INTO group_output VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id', (chat_id, message_id))

    def close(self):
        self.db.close()
