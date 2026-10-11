"""Private quiz content. Do not open quiz-data.json before playing if avoiding spoilers."""
import json
from pathlib import Path

QUIZ_ID = 'kolyandia-2026-10-v1'
TITLE = 'Я такого не говорил'
QUESTIONS = json.loads(Path(__file__).with_name('quiz-data.json').read_text(encoding='utf-8'))
PARTICIPANTS = json.loads(Path(__file__).with_name('participants.json').read_text(encoding='utf-8'))

def display_name(user_id, fallback):
    return PARTICIPANTS.get(str(user_id), fallback)

for question in QUESTIONS:
    assert len(question['options']) == len(set(question['options'])) == 4
    assert question['answer'] in ('0', '1', '2', '3')
