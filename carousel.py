"""Independent, durable endless quiz. Recent messages age for two calendar months."""
import calendar
import hashlib
import json
import random
import re
import secrets
import sqlite3
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from questions import PARTICIPANTS, display_name

SOURCE_CHAT_ID = -1001998359261
TITLE = '🎠 Карусель Санька'
TZ = ZoneInfo('Europe/Moscow')

def cutoff(now=None):
    now = (now or datetime.now(TZ)).astimezone(TZ)
    month = now.year * 12 + now.month - 1 - 2
    year, month = divmod(month, 12)
    month += 1
    return int(now.replace(year=year, month=month,
                           day=min(now.day, calendar.monthrange(year, month)[1])).timestamp())

def acceptable(text):
    return (60 <= len(text) <= 300 and sum(c.isalpha() for c in text) >= 20
            and not text.startswith('/')
            and not re.search(r'https?://|www\.|t\.me/|@\w+|\b\S+@\S+\.\S+|(?:\+?\d[\s()-]*){10,}', text, re.I))

class Carousel:
    def __init__(self, db):
        self.db = db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS carousel_messages (
            chat_id INTEGER, message_id INTEGER, author_id INTEGER NOT NULL,
            sent_at INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(chat_id,message_id));
        CREATE INDEX IF NOT EXISTS carousel_age ON carousel_messages(chat_id,sent_at);
        CREATE TABLE IF NOT EXISTS carousel_authors (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS carousel_imports (digest TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS carousel_seen (
            chat_id INTEGER, user_id INTEGER, message_id INTEGER,
            PRIMARY KEY(chat_id,user_id,message_id));
        CREATE TABLE IF NOT EXISTS carousel_sessions (
            chat_id INTEGER, user_id INTEGER, token TEXT NOT NULL,
            message_id INTEGER NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS carousel_stats (
            chat_id INTEGER, user_id INTEGER, name TEXT NOT NULL,
            answers INTEGER NOT NULL DEFAULT 0, correct INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id,user_id));
        ''')

    def author_names(self):
        names = dict(PARTICIPANTS)
        names.update({str(r['user_id']): r['name'] for r in self.db.execute('SELECT * FROM carousel_authors')})
        return names

    def ingest(self, chat_id, message_id, author_id, sent_at, text, allowed=True):
        if chat_id != SOURCE_CHAT_ID:
            return False
        valid = allowed and str(author_id) in self.author_names() and acceptable(text)
        with self.db:
            if not valid:
                self.db.execute('DELETE FROM carousel_messages WHERE chat_id=? AND message_id=?', (chat_id,message_id))
                return False
            self.db.execute('''INSERT INTO carousel_messages VALUES (?,?,?,?,?)
                ON CONFLICT(chat_id,message_id) DO UPDATE SET author_id=excluded.author_id,
                sent_at=excluded.sent_at,text=excluded.text''', (chat_id,message_id,author_id,sent_at,text))
        return True

    def import_seed(self, path):
        path = Path(path)
        if not path.exists():
            return 0
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if self.db.execute('SELECT 1 FROM carousel_imports WHERE digest=?',(digest,)).fetchone():
            return 0
        seed = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
        try:
            rows = seed.execute('SELECT chat_id,message_id,author_id,sent_at,text FROM carousel_messages').fetchall()
            authors = seed.execute('SELECT user_id,name FROM carousel_authors').fetchall() if seed.execute("SELECT 1 FROM sqlite_master WHERE name='carousel_authors'").fetchone() else []
        finally:
            seed.close()
        names = self.author_names()
        names.update({str(uid): name for uid,name in authors})
        rows = [r for r in rows if r[0] == SOURCE_CHAT_ID and str(r[2]) in names and acceptable(r[4])]
        with self.db:
            self.db.executemany('INSERT INTO carousel_authors VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name',authors)
            self.db.executemany('INSERT OR IGNORE INTO carousel_messages VALUES (?,?,?,?,?)',rows)
            self.db.execute('INSERT INTO carousel_imports VALUES (?)',(digest,))
        return len(rows)

    def session(self, chat_id, user_id):
        return self.db.execute('SELECT * FROM carousel_sessions WHERE chat_id=? AND user_id=?',(chat_id,user_id)).fetchone()

    def current_valid(self, row, now=None):
        src = self.db.execute('SELECT * FROM carousel_messages WHERE chat_id=? AND message_id=?',(row['chat_id'],row['message_id'])).fetchone()
        q = json.loads(row['payload'])
        return bool(src and src['sent_at'] < cutoff(now) and src['text']==q['text'] and src['author_id']==q['author_id'])

    def draw(self, chat_id, user_id, name, now=None):
        if chat_id != SOURCE_CHAT_ID:
            return None
        current = self.session(chat_id,user_id)
        if current and current['state'] != 'invalid' and self.current_valid(current,now):
            return current
        with self.db:
            self.db.execute('DELETE FROM carousel_sessions WHERE chat_id=? AND user_id=?',(chat_id,user_id))
            rows = self.db.execute('''SELECT m.* FROM carousel_messages m WHERE chat_id=? AND sent_at<?
              AND NOT EXISTS (SELECT 1 FROM carousel_seen s WHERE s.chat_id=m.chat_id AND s.user_id=? AND s.message_id=m.message_id)
              AND m.text NOT IN (SELECT old.text FROM carousel_seen s JOIN carousel_messages old
                ON old.chat_id=s.chat_id AND old.message_id=s.message_id
                WHERE s.chat_id=? AND s.user_id=?)''',
                (chat_id,cutoff(now),user_id,chat_id,user_id)).fetchall()
            if not rows:
                return None
            # Uniform author first, then message: prolific writers do not dominate.
            authors = list({r['author_id'] for r in rows})
            author = random.choice(authors)
            src = random.choice([r for r in rows if r['author_id']==author])
            names = self.author_names()
            correct = names[str(author)]
            choices = random.sample([n for n in set(names.values()) if n!=correct],3)+[correct]
            random.shuffle(choices)
            q = dict(text=src['text'],author_id=author,author=correct,options=choices,answer=str(choices.index(correct)))
            self.db.execute('INSERT INTO carousel_seen VALUES (?,?,?)',(chat_id,user_id,src['message_id']))
            self.db.execute('INSERT INTO carousel_sessions VALUES (?,?,?,?,?,?)',
                (chat_id,user_id,secrets.token_urlsafe(9),src['message_id'],json.dumps(q,ensure_ascii=False),'open'))
            self.db.execute('''INSERT INTO carousel_stats(chat_id,user_id,name) VALUES (?,?,?)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET name=excluded.name''',(chat_id,user_id,display_name(user_id,name)))
        return self.session(chat_id,user_id)

    def act(self, chat_id, user_id, token, action, now=None):
        with self.db:
            row = self.session(chat_id,user_id)
            if not row or row['token'] != token:
                return False
            if not self.current_valid(row,now):
                self.db.execute("UPDATE carousel_sessions SET state='invalid' WHERE chat_id=? AND user_id=?",(chat_id,user_id))
                return False
            if action=='next' and row['state'] in ('correct','wrong'):
                self.db.execute('DELETE FROM carousel_sessions WHERE chat_id=? AND user_id=?',(chat_id,user_id))
                return True
            if row['state']!='open' or action not in ('0','1','2','3'):
                return False
            q = json.loads(row['payload'])
            state = 'correct' if action==q['answer'] else 'wrong'
            self.db.execute('UPDATE carousel_sessions SET state=? WHERE chat_id=? AND user_id=?',(state,chat_id,user_id))
            self.db.execute('''UPDATE carousel_stats SET answers=answers+1,correct=correct+?
                WHERE chat_id=? AND user_id=?''',(int(state=='correct'),chat_id,user_id))
            return True

    def score(self, chat_id, user_id):
        return self.db.execute('SELECT * FROM carousel_stats WHERE chat_id=? AND user_id=?',(chat_id,user_id)).fetchone()

    def ranking(self, chat_id):
        rows = self.db.execute('SELECT * FROM carousel_stats WHERE chat_id=? AND answers>=20',(chat_id,)).fetchall()
        return sorted(rows,key=lambda r:(-Fraction(r['correct'],r['answers']),-r['answers'],r['user_id']))

    def stat_text(self, chat_id, user_id):
        row = self.score(chat_id,user_id)
        n,c = (row['answers'],row['correct']) if row else (0,0)
        text = f'Ответов: {n} · верных: {c}'
        return text + (f'\nТочность: {100*c/n:.1f}%' if n>=20 else f'\nЕщё {20-n} ответов до рейтинга.')

    def table(self, chat_id):
        rows = self.ranking(chat_id)
        text = TITLE+'\nРейтинг по точности (от 20 ответов)\n\n'
        text += '\n'.join(f'{i}. {escape(display_name(r["user_id"],r["name"])[:40])} — {100*r["correct"]/r["answers"]:.1f}% · {r["correct"]}/{r["answers"]}' for i,r in enumerate(rows[:20],1)) or 'Пока никто не ответил на 20 вопросов.'
        return text

class CarouselUI:
    def __init__(self, host):
        self.host = host
        self.store = Carousel(host.store.db)

    async def callback(self, query):
        if not query.message or query.message.chat.type!='private' or query.message.chat.id!=query.from_user.id:
            await query.answer('Игра доступна в личке.',show_alert=True)
            return
        parts = (query.data or '').split(':')
        if len(parts)!=3:
            await query.answer('Некорректная кнопка.'); return
        _,token,action = parts
        group = self.host.store.db.execute('SELECT * FROM quiz_groups WHERE chat_id=?',(SOURCE_CHAT_ID,)).fetchone()
        if not group:
            await query.answer('Сначала вызовите /quiz в группе.');return
        async with self.host.locks[query.from_user.id]:
            if action=='open' and token==group['token']:
                pass
            elif action=='top' and token==group['token']:
                await query.answer()
                await self.display(query.message,self.store.table(SOURCE_CHAT_ID),Markup(inline_keyboard=[
                    [Button(text='🎠 Продолжить',callback_data=f'c:{group["token"]}:open')],self.host.menu_button(SOURCE_CHAT_ID)]))
                return
            elif action not in ('0','1','2','3','next'):
                await query.answer('Кнопка недоступна.');return
            else:
                if not self.store.act(SOURCE_CHAT_ID,query.from_user.id,token,action):
                    await query.answer('Ответ уже учтён или вопрос изменён. /quiz откроет актуальный вопрос.')
                    return
            self.host.store.set_context(SOURCE_CHAT_ID,query.from_user.id)
            row = self.store.draw(SOURCE_CHAT_ID,query.from_user.id,query.from_user.full_name)
            try:
                await query.answer()
            except TelegramBadRequest:
                pass
            await self.show(query.message,query.from_user.id,row,group)

    async def display(self,message,text,markup):
        try:
            await message.edit_text(text,reply_markup=markup)
        except TelegramBadRequest as exc:
            if 'message is not modified' not in str(exc).lower():
                await message.answer(text,reply_markup=markup)

    async def show(self,message,user_id,row,group):
        text = '<b>'+TITLE+'</b>\nКто это написал?\n\n'
        buttons=[]
        if row:
            q=json.loads(row['payload']);text+=escape(q['text'])+'\n\n'
            if row['state']=='open':
                for i in range(0,4,2):
                    buttons.append([Button(text=q['options'][j],callback_data=f'c:{row["token"]}:{j}') for j in range(i,i+2)])
            else:
                text+= {'correct':'✅ Верно!','wrong':'Не угадал.'}[row['state']]+f' Автор: {escape(q["author"])}\n\n'
                buttons.append([Button(text='Ещё одну! →',callback_data=f'c:{row["token"]}:next')])
        else:
            text+='Все доступные сообщения архива уже просмотрены. Более свежие сообщения из архива откроются, когда им исполнится два месяца. Сбор новой переписки отключён.\n\n'
            buttons.append([Button(text='Проверить новые',callback_data=f'c:{group["token"]}:open')])
        text+=self.store.stat_text(SOURCE_CHAT_ID,user_id)
        buttons.extend([[Button(text='🏆 Рейтинг карусели',callback_data=f'c:{group["token"]}:top')],self.host.menu_button(SOURCE_CHAT_ID)])
        await self.display(message,text,Markup(inline_keyboard=buttons))
