from aiogram.fsm.state import State, StatesGroup


class DraftStates(StatesGroup):
    choosing_group = State()
    post_text = State()
    terms_text = State()
    image = State()
    winners_count = State()
    deadline = State()


class EditStates(StatesGroup):
    text = State()
    terms = State()
    deadline = State()
    winners = State()
    image = State()


class CaptchaStates(StatesGroup):
    answer = State()
