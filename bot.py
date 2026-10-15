import asyncio
import contextlib
import json
import logging
import os
from collections import defaultdict
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from questions import TITLE, QUIZ_ID, GAMES, GAMES_BY_ID, display_name
from config import database_path
from pathlib import Path
from carousel import CarouselUI, SOURCE_CHAT_ID
from storage import Storage

HELP = ('🎭 Я такого не говорил\n'
        'В группе: /quiz — ссылка на личное прохождение; /top — результаты завершивших.\n'
        'В личном чате: /quiz — выбрать игру или продолжить, /stats — ваш прогресс, /top — рейтинг выбранной группы.\n\n'
        'Без таймера. Один ответ на вопрос, 1 очко за правильный ответ. '
        'Одна попытка на каждый выпуск в группе. Рейтинги выпусков раздельные. Можно вернуться в любое время. '
        'Результат попадает в рейтинг только после последнего ответа.\n\n'
        '🎠 Карусель Санька — без ограничения числа вопросов. Сообщения старше двух месяцев, '
        '60–300 символов. Точность и место в отдельном рейтинге — после 20 ответов. '
        'Пропусков нет: следующий вопрос после ответа. /carousel_top — рейтинг карусели. '
        'Карусель работает только по загруженному архиву. Новые сообщения не записываются.')

class Quiz:
    def __init__(self, bot, store, username):
        self.bot, self.store, self.username = bot, store, username
        self.locks = defaultdict(asyncio.Lock)
        self.group_locks = defaultdict(asyncio.Lock)
        self.carousel = CarouselUI(self)
        self.router = Router()
        self.router.message.register(self.start, Command('start'))
        self.router.message.register(self.quiz, Command('quiz'))
        self.router.message.register(self.help, Command('help'))
        self.router.message.register(self.top, Command('top'))
        self.router.message.register(self.stats, Command('stats'))
        self.router.message.register(self.carousel_top, Command('carousel_top'))
        self.router.callback_query.register(self.carousel.callback, F.data.startswith('c:'))
        self.router.callback_query.register(self.menu_callback, F.data.startswith('menu:') | F.data.startswith('game:'))
        self.router.callback_query.register(self.callback, F.data.startswith('q:'))
        self.router.callback_query.register(self.old_button, F.data.startswith('a:') | F.data.startswith('p:'))

    def invitation(self, group):
        url = f'https://t.me/{self.username}?start=g_{group["token"]}'
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Пройти лично →', url=url)]])

    async def respond(self, message, text, *, output_kind=None, **kwargs):
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return await message.answer(text, **kwargs)
        async with self.group_locks[message.chat.id]:
            group = self.store.group(message.chat.id, message.chat.title or 'Группа')
            kwargs.setdefault('reply_markup', self.invitation(group))
            previous = self.store.previous_output(message.chat.id, output_kind) if output_kind else None
            if previous:
                try:
                    await self.bot.delete_message(message.chat.id, previous)
                except TelegramBadRequest as exc:
                    if 'message to delete not found' not in str(exc).lower():
                        # Telegram normally cannot delete messages older than 48 hours.
                        # Reuse that message instead of producing another one.
                        try:
                            await self.bot.edit_message_text(text, chat_id=message.chat.id,
                                                             message_id=previous, **kwargs)
                            return
                        except TelegramBadRequest as edit_exc:
                            if 'message is not modified' in str(edit_exc).lower():
                                return
                            logging.warning('Old group message could not be deleted or edited: %s', previous)
            sent = await message.answer(text, **kwargs)
            if output_kind:
                self.store.remember_output(message.chat.id, output_kind, sent.message_id)
            return sent

    async def help(self, message: Message):
        await self.respond(message, HELP, output_kind="help")

    async def old_button(self, query: CallbackQuery):
        await query.answer('Это кнопка прошлой викторины. Откройте новое приглашение /quiz.', show_alert=True)

    async def quiz(self, message: Message):
        if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            group = self.store.group(message.chat.id, message.chat.title or 'Группа')
            await self.respond(message, f'🎭 {TITLE}\n«Закончи фразу» и два выпуска «Кто это сказал?» — по 10 вопросов.\n'
                               'Плюс 🎠 Карусель Санька — бесконечный режим в Коляндии.\n'
                               'Четыре варианта, без таймера. Всё прохождение — в личке.\n'
                               'По одной попытке в каждом выпуске. Раздельные рейтинги — /top.\n'
                               'Нажмите кнопку, затем «Начать».', reply_markup=self.invitation(group), output_kind='quiz')
            return
        if message.chat.type != ChatType.PRIVATE or not message.from_user:
            return
        async with self.locks[message.from_user.id]:
            group = self.store.context(message.from_user.id)
            if not group:
                await self.respond(message, 'Откройте ссылку из вашей группы: там нужно отправить /quiz.')
                return
            await self.show_menu(message, group, message.from_user.id)

    async def start(self, message: Message):
        if message.chat.type != ChatType.PRIVATE or not message.from_user:
            await self.help(message)
            return
        parts = (message.text or '').split(maxsplit=1)
        if len(parts) == 1:
            await self.quiz(message)
            return
        token = parts[1][2:] if parts[1].startswith('g_') else ''
        group = self.store.by_token(token)
        if not group:
            await self.respond(message, 'Ссылка не найдена. Получите новую командой /quiz в группе.')
            return
        async with self.locks[message.from_user.id]:
            self.store.set_context(group['chat_id'], message.from_user.id)
            await self.show_menu(message, group, message.from_user.id)

    def menu_button(self, chat_id):
        token = self.store.db.execute('SELECT token FROM quiz_groups WHERE chat_id=?', (chat_id,)).fetchone()[0]
        return [InlineKeyboardButton(text='← Выбрать игру', callback_data=f'menu:{token}')]

    async def show_menu(self, message, group, user_id, edit=False):
        text = f'🎭 {TITLE}\nГруппа «{escape(group["title"])}»\n\nВыберите игру и выпуск:\n'
        buttons = []
        for key, game in GAMES.items():
            attempt = self.store.attempt(group['chat_id'], user_id, game['id'])
            state = 'Начать'
            if attempt:
                score = self.store.score(attempt['id'])
                count = len(json.loads(attempt['questions']))
                state = f'Итог {score["points"]}/{count}' if attempt['finished'] else f'Продолжить · {score["answers"]}/{count}'
            text += f'\n<b>{game["title"]}</b> — {state.lower()}'
            buttons.append([InlineKeyboardButton(text=f'{game["title"]} · {state}', callback_data=f'game:{group["token"]}:{key}')])
        if group['chat_id'] == SOURCE_CHAT_ID:
            buttons.append([InlineKeyboardButton(text='🎠 Карусель Санька', callback_data=f'c:{group["token"]}:open')])
            text += '\n\n🎠 Карусель Санька — без конца, отдельный рейтинг по точности после 20 ответов. Следующий вопрос — после ответа.'
        text += '\n\nБез таймера. По одной попытке на выпуск. Результаты и рейтинги раздельные. Можно переключаться и продолжать позже.'
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        if edit:
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as exc:
                if 'message is not modified' in str(exc).lower():
                    return
        await self.respond(message, text, reply_markup=markup)

    async def menu_callback(self, query: CallbackQuery):
        if not query.message or query.message.chat.type != ChatType.PRIVATE or query.message.chat.id != query.from_user.id:
            await query.answer('Откройте игру в личном чате.', show_alert=True)
            return
        parts = (query.data or '').split(':')
        valid = ((len(parts) == 2 and parts[0] == 'menu') or
                 (len(parts) == 3 and parts[0] == 'game' and parts[2] in GAMES))
        group = self.store.by_token(parts[1]) if valid else None
        if not group:
            await query.answer('Получите приглашение командой /quiz в группе.')
            return
        async with self.locks[query.from_user.id]:
            self.store.set_context(group['chat_id'], query.from_user.id)
            with contextlib.suppress(TelegramBadRequest):
                await query.answer()
            if parts[0] == 'menu':
                await self.show_menu(query.message, group, query.from_user.id, edit=True)
            else:
                attempt = self.store.start(group['chat_id'], query.from_user.id,
                                           query.from_user.full_name, GAMES[parts[2]]['id'])
                await self.show(query.message, attempt, edit=True)

    def table(self, chat_id, quiz_id=QUIZ_ID):
        rows = self.store.ranking(chat_id, quiz_id)
        if not rows:
            return 'Пока никто не завершил викторину.'
        lines, place, previous = [], 0, None
        for index, row in enumerate(rows[:20], 1):
            if row['points'] != previous:
                place = index
            previous = row['points']
            name = escape(' '.join(display_name(row['user_id'], row['name']).split())[:40])
            lines.append(f'{place}. {name} — {row["points"]} очк.')
        return '\n'.join(lines) + (f'\nЗавершили: {len(rows)}. Показаны первые 20.' if len(rows) > 20 else '')

    def render(self, attempt):
        questions = json.loads(attempt['questions'])
        pos = attempt['position']
        question = questions[pos]
        answer = self.store.answer_at(attempt['id'], pos)
        title = self.store.db.execute('SELECT title FROM quiz_groups WHERE chat_id=?', (attempt['chat_id'],)).fetchone()[0]
        game = GAMES_BY_ID[attempt['quiz_id']]
        text = f'<b>{escape(title)} · {escape(game["title"])} · {pos + 1}/{len(questions)}</b>\n<b>{escape(question["name"])}</b>\n'
        if answer:
            text += ('✅ Верно! +1 очко.\n' if answer['correct'] else 'Неверно. +0 очков.\n')
            text += f'Правильный вариант: {escape(question["options"][int(question["answer"])])}\n\n'
            text += f'«{escape(question["quote"])}»\n— {escape(question["author"])}, {question["date"]}'
            if attempt['finished']:
                score = self.store.score(attempt['id'])['points']
                text += f'\n\n🏁 Ваш результат: {score}/{len(questions)}.\n🏆 Рейтинг группы\n' + self.table(attempt['chat_id'], attempt['quiz_id'])
                return text, InlineKeyboardMarkup(inline_keyboard=[self.menu_button(attempt['chat_id'])])
            buttons = [[InlineKeyboardButton(text='Следующий вопрос →', callback_data=f'q:{attempt["id"]}:{pos}:next')]]
        else:
            text += '\n' + '\n\n'.join(f'{i + 1}. {escape(option)}' for i, option in enumerate(question['options']))
            text += '\n\nВыберите номер. Время не ограничено.'
            buttons = [[InlineKeyboardButton(text=str(choice + 1), callback_data=f'q:{attempt["id"]}:{pos}:{choice}')
                        for choice in range(4)]]
        buttons.append(self.menu_button(attempt['chat_id']))
        return text, InlineKeyboardMarkup(inline_keyboard=buttons)

    async def show(self, message, attempt, edit=False):
        text, keyboard = self.render(attempt)
        if edit:
            try:
                await message.edit_text(text, reply_markup=keyboard)
                return
            except TelegramBadRequest as exc:
                if 'message is not modified' in str(exc):
                    return
                # A deleted/too-old message does not lose the durable progress.
        await self.respond(message, text, reply_markup=keyboard)

    async def callback(self, query: CallbackQuery):
        if not query.message or query.message.chat.type != ChatType.PRIVATE or query.message.chat.id != query.from_user.id:
            await query.answer('Пройдите викторину в личном чате с ботом.', show_alert=True)
            return
        try:
            _, attempt_id, position, action = (query.data or '').split(':')
            attempt_id, position = int(attempt_id), int(position)
        except ValueError:
            await query.answer('Некорректная кнопка.')
            return
        async with self.locks[query.from_user.id]:
            attempt = self.store.get(attempt_id, query.from_user.id)
            if not attempt or action not in ('0', '1', '2', '3', 'next'):
                await query.answer('Эта кнопка недоступна.')
                return
            if action == 'next':
                accepted = self.store.advance(attempt_id, query.from_user.id, position)
            else:
                accepted = self.store.answer(attempt_id, query.from_user.id, position, action)
            # Changes are committed before contacting Telegram. /quiz can always restore the screen.
            with contextlib.suppress(TelegramBadRequest):
                await query.answer('' if accepted else 'Ответ уже сохранён или вопрос пройден.')
            await self.show(query.message, self.store.get(attempt_id, query.from_user.id), edit=True)

    def selected_group(self, message):
        if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            return self.store.group(message.chat.id, message.chat.title or 'Группа')
        return self.store.context(message.from_user.id) if message.from_user else None

    async def top(self, message: Message):
        group = self.selected_group(message)
        if not group:
            await self.respond(message, 'Сначала откройте ссылку /quiz из группы.')
            return
        await self.respond(message, f'🏆 {escape(group["title"])}\n\n' + '\n\n'.join(f'<b>{game["title"]}</b>\n' + self.table(group['chat_id'], game['id']) for game in GAMES.values()) + ('\n\n🎠 Карусель Санька: /carousel_top' if group['chat_id']==SOURCE_CHAT_ID else ''), output_kind='top')

    async def carousel_top(self, message: Message):
        group = self.selected_group(message)
        if not group or group['chat_id'] != SOURCE_CHAT_ID:
            await self.respond(message, 'Карусель доступна только для Коляндии.')
            return
        await self.respond(message, self.carousel.store.table(group['chat_id']), output_kind='carousel_top')

    async def stats(self, message: Message):
        if message.chat.type != ChatType.PRIVATE:
            await self.respond(message, 'Личная статистика доступна через /stats в личном чате с ботом.', output_kind='stats')
            return
        group = self.selected_group(message)
        if not group:
            await self.respond(message, 'Сначала откройте ссылку /quiz из группы.')
            return
        lines = [escape(group['title'])]
        for game in GAMES.values():
            attempt = self.store.attempt(group['chat_id'], message.from_user.id, game['id'])
            lines.append(f'\n<b>{game["title"]}</b>')
            if not attempt:
                lines.append('Ещё не начато.')
                continue
            score = self.store.score(attempt['id'])
            count = len(json.loads(attempt['questions']))
            state = 'Завершено, результат в рейтинге.' if attempt['finished'] else 'В процессе.'
            lines.append(f'Ответов: {score["answers"]}/{count} · Очки: {score["points"]}\n{state}')
        if group['chat_id'] == SOURCE_CHAT_ID:
            lines.append('\n<b>🎠 Карусель Санька</b>\n' + self.carousel.store.stat_text(group['chat_id'],message.from_user.id))
        lines.append('\n/quiz — выбрать игру или продолжить.')
        await self.respond(message, '\n'.join(lines))

async def main():
    load_dotenv()
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('Задайте BOT_TOKEN в .env или переменных окружения.')
    bot = Bot(token, default=DefaultBotProperties(parse_mode='HTML'))
    store = Storage(database_path())
    try:
        me = await bot.get_me()
        quiz = Quiz(bot, store, me.username)
        imported = quiz.carousel.store.import_seed(Path(__file__).with_name('carousel-seed.db'))
        logging.info('Carousel seed processed: %s rows', imported)
        dp = Dispatcher()
        dp.include_router(quiz.router)
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
            ('quiz', 'Начать или продолжить лично'), ('top', 'Рейтинг завершивших'),
            ('stats', 'Мой прогресс'), ('carousel_top', 'Карусель: рейтинг по точности'), ('help', 'Правила')]])
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        store.close()
        await bot.session.close()

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
