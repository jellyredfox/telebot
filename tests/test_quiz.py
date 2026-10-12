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
from questions import QUESTIONS, PARTICIPANTS, display_name, QUIZ_ID, AUTHOR_QUIZ_ID, AUTHOR_QUESTIONS, GAMES
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
        self.assertIsNone(self.db.attempt(-1, 9))
        self.assertEqual(personal.answer.await_count, 1)
        self.assertEqual(len(personal.answer.call_args.kwargs['reply_markup'].inline_keyboard), 2)
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

    async def test_group_cleanup_survives_restart_and_keeps_other_kinds(self):
        message = self.message(chat_type='supergroup', chat_id=-1)
        await self.quiz.quiz(message)
        self.db.close()
        self.db = Storage(self.path)
        self.quiz = Quiz(self.bot, self.db, 'test_bot')
        message.answer.return_value = NS(message_id=102)
        await self.quiz.top(message)
        self.bot.delete_message.assert_not_called()
        message.answer.return_value = NS(message_id=103)
        await self.quiz.top(message)
        self.bot.delete_message.assert_awaited_once_with(-1, 102)
        self.assertEqual(self.db.previous_output(-1, 'quiz'), 101)
        self.assertEqual(self.db.previous_output(-1, 'top'), 103)
        message.answer.return_value = NS(message_id=104)
        await self.quiz.quiz(message)
        self.bot.delete_message.assert_awaited_with(-1, 101)
        self.assertEqual(self.db.previous_output(-1, 'quiz'), 104)
        self.assertEqual(self.db.previous_output(-1, 'top'), 103)

    async def test_old_group_message_edit_fallback(self):
        self.db.remember_output(-1, 'quiz', 100)
        self.db.remember_output(-1, 'top', 200)
        self.bot.delete_message.side_effect = TelegramBadRequest(method=DeleteMessage(chat_id=-1,message_id=100), message="message can't be deleted")
        message = self.message(chat_type='supergroup', chat_id=-1)
        await self.quiz.quiz(message)
        self.bot.delete_message.assert_awaited_once_with(-1, 100)
        self.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs['message_id'], 100)
        message.answer.assert_not_called()
        self.assertEqual(self.db.previous_output(-1, 'quiz'), 100)
        self.assertEqual(self.db.previous_output(-1, 'top'), 200)

    async def test_concurrent_group_calls_keep_separate_outputs(self):
        message = self.message(chat_type='supergroup', chat_id=-1)
        message.answer.side_effect = [NS(message_id=201), NS(message_id=202)]
        await asyncio.gather(self.quiz.quiz(message), self.quiz.top(message))
        self.bot.delete_message.assert_not_called()
        self.assertEqual(self.db.previous_output(-1, 'quiz'), 201)
        self.assertEqual(self.db.previous_output(-1, 'top'), 202)
        message.answer.side_effect = [NS(message_id=203), NS(message_id=204)]
        await asyncio.gather(self.quiz.top(message), self.quiz.top(message))
        self.assertEqual([call.args for call in self.bot.delete_message.await_args_list], [(-1, 202), (-1, 203)])
        self.assertEqual(self.db.previous_output(-1, 'quiz'), 201)
        self.assertEqual(self.db.previous_output(-1, 'top'), 204)

    async def test_help_stats_and_other_groups_do_not_replace_quiz_or_top(self):
        self.db.remember_output(-1, 'quiz', 100)
        self.db.remember_output(-1, 'top', 200)
        message = self.message(chat_type='supergroup', chat_id=-1)
        message.answer.side_effect = [NS(message_id=i) for i in (301,302,303,304)]
        await self.quiz.help(message)
        await self.quiz.stats(message)
        self.bot.delete_message.assert_not_called()
        await self.quiz.help(message)
        await self.quiz.stats(message)
        self.assertEqual([call.args for call in self.bot.delete_message.await_args_list], [(-1,301),(-1,302)])
        other = self.message(chat_type='supergroup', chat_id=-2)
        await self.quiz.top(other)
        self.assertEqual(self.bot.delete_message.await_count, 2)
        self.assertEqual(self.db.previous_output(-1, 'quiz'), 100)
        self.assertEqual(self.db.previous_output(-1, 'top'), 200)

    async def test_legacy_untyped_card_is_not_guessed_or_deleted(self):
        with self.db.db:
            self.db.db.execute('INSERT INTO group_output VALUES (?,?)', (-1, 999))
        self.db.close()
        self.db = Storage(self.path)
        self.quiz = Quiz(self.bot, self.db, 'test_bot')
        message = self.message(chat_type='supergroup', chat_id=-1)
        await self.quiz.top(message)
        self.bot.delete_message.assert_not_called()
        self.assertEqual(self.db.db.execute('SELECT message_id FROM group_output WHERE chat_id=-1').fetchone()[0], 999)
        self.assertEqual(self.db.attempt(-1,7)['questions'], self.attempt['questions'])

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

    def menu_query(self, key='author', token=None, user=7):
        return NS(data=f'game:{token or self.group["token"]}:{key}',
                  from_user=NS(id=user, full_name='Alice'), message=self.message(user), answer=AsyncMock())

    async def test_two_games_switch_and_independent_attempts(self):
        await self.quiz.callback(self.callback())
        q = self.menu_query()
        await self.quiz.menu_callback(q)
        author = self.db.attempt(-1, 7, AUTHOR_QUIZ_ID)
        self.assertNotEqual(author['id'], self.attempt['id'])
        self.assertEqual(self.db.score(author['id'])['answers'], 0)
        await self.quiz.menu_callback(self.menu_query('finish'))
        self.assertEqual(self.db.score(self.attempt['id'])['points'], 1)
        self.assertEqual(self.db.attempt(-1, 7)['questions'], self.attempt['questions'])
        await self.quiz.menu_callback(self.menu_query())
        self.assertEqual(self.db.attempt(-1, 7, AUTHOR_QUIZ_ID)['id'], author['id'])
        text, keyboard = self.quiz.render(author)
        self.assertIn('Кто это сказал?', text)
        self.assertNotIn('Правильный вариант', text)
        self.assertNotIn(json.loads(author['questions'])[0]['date'], text)
        self.assertIn('menu:', keyboard.inline_keyboard[-1][0].callback_data)

    async def test_menu_is_bound_to_group_and_rejects_bad_buttons(self):
        other = self.db.group(-2, 'Other')
        self.db.set_context(-2, 7)
        await self.quiz.menu_callback(self.menu_query())
        self.assertIsNotNone(self.db.attempt(-1, 7, AUTHOR_QUIZ_ID))
        self.assertIsNone(self.db.attempt(-2, 7, AUTHOR_QUIZ_ID))
        self.assertEqual(self.db.context(7)['chat_id'], -1)
        for data in ('game:missing:author', f'game:{other["token"]}:unknown', 'menu:', 'game'):
            q = self.menu_query(); q.data = data
            await self.quiz.menu_callback(q)
            q.message.edit_text.assert_not_called()
        q = self.menu_query(); q.message.chat.id = 99
        await self.quiz.menu_callback(q)
        q.message.edit_text.assert_not_called()

    async def test_author_finished_ranking_stats_and_restart(self):
        await self.quiz.menu_callback(self.menu_query())
        author = self.db.attempt(-1, 7, AUTHOR_QUIZ_ID)
        questions = json.loads(author['questions'])
        for pos, question in enumerate(questions):
            q = self.callback(pos, question['answer'])
            q.data = f'q:{author["id"]}:{pos}:{question["answer"]}'
            await self.quiz.callback(q)
            await self.quiz.callback(q)
            if pos < len(questions)-1:
                self.assertEqual(self.db.ranking(-1, AUTHOR_QUIZ_ID), [])
                q.data = f'q:{author["id"]}:{pos}:next'
                await self.quiz.callback(q)
        self.assertEqual(self.db.score(author['id'])['points'], 10)
        self.assertEqual(self.db.ranking(-1), [])
        self.assertEqual(self.db.ranking(-1, AUTHOR_QUIZ_ID)[0]['points'], 10)
        text, keyboard = self.quiz.render(self.db.get(author['id'], 7))
        self.assertIn('10/10', text)
        self.assertTrue(keyboard.inline_keyboard[0][0].callback_data.startswith('menu:'))
        self.db.close(); self.db = Storage(self.path)
        self.quiz = Quiz(self.bot, self.db, 'test_bot')
        self.assertEqual(self.db.start(-1, 7, 'Alice', AUTHOR_QUIZ_ID)['id'], author['id'])
        self.assertTrue(self.db.get(author['id'], 7)['finished'])
        for handler in (self.quiz.top, self.quiz.stats):
            msg = self.message()
            await handler(msg)
            text = msg.answer.call_args.args[0]
            self.assertIn('Кто это сказал?', text)
            self.assertIn('Закончи фразу', text)
        self.bot.send_message.assert_not_called()

    async def test_nine_point_completion_result_preserved(self):
        questions = json.loads(self.attempt['questions'])
        for pos, question in enumerate(questions):
            choice = question['answer'] if pos else str((int(question['answer'])+1)%4)
            self.db.answer(self.attempt['id'], 7, pos, choice)
            self.db.advance(self.attempt['id'], 7, pos)
        self.db.close(); self.db = Storage(self.path)
        self.quiz = Quiz(self.bot, self.db, 'test_bot')
        await self.quiz.menu_callback(self.menu_query())
        await self.quiz.menu_callback(self.menu_query('finish'))
        old = self.db.attempt(-1, 7)
        self.assertEqual(old['id'], self.attempt['id'])
        self.assertEqual(old['questions'], self.attempt['questions'])
        self.assertEqual(old['quiz_id'], 'kolyandia-2026-10-v1')
        self.assertTrue(old['finished'])
        self.assertEqual(self.db.ranking(-1)[0]['points'], 9)
        self.assertEqual(self.db.score(old['id'])['answers'], 10)

    async def test_author_data_and_callback_lengths(self):
        self.assertFalse({q['source_id'] for q in QUESTIONS} & {q['source_id'] for q in AUTHOR_QUESTIONS})
        for q in AUTHOR_QUESTIONS:
            self.assertEqual(q['name'], q['quote'])
            self.assertEqual(q['options'][int(q['answer'])], q['author'])
            self.assertTrue(set(q['options']).issubset(set(PARTICIPANTS.values())))
        msg = self.message()
        await self.quiz.show_menu(msg, self.group, 7)
        for row in msg.answer.call_args.kwargs['reply_markup'].inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)

if __name__ == '__main__':
    unittest.main()
