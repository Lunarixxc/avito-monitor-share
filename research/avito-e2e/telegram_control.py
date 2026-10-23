"""Private Telegram controls for the owner's Avito top."""
import asyncio
import datetime as dt
import logging
import os
import re
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

import notify
from notifications import STATE
from preferences import change, listing_id

MOSCOW = ZoneInfo('Europe/Moscow')
router = Router()


def owner(chat_id, user_id, values):
    expected = int(values['TELEGRAM_CHAT_ID'])
    return chat_id == expected and user_id == expected


def result_text(result):
    return {
        'hidden': 'Скрыто до следующего подтверждённого изменения цены.',
        'already_hidden': 'Объявление уже скрыто.',
        'visible': 'Объявление снова может попадать в топ.',
        'already_visible': 'Объявление уже не скрыто.',
        'missing': 'Не нашёл объявление в сохранённой базе.',
        'unverified': 'У объявления нет проверенной цены карточки.',
        'stale': 'Цена уже изменилась. Откройте свежий топ или используйте /hide со ссылкой.',
        'invalid': 'Укажите ID или ссылку на объявление Авито.',
    }[result]


@router.message(Command('start', 'cancel'))
async def help_command(message: Message, values: dict):
    if not owner(message.chat.id, message.from_user.id if message.from_user else None, values):
        return
    if message.text and message.text.startswith('/cancel'):
        await message.answer('Активного ввода нет. Используйте /hide или /show со ссылкой.')
    else:
        await message.answer('Кнопка «🙈 Убрать» под вечерним топом скрывает объявление до изменения '
                             'проверенной цены. Команды: /hide <ссылка или ID>, /show <ссылка или ID>, /cancel.')


@router.message(Command('hide', 'show'))
async def choice_command(message: Message, command: CommandObject, values: dict):
    if not owner(message.chat.id, message.from_user.id if message.from_user else None, values):
        return
    identifier = listing_id(command.args)
    if not identifier:
        await message.answer(result_text('invalid'))
        return
    action = command.command.lower()
    result = await asyncio.to_thread(change, STATE, identifier, action, dt.datetime.now(MOSCOW).isoformat())
    await message.answer(result_text(result))


@router.callback_query(F.data.startswith('hide:'))
async def hide_callback(query: CallbackQuery, values: dict):
    chat_id = query.message.chat.id if query.message else None
    if not owner(chat_id, query.from_user.id, values):
        await query.answer()
        return
    match = re.fullmatch(r'hide:(\d+):(\d+(?:\.\d+)?)', query.data or '')
    if not match:
        await query.answer(result_text('invalid'), show_alert=True)
        return
    await query.answer('Обрабатываю...')
    result = await asyncio.to_thread(change, STATE, match[1], 'hide', dt.datetime.now(MOSCOW).isoformat(),
                                     expected_price=float(match[2]))
    await query.bot.send_message(chat_id, result_text(result))


async def main():
    os.umask(0o077)
    logging.disable(logging.CRITICAL)  # Third-party transport errors may contain the token URL.
    values = notify.config()
    if not notify.ready(values) or not values.get('TELEGRAM_PROXY'):
        raise RuntimeError('telegram_not_configured')
    session = AiohttpSession(proxy=values['TELEGRAM_PROXY'])
    bot = Bot(token=values['BOT_TOKEN'], session=session)
    dispatcher = Dispatcher(values=values)
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot, allowed_updates=['message', 'callback_query'])


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print(type(exc).__name__)
        raise SystemExit(1)
