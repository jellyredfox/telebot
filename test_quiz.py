import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bot import Quiz
from config import database_path
from questions import QUESTIONS, PARTICIPANTS, display_name
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage
from storage import Storage

class QuizTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'quiz.db')
        self.db = Storage(self.path)
        self.bot = AsyncMock()
        self.quiz = Quiz(self.bot, self.db, 'test_bot')
        self.group = self.db.group(-1, '<Friends>')
        self.attempt = self.db.start(-1, 7, '<Alice>')

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def message(self, user=7, chat_type='private', chat_id=None, text='/quiz'):
        message = NS(chat=NS(id=chat_id if chat_id is not None else user, type=chat_type, title='Friends'),
                  from_user=NS(id=user, full_name='Alice'), text=text,
                  answer=AsyncMock(), edit_text=AsyncMock())
        message.answer.return_value = NS(message_id=101)
        return message

    def callback(self, pos=0, action=None, user=7):
        action = action if action is not None else json.loads(self.attempt['questions'])[pos]['answer']
        return NS(data=f'q:{self.attempt["id"]}:{pos}:{action}', from_user=NS(id=user),
                  message=self.message(user), answer=AsyncMock())

    async def test_independent_users_and_no_group_messages(self):
        second = self.db.start(-1, 8, 'Bob')
        await self.quiz.callback(self.callback())
        self.assertEqual(self.db.score(second['id'])['answers'], 0)
        self.assertEqual(self.db.ranking(-1), [])
        self.bot.send_message.assert_not_called()

    async def test_concurrent_duplicate_answer_and_next(self):
        await asyncio.gather(self.quiz.callback(self.callback()), self.quiz.callback(self.callback()))
        self.assertEqual(self.db.score(self.attempt['id'])['points'], 1)
        await asyncio.gather(self.quiz.callback(self.callback(action='next')), self.quiz.callback(self.callback(action='next')))
        self.assertEqual(self.db.get(self.attempt['id'], 7)['position'], 1)
        await self.quiz.callback(self.callback())
        self.assertEqual(self.db.score(self.attempt['id'])['answers'], 1)

    async def test_ownership_invalid_choice_and_skip(self):
        await self.quiz.callback(self.callback(user=8))
        await self.quiz.callback(self.callback(action='next'))
        await self.quiz.callback(self.callback(action='invalid'))
        self.assertEqual(self.db.score(self.attempt['id'])['answers'], 0)
        self.assertEqual(self.db.get(self.attempt['id'], 7)['position'], 0)

    async def test_full_game_finished_only_ranking_and_single_attempt(self):
        questions = json.loads(self.attempt['questions'])
        for pos in range(len(questions)):
            q = self.callback(pos)
            await self.quiz.callback(q)
            q.message.edit_text.assert_awaited_once()
            if pos < len(questions) - 1:
                self.assertEqual(self.db.ranking(-1), [])
                await self.quiz.callback(self.callback(pos, 'next'))
        self.assertEqual(self.db.ranking(-1)[0]['points'], len(QUESTIONS))
        self.assertIn('&lt;Alice&gt;', self.quiz.table(-1))
        resumed = self.db.start(-1, 7, 'Alice')
        self.assertEqual(resumed['id'], self.attempt['id'])
        self.assertTrue(resumed['finished'])
        self.bot.send_message.assert_not_called()

    async def test_resume_after_restart_keeps_order_feedback_and_context(self):
        await self.quiz.callback(self.callback())
        self.db.close()
        self.db = Storage(self.path)
        self.quiz = Quiz(self.bot, self.db, 'test_bot')
        attempt = self.db.get(self.attempt['id'], 7)
        self.assertEqual(attempt['questions'], self.attempt['questions'])
        text, keyboard = self.quiz.render(attempt)
        self.assertIn('Верно', text)
        self.assertIn('next', keyboard.inline_keyboard[0][0].callback_data)
        self.assertEqual(self.db.context(7)['chat_id'], -1)
        self.assertEqual(self.db.by_token(self.group['token'])['chat_id'], -1)

    async def test_group_invitation_and_deep_link(self):
        message = self.message(chat_type='supergroup', chat_id=-1)
        await self.quiz.quiz(message)
        url = message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url
        self.assertIn('https://t.me/test_bot?start=g_', url)
        personal = self.message(user=9, text='/start g_' + self.group['token'])
        await self.quiz.start(personal)
        self.assertIsNotNone(self.db.attempt(-1, 9))
        self.assertEqual(personal.answer.await_count, 2)
        invalid = self.message(user=10, text='/start g_unknown')
        await self.quiz.start(invalid)
        self.assertIsNone(self.db.attempt(-1, 10))

    async def test_group_isolation_and_context_switch(self):
        self.db.group(-2, 'Other')
        other = self.db.start(-2, 7, 'Alice')
        self.assertNotEqual(other['id'], self.attempt['id'])
        self.assertEqual(self.db.context(7)['chat_id'], -2)
        self.assertEqual(self.db.ranking(-2), [])

    async def test_wrong_answer(self):
        correct = json.loads(self.attempt['questions'])[0]['answer']
        await self.quiz.callback(self.callback(action=str((int(correct) + 1) % 4)))
        self.assertEqual(self.db.score(self.attempt['id'])['points'], 0)
        self.assertEqual(self.db.score(self.attempt['id'])['answers'], 1)

    async def test_legacy_database_is_preserved(self):
        with self.db.db:
            self.db.db.execute('CREATE TABLE games(id INTEGER PRIMARY KEY, status TEXT)')
            self.db.db.execute("INSERT INTO games VALUES (1,'finished')")
        self.db.close()
        self.db = Storage(self.path)
        self.assertEqual(self.db.db.execute('SELECT status FROM games').fetchone()[0], 'finished')
        self.assertIsNotNone(self.db.get(self.attempt['id'], 7))

    async def test_group_cleanup_survives_restart(self):
        message = self.message(chat_type='supergroup', chat_id=-1)
        await self.quiz.quiz(message)
        self.assertEqual(self.db.previous_output(-1), 101)
        self.db.close()
        self.db = Storage(self.path)
        self.quiz = Quiz(self.bot, self.db, 'test_bot')
        message.answer.return_value = NS(message_id=102)
        await self.quiz.top(message)
        self.bot.delete_message.assert_awaited_once_with(-1, 101)
        self.assertEqual(self.db.previous_output(-1), 102)
        self.assertIn('Пройти лично', message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0].text)

    async def test_old_group_message_edit_fallback(self):
        self.db.remember_output(-1, 100)
        self.bot.delete_message.side_effect = TelegramBadRequest(method=DeleteMessage(chat_id=-1,message_id=100), message="message can't be deleted")
        message = self.message(chat_type='supergroup', chat_id=-1)
        await self.quiz.quiz(message)
        self.bot.edit_message_text.assert_awaited_once()
        message.answer.assert_not_called()
        self.assertEqual(self.db.previous_output(-1), 100)

    async def test_concurrent_group_calls_leave_one_tracked_output(self):
        message = self.message(chat_type='supergroup', chat_id=-1)
        message.answer.side_effect = [NS(message_id=201), NS(message_id=202)]
        await asyncio.gather(self.quiz.quiz(message), self.quiz.top(message))
        self.bot.delete_message.assert_awaited_once_with(-1, 201)
        self.assertEqual(self.db.previous_output(-1), 202)

    async def test_names_and_no_author_before_answer(self):
        self.assertEqual(len(set(PARTICIPANTS.values())), 13)
        self.assertEqual(display_name(89858459, 'random'), 'Артем К.')
        self.assertEqual(display_name(774349153, 'random'), 'Артем Г.')
        attempt = self.db.start(-1, 89858459, 'changed nickname')
        self.assertEqual(attempt['name'], 'Артем К.')
        text, keyboard = self.quiz.render(attempt)
        q = json.loads(attempt['questions'])[0]
        self.assertNotIn(q['date'], text)
        self.assertNotIn('Правильный вариант:', text)
        self.assertEqual(len(keyboard.inline_keyboard[0]), 4)
        self.assertTrue(all(len(b.callback_data.encode()) < 64 for b in keyboard.inline_keyboard[0]))

    async def test_railway_storage_guard(self):
        with patch.dict('os.environ', {'RAILWAY_PROJECT_ID':'test'}, clear=True):
            with self.assertRaises(SystemExit):
                database_path()
        with patch.dict('os.environ', {'RAILWAY_PROJECT_ID':'test','RAILWAY_VOLUME_MOUNT_PATH':self.tmp.name}, clear=True):
            self.assertTrue(Path(database_path()).is_relative_to(Path(self.tmp.name).resolve()))
        with patch.dict('os.environ', {'RAILWAY_PROJECT_ID':'test','RAILWAY_VOLUME_MOUNT_PATH':self.tmp.name,'DB_PATH':'/tmp/outside.sqlite3'}, clear=True):
            with self.assertRaises(SystemExit):
                database_path()

    async def test_shuffled_answers_still_match_quote(self):
        for q in json.loads(self.attempt['questions']):
            self.assertEqual(len(set(q['options'])), 4)
            self.assertIn(q['options'][int(q['answer'])], q['quote'])

if __name__ == '__main__':
    unittest.main()
