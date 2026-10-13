import json
from pathlib import Path

ROOT = Path(__file__).parent
QUIZ_ID = 'kolyandia-2026-10-v1'  # Keep: existing scores and attempts use this ID.
AUTHOR_QUIZ_ID = 'kolyandia-authors-2026-10-v1'
AUTHOR_V2_QUIZ_ID = 'kolyandia-authors-2026-10-v2'
TITLE = 'Я такого не говорил!'
QUESTIONS = json.loads((ROOT / 'quiz-data.json').read_text(encoding='utf-8'))
AUTHOR_QUESTIONS = json.loads((ROOT / 'author-data.json').read_text(encoding='utf-8'))
AUTHOR_V2_QUESTIONS = json.loads((ROOT / 'author-data-v2.json').read_text(encoding='utf-8'))
PARTICIPANTS = json.loads((ROOT / 'participants.json').read_text(encoding='utf-8'))
GAMES = {
    'finish': {'id': QUIZ_ID, 'title': 'Закончи фразу', 'questions': QUESTIONS},
    'author': {'id': AUTHOR_QUIZ_ID, 'title': 'Кто это сказал? · Выпуск 1', 'questions': AUTHOR_QUESTIONS},
    'author2': {'id': AUTHOR_V2_QUIZ_ID, 'title': 'Кто это сказал? · Выпуск 2', 'questions': AUTHOR_V2_QUESTIONS},
}
GAMES_BY_ID = {game['id']: game for game in GAMES.values()}

def display_name(user_id, fallback):
    return PARTICIPANTS.get(str(user_id), fallback)

for game in GAMES.values():
    for question in game['questions']:
        assert len(question['options']) == len(set(question['options'])) == 4
        assert question['answer'] in ('0', '1', '2', '3')
