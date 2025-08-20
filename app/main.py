"""
Entry point for the Valera bot.

This module wires together the configuration, database models, OpenAI API
integration and the Telegram Bot API.  It defines the bot's command handlers,
state machines and payment processing logic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import base64
from typing import List, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ChatAction
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, FSInputFile, PhotoSize,
                           InputMediaPhoto, LabeledPrice, PreCheckoutQuery,
                           SuccessfulPayment, URLInputFile, InlineKeyboardMarkup,
                           InlineKeyboardButton, BotCommand)

from .config import Config
from .database import (
    async_session_factory,
    init_db,
    get_user,
    create_user,
    add_credits,
    deduct_credits,
    set_membership,
    grant_referral_bonus,
    log_message,
)
from .openai_client import OpenAIClient
from .prompts import build_chat_prompt, build_profile_prompt, SYSTEM_PROMPT
from .utils import generate_referral_code

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Form(StatesGroup):
    """Conversation states for different user flows."""
    # Ожидание текста или скринов для разбора переписки
    chat_waiting_input = State()
    # Ожидание анкеты девушки
    girl_profile_waiting_input = State()
    # Ожидание анкеты пользователя
    my_profile_waiting_input = State()
    # Ожидание описания неловкой паузы или контекста
    pause_waiting_input = State()


async def ensure_membership(bot: Bot, config: Config, user_id: int) -> bool:
    """Check if the user is a member of the required channel."""
    try:
        member = await bot.get_chat_member(config.telegram_channel_id, user_id)
        status = getattr(member, "status", None)
        # Consider user a member if not left or kicked
        return status not in ("left", "kicked")
    except Exception as exc:
        logger.warning("Failed to get chat member: %s", exc)
        return False


async def handle_start(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
) -> None:
    """Handle the /start command, including referral codes and membership gating."""
    # Extract any argument passed after /start manually, rather than relying on
    # ``message.command`` which may not be present on the Message object. The
    # Command filter passes the message through without attaching a ``command``
    # attribute, so attempting to access ``message.command`` raises
    # ``AttributeError``. Instead, parse the text of the message and look
    # for a token after the command name.  ``arg`` will be ``None`` if no
    # argument was supplied.
    arg = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1]
    telegram_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    referred_by: Optional[int] = None
    if arg and arg.startswith("ref_"):
        try:
            # arg like ref_<telegram_id>
            referred_by = int(arg.split("_", 1)[1])
        except Exception:
            referred_by = None

    async with async_session_factory() as session:
        user = await get_user(session, telegram_id)
        is_new = user is None
        if is_new:
            # generate referral code for this user
            ref_code = generate_referral_code(telegram_id)
            user = await create_user(
                session,
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                referred_by=referred_by,
                referral_code=ref_code,
                initial_credits=config.initial_credits,
            )
        # check membership
        member = await ensure_membership(bot, config, telegram_id)
        if not member:
            # store membership = False
            await set_membership(session, user, False)
            await message.answer(
                "Чтобы пользоваться ботом, подпишись на наш канал и нажми «Проверить подписку».",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Подписаться",
                                url=f"https://t.me/{config.telegram_channel_id.lstrip('@')}"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="Проверить подписку",
                                callback_data="check_subscription",
                            )
                        ],
                    ]
                ),
            )
        else:
            # user is a member
            if not user.is_member:
                await set_membership(session, user, True)
                # grant referral bonus if applicable
                await grant_referral_bonus(session, user, config.referral_bonus, config.referral_bonus)
            # greet
            # Приветственное сообщение и основное меню. Пользователь может выбрать кнопку
            # либо просто написать сообщение в чат, и Валера его проанализирует.
            menu_kb = [
                [InlineKeyboardButton(text="Разобрать переписку", callback_data="start_chat")],
                [InlineKeyboardButton(text="Анализ профиля девушки", callback_data="girl_profile")],
                [InlineKeyboardButton(text="Анализ моего профиля", callback_data="my_profile")],
                [InlineKeyboardButton(text="Неловкие паузы", callback_data="awkward_pauses")],
                [InlineKeyboardButton(text="Мой баланс", callback_data="show_balance")],
                [InlineKeyboardButton(text="Пополнить баланс", callback_data="buy_credits")],
                [InlineKeyboardButton(text="Реферальная ссылка", callback_data="show_referral")],
            ]
            await message.answer(
                f"Привет, {first_name or 'друг'}! Я Валера, твой wingman.\n"
                "Я могу проанализировать переписку, анкету, предложить темы для разговора или просто поболтать.\n"
                "Выбери пункт меню ниже или напиши, в чём нужна помощь:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=menu_kb),
            )


async def callback_handler(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    config: Config,
) -> None:
    """Handle button callbacks from the inline keyboard."""
    data = callback.data
    user_id = callback.from_user.id
    # membership check for any command except subscription check
    if data != "check_subscription":
        is_member = await ensure_membership(bot, config, user_id)
        if not is_member:
            await callback.answer("Нужно подписаться на канал", show_alert=True)
            return

    if data == "check_subscription":
        # check membership when user clicks the button
        is_member = await ensure_membership(bot, config, user_id)
        async with async_session_factory() as session:
            user = await get_user(session, user_id)
            if is_member:
                await set_membership(session, user, True)
                # grant referral bonus if applicable
                await grant_referral_bonus(session, user, config.referral_bonus, config.referral_bonus)
                await callback.message.edit_text(
                    "Спасибо за подписку! Теперь ты можешь пользоваться ботом.",
                    reply_markup=None,
                )
            else:
                await callback.answer("Похоже, ты ещё не подписан", show_alert=True)
    elif data == "start_chat":
        # Prompt the user to send the conversation.  Keep the tone friendly and clear.
        await callback.message.answer(
            "Ок! Пришли переписку — текстом или скринами. Я помогу понять, как она к тебе относится, и предложу лучшие ответы."
        )
        await state.set_state(Form.chat_waiting_input)
        await callback.answer()
    elif data == "girl_profile":
        # Пользователь хочет проанализировать анкету девушки
        await callback.message.answer(
            "Пришли анкету девушки: текст, фото или скрин. Я расскажу, какая она, чем увлекается и как лучше завести разговор."
        )
        await state.set_state(Form.girl_profile_waiting_input)
        await callback.answer()
    elif data == "my_profile":
        # Пользователь хочет проанализировать свою анкету
        await callback.message.answer(
            "Давай посмотрим на твой профиль. Пришли текст, фото или скрины, и я скажу, что супер, а что можно подтянуть."
        )
        await state.set_state(Form.my_profile_waiting_input)
        await callback.answer()
    elif data == "awkward_pauses":
        # Пользователь хочет закрыть неловкую паузу
        await callback.message.answer(
            "Опиши, где вы сейчас (чат или свидание) и что обсуждали. Я подкину темы, чтобы заполнить паузу и поддержать вайб."
        )
        await state.set_state(Form.pause_waiting_input)
        await callback.answer()
    elif data == "show_balance":
        # Показываем баланс токенов. Ссылку на рефералку выводим в отдельном разделе.
        async with async_session_factory() as session:
            user = await get_user(session, user_id)
            # Обновим реферальный код при необходимости, но не показываем ссылку здесь
            ref_code = user.referral_code or generate_referral_code(user_id)
            if not user.referral_code:
                user.referral_code = ref_code
                await session.commit()
            text = (
                f"\U0001F4B0 Твой баланс: {user.credits} токен(ов).\n"
                "1 токен = 1 ответ Валеры.\n"
                f"Пригласи друга и вы оба получите +{config.referral_bonus} токенов!\n"
                "Чтобы узнать свою персональную ссылку, перейди в раздел ‘Реферальная ссылка’.\n\n"
                "Чтобы продолжить общение, пополни баланс или пригласи друга."
            )
            await callback.message.answer(text)
            await callback.answer()
    elif data == "buy_credits":
        # show packages
        kb = [
            [
                InlineKeyboardButton(
                    text=f"{credits} токенов — {amount}⭐", callback_data=f"buy_{slug}"
                )
            ]
            for slug, (credits, amount, _desc) in config.pricing.items()
        ]
        kb.append([InlineKeyboardButton(text="Назад", callback_data="back_main")])
        await callback.message.answer(
            "Выбери пакет для пополнения:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
        await callback.answer()
    elif data and data.startswith("buy_"):
        slug = data.split("_", 1)[1]
        if slug not in config.pricing:
            await callback.answer("Неизвестный пакет", show_alert=True)
            return
        credits, amount, description = config.pricing[slug]
        prices = [LabeledPrice(label=description, amount=amount)]
        payload = slug
        title = description
        await bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=f"Пополнение баланса: {description}",
            payload=payload,
            provider_token=config.provider_token,
            currency=config.currency,
            prices=prices,
            # For digital goods we don't need to supply start_parameter
            need_name=False,
            need_email=False,
            need_phone_number=False,
        )
        await callback.answer()
    elif data == "back_main":
        # Показать главное меню. Предложите выбрать пункт или задать вопрос прямо текстом.
        menu_kb = [
            [InlineKeyboardButton(text="Разобрать переписку", callback_data="start_chat")],
            [InlineKeyboardButton(text="Анализ профиля девушки", callback_data="girl_profile")],
            [InlineKeyboardButton(text="Анализ моего профиля", callback_data="my_profile")],
            [InlineKeyboardButton(text="Неловкие паузы", callback_data="awkward_pauses")],
            [InlineKeyboardButton(text="Мой баланс", callback_data="show_balance")],
            [InlineKeyboardButton(text="Пополнить баланс", callback_data="buy_credits")],
            [InlineKeyboardButton(text="Реферальная ссылка", callback_data="show_referral")],
        ]
        await callback.message.answer(
            "Главное меню. Выбери кнопку или просто напиши мне, в чём нужна помощь:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=menu_kb),
        )
        await callback.answer()

    elif data == "show_referral":
        # Отображаем реферальную ссылку пользователя и условия бонуса
        async with async_session_factory() as session:
            user = await get_user(session, user_id)
            ref_code = user.referral_code or generate_referral_code(user_id)
            if not user.referral_code:
                user.referral_code = ref_code
                await session.commit()
            link = f"https://t.me/{(await bot.get_me()).username}?start=ref_{user_id}"
            text = (
                f"\U0001F517 Твоя персональная реферальная ссылка:\n{link}\n\n"
                f"Пригласи друга и вы оба получите +{config.referral_bonus} токенов!"
            )
            await callback.message.answer(text)
            await callback.answer()


async def handle_pre_checkout(query: PreCheckoutQuery, bot: Bot, config: Config) -> None:
    """Answer the pre-checkout query; always approve payment."""
    await bot.answer_pre_checkout_query(query.id, ok=True)


async def handle_successful_payment(
    message: Message,
    bot: Bot,
    config: Config,
) -> None:
    """Update credits after payment is successful."""
    user_id = message.from_user.id
    invoice_payload = message.successful_payment.invoice_payload
    slug = invoice_payload  # we used slug as payload
    # Determine credits from config
    pricing = config.pricing.get(slug)
    if not pricing:
        await message.answer("Произошла ошибка: неизвестный товар")
        return
    credits, amount, description = pricing
    async with async_session_factory() as session:
        user = await get_user(session, user_id)
        await add_credits(session, user, credits)
    await message.answer(f"Оплата прошла успешно! Начислено {credits} токенов.")


async def handle_chat_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    openai_client: OpenAIClient,
) -> None:
    """Handle input for chat analysis state."""
    user_id = message.from_user.id
    # Cancel if not member
    is_member = await ensure_membership(bot, config, user_id)
    if not is_member:
        await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
        await state.clear()
        return
    # fetch user
    async with async_session_factory() as session:
        user = await get_user(session, user_id)
        if not user:
            await message.answer("Не удалось найти пользователя. Введите /start.")
            await state.clear()
            return
        if user.credits <= 0:
            await message.answer(
                "У тебя закончились токены. Пригласи друга или пополни баланс."
            )
            await state.clear()
            return
        # Gather text from message or photos
        user_text_parts: List[str] = []
        encoded_images: List[str] = []
        if message.text:
            user_text_parts.append(message.text)
        if message.photo:
            for photo in message.photo:
                file = await bot.get_file(photo.file_id)
                downloaded = await bot.download_file(file.file_path)
                b = downloaded.read()
                encoded = base64.b64encode(b).decode()
                encoded_images.append(encoded)
                # Append a placeholder for logging
                user_text_parts.append("[изображение]")
        combined = "\n".join(user_text_parts)
        # Log the user's message in the history
        await log_message(session, user_id, "user", combined)
        # Build messages for the AI; include images when available
        if encoded_images:
            content = []
            # Text part
            content.append({"type": "text", "text": "Я отправлю тебе переписку с девушкой, помоги мне её проанализировать.\n\nПереписка:\n" + combined})
            # Add each image
            for img in encoded_images:
                content.append({"type": "image_url", "image_url": "data:image/jpeg;base64," + img})
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ]
        else:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Я отправлю тебе переписку с девушкой, помоги мне её проанализировать.\n\nПереписка:\n" + combined,
                },
            ]
        try:
            # Show typing indicator while waiting for AI response
            try:
                await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            except Exception:
                pass
            result = await openai_client.chat(messages)
        except Exception as exc:
            logger.error("OpenAI chat analysis failed: %s", exc)
            await message.answer("Что-то пошло не так, попробуй ещё раз через пару минут.")
            await state.clear()
            return
        # Deduct one token
        await deduct_credits(session, user, 1)
        # Log Valera's reply before sending
        await log_message(session, user_id, "valera", result)
        # Send plain text result
        await message.answer(result)
        await state.clear()


async def handle_profile_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    openai_client: OpenAIClient,
) -> None:
    """Handle input for profile analysis state."""
    user_id = message.from_user.id
    is_member = await ensure_membership(bot, config, user_id)
    if not is_member:
        await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
        await state.clear()
        return
    async with async_session_factory() as session:
        user = await get_user(session, user_id)
        if not user:
            await message.answer("Не удалось найти пользователя. Введите /start.")
            await state.clear()
            return
        if user.credits <= 0:
            await message.answer(
                "У тебя закончились токены. Пригласи друга или пополни баланс."
            )
            await state.clear()
            return
        # Extract text and images
        user_text_parts: List[str] = []
        encoded_images: List[str] = []
        if message.text:
            user_text_parts.append(message.text)
        if message.photo:
            for photo in message.photo:
                file = await bot.get_file(photo.file_id)
                downloaded = await bot.download_file(file.file_path)
                b = downloaded.read()
                encoded = base64.b64encode(b).decode()
                encoded_images.append(encoded)
                user_text_parts.append("[фото]")
        combined = "\n".join(user_text_parts)
        # Build profile messages; include images if available
        if encoded_images:
            content = []
            content.append({"type": "text", "text": "Я отправлю тебе свой профиль, подскажи что можно улучшить.\n\nДанные:\n" + combined})
            for img in encoded_images:
                content.append({"type": "image_url", "image_url": "data:image/jpeg;base64," + img})
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ]
        else:
            messages = build_profile_prompt(combined)
        try:
            # Show typing indicator while waiting for AI response
            try:
                await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            except Exception:
                pass
            result = await openai_client.chat(messages)
        except Exception as exc:
            logger.error("OpenAI profile analysis failed: %s", exc)
            await message.answer(
                "Упс! Сейчас не получилось получить ответ. Давай попробуем ещё раз через пару минут."
            )
            await state.clear()
            return
        # Deduct one token and log Valera's reply
        await deduct_credits(session, user, 1)
        await log_message(session, user_id, "valera", result)
        # Send plain text result without JSON formatting
        await message.answer(result)
        await state.clear()


async def handle_girl_profile_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    openai_client: OpenAIClient,
) -> None:
    """Handle input when analysing a girl's profile."""
    user_id = message.from_user.id
    # Check membership
    if not await ensure_membership(bot, config, user_id):
        await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
        await state.clear()
        return
    async with async_session_factory() as session:
        user = await get_user(session, user_id)
        if not user:
            await message.answer("Не удалось найти пользователя. Введите /start.")
            await state.clear()
            return
        if user.credits <= 0:
            await message.answer("У тебя закончились токены. Пригласи друга или пополни баланс.")
            await state.clear()
            return
        parts: List[str] = []
        encoded_images: List[str] = []
        if message.text:
            parts.append(message.text)
        if message.photo:
            for photo in message.photo:
                file = await bot.get_file(photo.file_id)
                downloaded = await bot.download_file(file.file_path)
                b = downloaded.read()
                encoded = base64.b64encode(b).decode()
                encoded_images.append(encoded)
                parts.append("[фото]")
        combined = "\n".join(parts)
        # Log the user's message
        await log_message(session, user_id, "user", combined)
        # Build messages; include images if present
        if encoded_images:
            content = []
            content.append({"type": "text", "text": "Я отправлю тебе профиль девушки, подскажи что к чему там.\n\nПрофиль:\n" + combined})
            for img in encoded_images:
                content.append({"type": "image_url", "image_url": "data:image/jpeg;base64," + img})
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ]
        else:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Я отправлю тебе профиль девушки, подскажи что к чему там.\n\nПрофиль:\n" + combined,
                },
            ]
        try:
            try:
                await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            except Exception:
                pass
            result = await openai_client.chat(messages)
        except Exception as exc:
            logger.error("OpenAI girl profile analysis failed: %s", exc)
            await message.answer("Что-то пошло не так, попробуй ещё раз через пару минут.")
            await state.clear()
            return
        await deduct_credits(session, user, 1)
        # Log Valera's reply
        await log_message(session, user_id, "valera", result)
        await message.answer(result)
        await state.clear()


async def handle_my_profile_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    openai_client: OpenAIClient,
) -> None:
    """Handle input when analysing the user's own profile."""
    user_id = message.from_user.id
    if not await ensure_membership(bot, config, user_id):
        await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
        await state.clear()
        return
    async with async_session_factory() as session:
        user = await get_user(session, user_id)
        if not user:
            await message.answer("Не удалось найти пользователя. Введите /start.")
            await state.clear()
            return
        if user.credits <= 0:
            await message.answer("У тебя закончились токены. Пригласи друга или пополни баланс.")
            await state.clear()
            return
        parts: List[str] = []
        encoded_images: List[str] = []
        if message.text:
            parts.append(message.text)
        if message.photo:
            for photo in message.photo:
                file = await bot.get_file(photo.file_id)
                downloaded = await bot.download_file(file.file_path)
                b = downloaded.read()
                encoded = base64.b64encode(b).decode()
                encoded_images.append(encoded)
                parts.append("[фото]")
        combined = "\n".join(parts)
        # Log the user's message
        await log_message(session, user_id, "user", combined)
        # Build messages; include images if present
        if encoded_images:
            content = []
            content.append({"type": "text", "text": "Я отправлю тебе свой профиль, подскажи что можно улучшить.\n\nПрофиль:\n" + combined})
            for img in encoded_images:
                content.append({"type": "image_url", "image_url": "data:image/jpeg;base64," + img})
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ]
        else:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Я отправлю тебе свой профиль, подскажи что можно улучшить.\n\nПрофиль:\n" + combined,
                },
            ]
        try:
            try:
                await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            except Exception:
                pass
            result = await openai_client.chat(messages)
        except Exception as exc:
            logger.error("OpenAI my profile analysis failed: %s", exc)
            await message.answer("Что-то пошло не так, попробуй ещё раз через пару минут.")
            await state.clear()
            return
        await deduct_credits(session, user, 1)
        # Log Valera's reply
        await log_message(session, user_id, "valera", result)
        await message.answer(result)
        await state.clear()


async def handle_pause_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    openai_client: OpenAIClient,
) -> None:
    """Handle input when the user needs topics for awkward pauses."""
    user_id = message.from_user.id
    if not await ensure_membership(bot, config, user_id):
        await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
        await state.clear()
        return
    async with async_session_factory() as session:
        user = await get_user(session, user_id)
        if not user:
            await message.answer("Не удалось найти пользователя. Введите /start.")
            await state.clear()
            return
        if user.credits <= 0:
            await message.answer("У тебя закончились токены. Пригласи друга или пополни баланс.")
            await state.clear()
            return
        parts: List[str] = []
        encoded_images: List[str] = []
        if message.text:
            parts.append(message.text)
        if message.photo:
            for photo in message.photo:
                file = await bot.get_file(photo.file_id)
                downloaded = await bot.download_file(file.file_path)
                b = downloaded.read()
                encoded = base64.b64encode(b).decode()
                encoded_images.append(encoded)
                parts.append("[фото]")
        combined = "\n".join(parts)
        # Log the user's message
        await log_message(session, user_id, "user", combined)
        # Build messages; include images if present
        if encoded_images:
            content = []
            content.append({"type": "text", "text": "Я общаюсь с девушкой и возникла неловкая пауза, подкинь какие-нибудь темы для беседы, чтобы её заполнить.\n\nКонтекст:\n" + combined})
            for img in encoded_images:
                content.append({"type": "image_url", "image_url": "data:image/jpeg;base64," + img})
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ]
        else:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Я общаюсь с девушкой и возникла неловкая пауза, подкинь какие-нибудь темы для беседы, чтобы её заполнить.\n\nКонтекст:\n" + combined,
                },
            ]
        try:
            try:
                await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            except Exception:
                pass
            result = await openai_client.chat(messages)
        except Exception as exc:
            logger.error("OpenAI pause analysis failed: %s", exc)
            await message.answer("Что-то пошло не так, попробуй ещё раз через пару минут.")
            await state.clear()
            return
        await deduct_credits(session, user, 1)
        # Log Valera's reply
        await log_message(session, user_id, "valera", result)
        await message.answer(result)
        await state.clear()


async def handle_free_chat(
    message: Message,
    bot: Bot,
    config: Config,
    openai_client: OpenAIClient,
    state: FSMContext,
) -> None:
    """Handle free chat when no specific state is active."""
    # Ignore commands
    if message.text and message.text.startswith('/'):
        return
    user_id = message.from_user.id
    if not await ensure_membership(bot, config, user_id):
        await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
        return
    async with async_session_factory() as session:
        user = await get_user(session, user_id)
        if not user:
            await message.answer("Не удалось найти пользователя. Введите /start.")
            return
        if user.credits <= 0:
            await message.answer("У тебя закончились токены. Пригласи друга или пополни баланс.")
            return
        parts: List[str] = []
        encoded_images: List[str] = []
        if message.text:
            parts.append(message.text)
        if message.photo:
            for photo in message.photo:
                file = await bot.get_file(photo.file_id)
                downloaded = await bot.download_file(file.file_path)
                b = downloaded.read()
                encoded = base64.b64encode(b).decode()
                encoded_images.append(encoded)
                parts.append("[фото]")
        combined = "\n".join(parts)
        # Log the user's message
        async with async_session_factory() as log_session:
            await log_message(log_session, user_id, "user", combined)
        # Build messages; include images if present
        if encoded_images:
            content = []
            content.append({"type": "text", "text": combined})
            for img in encoded_images:
                content.append({"type": "image_url", "image_url": "data:image/jpeg;base64," + img})
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ]
        else:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": combined},
            ]
        try:
            try:
                await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            except Exception:
                pass
            result = await openai_client.chat(messages)
        except Exception as exc:
            logger.error("OpenAI free chat failed: %s", exc)
            await message.answer("Что-то пошло не так, попробуй ещё раз через пару минут.")
            return
        await deduct_credits(session, user, 1)
        # Log Valera's reply
        async with async_session_factory() as log_session:
            await log_message(log_session, user_id, "valera", result)
        await message.answer(result)


async def setup_bot() -> None:
    config = Config()
    await init_db()
    bot = Bot(token=config.bot_token)
    dp = Dispatcher()
    router = Router()

    # Build OpenAI client
    openai_client = OpenAIClient(api_key=config.openai_api_key)

    # Register handlers. Define async wrapper functions to pass config, bot and other dependencies.
    # The aiogram dispatcher automatically awaits async handlers, so wrapper functions must also be async.

    async def start_handler(message: Message, state: FSMContext) -> None:
        """Wrapper for the /start command that injects bot and config."""
        await handle_start(message, state, bot, config)

    async def callback_query_handler(callback: CallbackQuery, state: FSMContext) -> None:
        """Wrapper for callback queries that injects bot and config."""
        await callback_handler(callback, state, bot, config)

    async def pre_checkout_handler(query: PreCheckoutQuery) -> None:
        """Wrapper for pre-checkout queries that injects bot and config."""
        await handle_pre_checkout(query, bot, config)

    async def successful_payment_handler(message: Message) -> None:
        """Wrapper for successful payment messages that injects bot and config."""
        await handle_successful_payment(message, bot, config)

    async def chat_input_handler(message: Message, state: FSMContext) -> None:
        """Wrapper for chat input state that injects bot, config and openai_client."""
        await handle_chat_input(message, state, bot, config, openai_client)

    async def girl_profile_input_handler(message: Message, state: FSMContext) -> None:
        """Wrapper for girl profile state."""
        await handle_girl_profile_input(message, state, bot, config, openai_client)

    async def my_profile_input_handler(message: Message, state: FSMContext) -> None:
        """Wrapper for user's own profile state."""
        await handle_my_profile_input(message, state, bot, config, openai_client)

    async def pause_input_handler(message: Message, state: FSMContext) -> None:
        """Wrapper for pause state."""
        await handle_pause_input(message, state, bot, config, openai_client)

    async def free_chat_wrapper(message: Message, state: FSMContext) -> None:
        """Wrapper for free chat messages outside any state."""
        await handle_free_chat(message, bot, config, openai_client, state)

    # Commands for side menu. Each command replicates the callback actions but is triggered via /command.
    async def start_chat_cmd(message: Message, state: FSMContext) -> None:
        # Membership check
        if not await ensure_membership(bot, config, message.from_user.id):
            await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
            return
        await message.answer(
            "Ок! Пришли переписку — текстом или скринами. Я помогу понять, как она к тебе относится, и предложу лучшие ответы."
        )
        await state.set_state(Form.chat_waiting_input)

    async def girl_profile_cmd(message: Message, state: FSMContext) -> None:
        if not await ensure_membership(bot, config, message.from_user.id):
            await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
            return
        await message.answer(
            "Пришли анкету девушки: текст, фото или скрин. Я расскажу, какая она, чем увлекается и как лучше завести разговор."
        )
        await state.set_state(Form.girl_profile_waiting_input)

    async def my_profile_cmd(message: Message, state: FSMContext) -> None:
        if not await ensure_membership(bot, config, message.from_user.id):
            await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
            return
        await message.answer(
            "Давай посмотрим на твой профиль. Пришли текст, фото или скрины, и я скажу, что супер, а что можно подтянуть."
        )
        await state.set_state(Form.my_profile_waiting_input)

    async def awkward_pauses_cmd(message: Message, state: FSMContext) -> None:
        if not await ensure_membership(bot, config, message.from_user.id):
            await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
            return
        await message.answer(
            "Опиши, где вы сейчас (чат или свидание) и что обсуждали. Я подкину темы, чтобы заполнить паузу и поддержать вайб."
        )
        await state.set_state(Form.pause_waiting_input)

    async def show_balance_cmd(message: Message) -> None:
        # Replicate balance display without referral link
        user_id = message.from_user.id
        if not await ensure_membership(bot, config, user_id):
            await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
            return
        async with async_session_factory() as session:
            user = await get_user(session, user_id)
            if not user:
                await message.answer("Не удалось найти пользователя. Введите /start.")
                return
            # ensure ref code exists but don't display link here
            if not user.referral_code:
                user.referral_code = generate_referral_code(user_id)
                await session.commit()
            await message.answer(
                f"\U0001F4B0 Твой баланс: {user.credits} токен(ов).\n"
                "1 токен = 1 ответ Валеры.\n"
                f"Пригласи друга и вы оба получите +{config.referral_bonus} токенов!\n"
                "Чтобы узнать свою персональную ссылку, перейди в раздел ‘Реферальная ссылка’.\n\n"
                "Чтобы продолжить общение, пополни баланс или пригласи друга."
            )

    async def buy_credits_cmd(message: Message) -> None:
        # Show packages
        if not await ensure_membership(bot, config, message.from_user.id):
            await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
            return
        kb = [
            [
                InlineKeyboardButton(
                    text=f"{credits} токенов — {amount}\u2B50",
                    callback_data=f"buy_{slug}",
                )
            ]
            for slug, (credits, amount, _desc) in config.pricing.items()
        ]
        kb.append([InlineKeyboardButton(text="Назад", callback_data="back_main")])
        await message.answer(
            "Выбери пакет для пополнения:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )

    async def show_referral_cmd(message: Message) -> None:
        # Show referral link
        user_id = message.from_user.id
        if not await ensure_membership(bot, config, user_id):
            await message.answer("Нужно подписаться на канал, чтобы использовать бота.")
            return
        async with async_session_factory() as session:
            user = await get_user(session, user_id)
            if not user:
                await message.answer("Не удалось найти пользователя. Введите /start.")
                return
            if not user.referral_code:
                user.referral_code = generate_referral_code(user_id)
                await session.commit()
            link = f"https://t.me/{(await bot.get_me()).username}?start=ref_{user_id}"
            await message.answer(
                f"\U0001F517 Твоя персональная реферальная ссылка:\n{link}\n\n"
                f"Пригласи друга и вы оба получите +{config.referral_bonus} токенов!"
            )

    # Register the wrapper handlers with appropriate filters.
    router.message.register(start_handler, Command(commands=["start"]))
    router.callback_query.register(callback_query_handler)
    router.pre_checkout_query.register(pre_checkout_handler)
    router.message.register(successful_payment_handler, F.successful_payment)
    # Chat analysis state
    router.message.register(chat_input_handler, StateFilter(Form.chat_waiting_input))
    # Girl profile analysis state
    router.message.register(girl_profile_input_handler, StateFilter(Form.girl_profile_waiting_input))
    # My profile analysis state
    router.message.register(my_profile_input_handler, StateFilter(Form.my_profile_waiting_input))
    # Pause topics state
    router.message.register(pause_input_handler, StateFilter(Form.pause_waiting_input))
    # Free chat (catch-all) should be registered last so it doesn't override other handlers
    router.message.register(free_chat_wrapper)

    # Register command handlers for side menu commands
    router.message.register(start_chat_cmd, Command(commands=["start_chat"]))
    router.message.register(girl_profile_cmd, Command(commands=["girl_profile"]))
    router.message.register(my_profile_cmd, Command(commands=["my_profile"]))
    router.message.register(awkward_pauses_cmd, Command(commands=["awkward_pauses"]))
    router.message.register(show_balance_cmd, Command(commands=["show_balance"]))
    router.message.register(buy_credits_cmd, Command(commands=["buy_credits"]))
    router.message.register(show_referral_cmd, Command(commands=["show_referral"]))

    dp.include_router(router)
    # Set bot commands so users can access a persistent side menu
    commands = [
        BotCommand(command="start_chat", description="Разобрать переписку"),
        BotCommand(command="girl_profile", description="Анализ профиля девушки"),
        BotCommand(command="my_profile", description="Анализ моего профиля"),
        BotCommand(command="awkward_pauses", description="Неловкие паузы"),
        BotCommand(command="show_balance", description="Мой баланс"),
        BotCommand(command="buy_credits", description="Пополнить баланс"),
        BotCommand(command="show_referral", description="Реферальная ссылка"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as exc:
        logger.warning("Failed to set bot commands: %s", exc)
    # Start polling
    await dp.start_polling(bot)


def main() -> None:
    asyncio.run(setup_bot())


if __name__ == "__main__":
    main()