import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from bot import Quiz
from carousel import Carousel, SOURCE_CHAT_ID, cutoff, acceptable
from storage import Storage
from questions import PARTICIPANTS, AUTHOR_QUIZ_ID

NOW=datetime(2026,10,15,12,tzinfo=ZoneInfo('Europe/Moscow'))
OLD=int(datetime(2026,7,1,tzinfo=ZoneInfo('Europe/Moscow')).timestamp())
AUTHORS=list(map(int,PARTICIPANTS))
TEXT='Это тестовое сообщение достаточно длинное, чтобы попасть в карусель без ссылок и пересылок.'

class CarouselTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'quiz.db'
        self.storage=Storage(self.path)
        self.quiz=Quiz(AsyncMock(),self.storage,'test_bot')
        self.c=self.quiz.carousel.store
        self.group=self.storage.group(SOURCE_CHAT_ID,'Friends')
        self.quiz.bot.get_chat_member.return_value=NS(status='member')
        self.patcher=patch('carousel.cutoff',return_value=cutoff(NOW));self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop();self.storage.close();self.tmp.cleanup()

    def seed(self,count=25):
        for i in range(count):self.c.ingest(SOURCE_CHAT_ID,i,AUTHORS[i%len(AUTHORS)],OLD,TEXT+f' Вариант {i}.')

    def query(self,token=None,action='open',user=7):
        return NS(data=f'c:{token or self.group["token"]}:{action}',from_user=NS(id=user,full_name='Player'),answer=AsyncMock(),
            message=NS(chat=NS(id=user,type='private'),edit_text=AsyncMock(),answer=AsyncMock()))

    def finish(self,user=7,right=True):
        row=self.c.draw(SOURCE_CHAT_ID,user,'Player',NOW)
        correct=json.loads(row['payload'])['answer']
        choice=correct if right else str((int(correct)+1)%4)
        self.assertTrue(self.c.act(SOURCE_CHAT_ID,user,row['token'],choice,NOW))
        self.assertTrue(self.c.act(SOURCE_CHAT_ID,user,row['token'],'next',NOW))
        return row

    async def test_calendar_cutoff_and_strict_age(self):
        self.patcher.stop()
        self.assertEqual(datetime.fromtimestamp(cutoff(NOW),NOW.tzinfo),datetime(2026,8,15,12,tzinfo=NOW.tzinfo))
        boundary=datetime(2026,4,30,12,tzinfo=NOW.tzinfo)
        self.assertEqual(datetime.fromtimestamp(cutoff(boundary),NOW.tzinfo),datetime(2026,2,28,12,tzinfo=NOW.tzinfo))
        self.c.ingest(SOURCE_CHAT_ID,1,AUTHORS[0],cutoff(NOW),TEXT)
        self.assertIsNone(self.c.draw(SOURCE_CHAT_ID,7,'Player',NOW))
        self.assertIsNotNone(self.c.draw(SOURCE_CHAT_ID,7,'Player',NOW.replace(second=1)))
        self.patcher.start()

    async def test_filters_and_group_scope(self):
        self.assertTrue(acceptable('а'*60));self.assertTrue(acceptable('а'*300))
        for text in ['а'*59,'а'*301,'😀'*90,TEXT+' https://example.com',TEXT+' @someone',TEXT+' 89123456789']:
            self.assertFalse(acceptable(text))
        self.assertFalse(self.c.ingest(-42,1,AUTHORS[0],OLD,TEXT))
        self.assertFalse(self.c.ingest(SOURCE_CHAT_ID,1,999,OLD,TEXT))
        self.assertFalse(self.c.ingest(SOURCE_CHAT_ID,1,AUTHORS[0],OLD,TEXT,False))

    async def test_no_skip_no_next_before_answer_and_resume(self):
        self.seed();a=self.c.draw(SOURCE_CHAT_ID,7,'Player',NOW)
        self.assertFalse(self.c.act(SOURCE_CHAT_ID,7,a['token'],'skip',NOW))
        self.assertFalse(self.c.act(SOURCE_CHAT_ID,7,a['token'],'next',NOW))
        self.assertFalse(self.c.act(SOURCE_CHAT_ID,8,a['token'],'0',NOW))
        self.storage.close();self.storage=Storage(self.path);self.c=Carousel(self.storage.db)
        resumed=self.c.draw(SOURCE_CHAT_ID,7,'Player',NOW)
        self.assertEqual(dict(a),dict(resumed))
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,7)['answers'],0)

    async def test_threshold_twenty_wrong_answers_and_ranking(self):
        self.seed(50)
        for i in range(19):self.finish(right=i<15)
        self.assertEqual(self.c.ranking(SOURCE_CHAT_ID),[])
        self.assertNotIn('%',self.c.stat_text(SOURCE_CHAT_ID,7))
        self.finish(right=False)
        self.assertIn('75.0%',self.c.stat_text(SOURCE_CHAT_ID,7))
        for i in range(24):self.finish(user=8,right=i<18)
        self.assertEqual([r['user_id'] for r in self.c.ranking(SOURCE_CHAT_ID)],[8,7])
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,7)['answers'],20)
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,7)['correct'],15)
        self.assertEqual(self.storage.ranking(SOURCE_CHAT_ID,AUTHOR_QUIZ_ID),[])

    async def test_duplicates_old_buttons_and_permanent_no_repeats(self):
        self.seed(2)
        first=self.c.draw(SOURCE_CHAT_ID,7,'P',NOW)
        choice=json.loads(first['payload'])['answer']
        self.assertTrue(self.c.act(SOURCE_CHAT_ID,7,first['token'],choice,NOW))
        self.assertFalse(self.c.act(SOURCE_CHAT_ID,7,first['token'],choice,NOW))
        self.assertTrue(self.c.act(SOURCE_CHAT_ID,7,first['token'],'next',NOW))
        second=self.c.draw(SOURCE_CHAT_ID,7,'P',NOW)
        self.assertNotEqual(first['message_id'],second['message_id'])
        self.assertFalse(self.c.act(SOURCE_CHAT_ID,7,first['token'],'next',NOW))
        self.finish()
        self.assertIsNone(self.c.draw(SOURCE_CHAT_ID,7,'P',NOW))
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,7)['answers'],2)

    async def test_edits_invalidate_question_without_penalty(self):
        self.seed(1);row=self.c.draw(SOURCE_CHAT_ID,7,'P',NOW)
        self.c.ingest(SOURCE_CHAT_ID,0,AUTHORS[0],OLD,'short')
        self.assertFalse(self.c.act(SOURCE_CHAT_ID,7,row['token'],'0',NOW))
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,7)['answers'],0)
        self.assertIsNone(self.c.draw(SOURCE_CHAT_ID,7,'P',NOW))

    async def test_seed_import_idempotent_and_live_edits_not_overwritten(self):
        seed=Path(self.tmp.name)/'seed.db';db=sqlite3.connect(seed)
        db.execute('CREATE TABLE carousel_messages(chat_id,message_id,author_id,sent_at,text)')
        db.execute('INSERT INTO carousel_messages VALUES (?,?,?,?,?)',(SOURCE_CHAT_ID,1,AUTHORS[0],OLD,TEXT));db.commit();db.close()
        self.assertEqual(self.c.import_seed(seed),1)
        self.c.ingest(SOURCE_CHAT_ID,1,AUTHORS[0],OLD,'short')
        self.assertEqual(self.c.import_seed(seed),0)
        self.assertIsNone(self.c.draw(SOURCE_CHAT_ID,7,'P',NOW))

    async def test_concurrent_callbacks_and_any_invited_player(self):
        self.seed()
        await self.quiz.carousel.callback(self.query())
        row=self.c.session(SOURCE_CHAT_ID,7);choice=json.loads(row['payload'])['answer']
        await asyncio.gather(*(self.quiz.carousel.callback(self.query(row['token'],choice)) for _ in range(2)))
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,7)['answers'],1)
        self.quiz.bot.get_chat_member.return_value=NS(status='left')
        q=self.query(user=8);await self.quiz.carousel.callback(q)
        self.assertIsNotNone(self.c.session(SOURCE_CHAT_ID,8))
        q.message.edit_text.assert_awaited_once()
        self.quiz.bot.get_chat_member.assert_not_called()
        self.quiz.bot.send_message.assert_not_called()

    async def test_open_screen_no_spoiler_or_skip_and_same_question(self):
        self.seed();q=self.query();await self.quiz.carousel.callback(q)
        row=self.c.session(SOURCE_CHAT_ID,7)
        text=q.message.edit_text.call_args.args[0]
        self.assertNotIn('Автор:',text);self.assertNotIn('%',text)
        buttons=q.message.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertFalse(any('Пропустить' in b.text for line in buttons for b in line))
        await self.quiz.carousel.callback(self.query())
        self.assertEqual(self.c.session(SOURCE_CHAT_ID,7)['token'],row['token'])

    async def test_real_dispatcher_ignores_new_and_edited_messages(self):
        bot=Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        dp=Dispatcher();dp.include_router(self.quiz.router)
        data=dict(message_id=123,date=OLD,chat={'id':SOURCE_CHAT_ID,'type':'supergroup','title':'Friends'},
            from_user={'id':AUTHORS[0],'is_bot':False,'first_name':'Test'},text=TEXT)
        try:
            await dp.feed_update(bot,Update(update_id=1,message=data))
            self.assertEqual(self.storage.db.execute('SELECT COUNT(*) FROM carousel_messages').fetchone()[0],0)
            data['text']='short'
            await dp.feed_update(bot,Update(update_id=2,edited_message=data))
            self.assertEqual(self.storage.db.execute('SELECT COUNT(*) FROM carousel_messages').fetchone()[0],0)
            data['text']=TEXT;data['forward_origin']={'type':'hidden_user','sender_user_name':'Someone','date':OLD}
            await dp.feed_update(bot,Update(update_id=3,message=data))
            self.assertEqual(self.storage.db.execute('SELECT COUNT(*) FROM carousel_messages').fetchone()[0],0)
            self.quiz.bot.send_message.assert_not_called()
        finally:
            await bot.session.close()

    async def test_import_authors_outside_original_roster(self):
        seed=Path(self.tmp.name)/'expanded.db';db=sqlite3.connect(seed)
        db.execute('CREATE TABLE carousel_messages(chat_id,message_id,author_id,sent_at,text)')
        db.execute('CREATE TABLE carousel_authors(user_id INTEGER PRIMARY KEY,name TEXT)')
        db.execute('INSERT INTO carousel_authors VALUES (?,?)',(987654,'Новый участник'))
        db.execute('INSERT INTO carousel_messages VALUES (?,?,?,?,?)',(SOURCE_CHAT_ID,991,987654,OLD,TEXT))
        db.commit();db.close()
        self.assertEqual(self.c.import_seed(seed),1)
        row=self.c.draw(SOURCE_CHAT_ID,999999,'Гость',NOW)
        q=json.loads(row['payload'])
        self.assertEqual(q['author'],'Новый участник')
        self.assertEqual(q['options'][int(q['answer'])],'Новый участник')
        self.assertEqual(len(set(q['options'])),4)

    async def test_invitation_token_required_and_foreign_answer_rejected(self):
        self.seed()
        invalid=self.query(token='invented')
        await self.quiz.carousel.callback(invalid)
        invalid.message.edit_text.assert_not_called()
        self.assertIsNone(self.c.session(SOURCE_CHAT_ID,7))
        await self.quiz.carousel.callback(self.query(user=999999))
        row=self.c.session(SOURCE_CHAT_ID,999999)
        q=self.query(row['token'],json.loads(row['payload'])['answer'],user=7)
        await self.quiz.carousel.callback(q)
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,999999)['answers'],0)
        self.quiz.bot.get_chat_member.assert_not_called()

    async def test_sanya_accounts_merge_preserves_progress_and_replaces_old_buttons(self):
        from carousel import SANYA_ID, SANYA_ALIASES
        self.seed(1)
        row=self.c.draw(SOURCE_CHAT_ID,7,'Player',NOW)
        q=json.loads(row['payload'])
        q.update(author_id=SANYA_ALIASES[0],author='Удалённый аккаунт',
                 options=['Саня','Удалённый аккаунт','Санёк','Вика'],answer='1')
        with self.storage.db:
            for alias,name in zip(SANYA_ALIASES,['Удалённый аккаунт','Санёк']):
                self.storage.db.execute('INSERT INTO carousel_authors VALUES (?,?)',(alias,name))
            self.storage.db.execute('UPDATE carousel_messages SET author_id=?',(SANYA_ALIASES[0],))
            self.storage.db.execute('UPDATE carousel_sessions SET payload=?',(json.dumps(q,ensure_ascii=False),))
            self.storage.db.execute('UPDATE carousel_stats SET answers=6,correct=4')
        seen=[tuple(r) for r in self.storage.db.execute('SELECT * FROM carousel_seen')]
        self.c=Carousel(self.storage.db)
        after=self.c.session(SOURCE_CHAT_ID,7);new=json.loads(after['payload'])
        self.assertEqual(new['author_id'],SANYA_ID)
        self.assertEqual(new['options'][1],'Саня')
        self.assertEqual(new['options'].count('Саня'),1)
        self.assertEqual(len(set(new['options'])),4)
        self.assertNotIn('Удалённый аккаунт',new['options'])
        self.assertNotIn('Санёк',new['options'])
        self.assertEqual(new['text'],q['text'])
        self.assertNotEqual(row['token'],after['token'])
        self.assertFalse(self.c.act(SOURCE_CHAT_ID,7,row['token'],'1',NOW))
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,7)['answers'],6)
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,7)['correct'],4)
        self.assertEqual([tuple(r) for r in self.storage.db.execute('SELECT * FROM carousel_seen')],seen)
        self.assertEqual(self.c.draw(SOURCE_CHAT_ID,7,'Player',NOW)['token'],after['token'])
        self.c=Carousel(self.storage.db)
        self.assertEqual(self.c.session(SOURCE_CHAT_ID,7)['token'],after['token'])
        self.assertTrue(self.c.act(SOURCE_CHAT_ID,7,after['token'],'1',NOW))

    async def test_expanded_seed_groups_sanya_before_author_sampling(self):
        from carousel import SANYA_ID, SANYA_ALIASES
        self.c.import_seed(Path(__file__).resolve().parents[1]/'carousel-seed.db')
        names=self.c.author_names()
        self.assertEqual(len(names),14)
        self.assertTrue(all(str(uid) not in names for uid in SANYA_ALIASES))
        self.assertEqual(self.storage.db.execute('SELECT COUNT(*) FROM carousel_messages WHERE author_id=?',(SANYA_ID,)).fetchone()[0],998)
        self.assertEqual(self.storage.db.execute('SELECT COUNT(*) FROM carousel_messages').fetchone()[0],5353)
        self.assertEqual(self.c.import_seed(Path(__file__).resolve().parents[1]/'carousel-seed.db'),0)

    async def test_jelly_fox_removed_from_existing_question_without_reset(self):
        self.seed(1)
        row=self.c.draw(SOURCE_CHAT_ID,7,'Player',NOW)
        q=json.loads(row['payload']);answer=int(q['answer'])
        index=(answer+1)%4;q['options'][index]='Jelly Fox'
        with self.storage.db:
            self.storage.db.execute('INSERT INTO carousel_authors VALUES (?,?)',(7306272727,'Jelly Fox'))
            self.storage.db.execute('UPDATE carousel_sessions SET payload=?',(json.dumps(q,ensure_ascii=False),))
            self.storage.db.execute('UPDATE carousel_stats SET answers=6,correct=4')
        seen=[tuple(r) for r in self.storage.db.execute('SELECT * FROM carousel_seen')]
        self.c=Carousel(self.storage.db)
        after=self.c.session(SOURCE_CHAT_ID,7);updated=json.loads(after['payload'])
        self.assertNotIn('7306272727',self.c.author_names())
        self.assertNotIn('Jelly Fox',updated['options'])
        self.assertEqual(updated['text'],q['text'])
        self.assertEqual(updated['options'][answer],q['options'][answer])
        self.assertEqual(len(set(updated['options'])),4)
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,7)['answers'],6)
        self.assertEqual(self.c.score(SOURCE_CHAT_ID,7)['correct'],4)
        self.assertEqual([tuple(r) for r in self.storage.db.execute('SELECT * FROM carousel_seen')],seen)
        self.assertFalse(self.c.act(SOURCE_CHAT_ID,7,row['token'],str(index),NOW))
        self.c.import_seed(Path(__file__).resolve().parents[1]/'carousel-seed.db')
        self.assertNotIn('Jelly Fox',self.c.author_names().values())
        self.c=Carousel(self.storage.db)
        self.assertEqual(self.c.session(SOURCE_CHAT_ID,7)['token'],after['token'])

if __name__=='__main__':unittest.main()
