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
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, FSInputFile, PhotoSize,
                           InputMediaPhoto, LabeledPrice, PreCheckoutQuery,
                           SuccessfulPayment, URLInputFile, InlineKeyboardMarkup,
                           InlineKeyboardButton)

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
)
from .openai_client import OpenAIClient
from .prompts import build_chat_prompt, build_profile_prompt
from .utils import generate_referral_code

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Form(StatesGroup):
    chat_waiting_input = State()
    profile_waiting_input = State()


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
    command: CommandObject = message.command
    arg = command.args if command else None
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
            await message.answer(
                f"Привет, {first_name or 'друг'}! Я Валера, твой wingman.\n"
                "Готов помочь оценить переписку или анкету. \n"
                "Используй кнопки ниже:",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Разобрать переписку", callback_data="start_chat")],
                        [InlineKeyboardButton(text="Анализ профиля", callback_data="start_profile")],
                        [InlineKeyboardButton(text="Мой баланс", callback_data="show_balance")],
                        [InlineKeyboardButton(text="Пополнить баланс", callback_data="buy_credits")],
                    ]
                ),
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
        await callback.message.answer("Отправь текст переписки или фотографии, которые нужно проанализировать. После получения я пришлю результат.")
        await state.set_state(Form.chat_waiting_input)
        await callback.answer()
    elif data == "start_profile":
        await callback.message.answer("Отправь свою анкету: фотографии и био. После получения я пришлю результат.")
        await state.set_state(Form.profile_waiting_input)
        await callback.answer()
    elif data == "show_balance":
        async with async_session_factory() as session:
            user = await get_user(session, user_id)
            ref_code = user.referral_code or generate_referral_code(user_id)
            # In case referral_code was None on creation
            if not user.referral_code:
                user.referral_code = ref_code
                await session.commit()
            link = f"https://t.me/{(await bot.get_me()).username}?start=ref_{user_id}"
            text = (
                f"У тебя {user.credits} генераций.\n"
                f"Твоя реферальная ссылка: {link}\n"
                f"За каждого приглашённого — +{config.referral_bonus} тебе и +{config.referral_bonus} другу"
            )
            await callback.message.answer(text)
            await callback.answer()
    elif data == "buy_credits":
        # show packages
        kb = [
            [
                InlineKeyboardButton(
                    text=f"{credits} ген. — {amount/100:.2f}⭐", callback_data=f"buy_{slug}"
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
        await callback.message.answer(
            "Главное меню:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Разобрать переписку", callback_data="start_chat")],
                    [InlineKeyboardButton(text="Анализ профиля", callback_data="start_profile")],
                    [InlineKeyboardButton(text="Мой баланс", callback_data="show_balance")],
                    [InlineKeyboardButton(text="Пополнить баланс", callback_data="buy_credits")],
                ]
            ),
        )
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
    await message.answer(f"Оплата прошла успешно! Начислено {credits} генераций.")


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
                "У тебя закончились генерации. Пригласи друга или пополни баланс через /buy."
            )
            await state.clear()
            return
        # Gather text from message or photos
        user_text_parts: List[str] = []
        if message.text:
            user_text_parts.append(message.text)
        # handle photos: download each and base64 encode
        if message.photo:
            for photo in message.photo:
                file = await bot.get_file(photo.file_id)
                file_path = file.file_path
                downloaded = await bot.download_file(file_path)
                b = downloaded.read()
                encoded = base64.b64encode(b).decode()
                # We embed as markdown for the model (not used here). We'll just append placeholder
                user_text_parts.append("[изображение]")
        combined = "\n".join(user_text_parts)
        # Build prompt and call OpenAI
        messages = build_chat_prompt(combined)
        try:
            result = await openai_client.chat(messages)
        except Exception as exc:
            logger.error("OpenAI chat failed: %s", exc)
            await message.answer("Не удалось получить ответ от AI. Попробуй позже.")
            await state.clear()
            return
        # Deduct credit
        await deduct_credits(session, user, 1)
        # Pretty print the JSON for readability
        formatted = json.dumps(result, ensure_ascii=False, indent=2)
        await message.answer(f"Результат анализа:\n<pre>{formatted}</pre>", parse_mode="HTML")
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
                "У тебя закончились генерации. Пригласи друга или пополни баланс через /buy."
            )
            await state.clear()
            return
        # Extract text and images
        user_text_parts: List[str] = []
        if message.text:
            user_text_parts.append(message.text)
        if message.photo:
            for photo in message.photo:
                file = await bot.get_file(photo.file_id)
                downloaded = await bot.download_file(file.file_path)
                b = downloaded.read()
                encoded = base64.b64encode(b).decode()
                user_text_parts.append("[фото]")
        combined = "\n".join(user_text_parts)
        messages = build_profile_prompt(combined)
        try:
            result = await openai_client.chat(messages)
        except Exception as exc:
            logger.error("OpenAI profile analysis failed: %s", exc)
            await message.answer("Не удалось получить ответ от AI. Попробуй позже.")
            await state.clear()
            return
        await deduct_credits(session, user, 1)
        formatted = json.dumps(result, ensure_ascii=False, indent=2)
        await message.answer(f"Результат анализа:\n<pre>{formatted}</pre>", parse_mode="HTML")
        await state.clear()


async def setup_bot() -> None:
    config = Config()
    await init_db()
    bot = Bot(token=config.bot_token)
    dp = Dispatcher()
    router = Router()

    # Build OpenAI client
    openai_client = OpenAIClient(api_key=config.openai_api_key)

    # Register handlers
    router.message.register(handle_start, Command(commands=["start"]))
    router.callback_query.register(callback_handler)
    router.pre_checkout_query.register(handle_pre_checkout)
    router.message.register(handle_successful_payment, F.successful_payment)
    # Chat state
    router.message.register(
        lambda m, s: handle_chat_input(m, s, bot, config, openai_client),
        StateFilter(Form.chat_waiting_input),
    )
    # Profile state
    router.message.register(
        lambda m, s: handle_profile_input(m, s, bot, config, openai_client),
        StateFilter(Form.profile_waiting_input),
    )

    dp.include_router(router)
    # Start polling
    await dp.start_polling(bot)


def main() -> None:
    asyncio.run(setup_bot())


if __name__ == "__main__":
    main()