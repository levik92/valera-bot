"""
Prompt templates used for the OpenAI calls.  They define the role of the
assistant and specify the output JSON structure.
"""

SYSTEM_PROMPT = (
    "Ты — Валера, эксперт по соблазнению и женской психологии. "
    "Твоя задача — помочь пользователю понять, насколько собеседница вовлечена, "
    "какие в переписке или анкете присутствуют красные или зелёные флажки, и "
    "как лучше построить общение, чтобы в конечном итоге соблазнить девушку. "
    "Отвечай кратко, уверенно и без воды, со здоровой иронией, но без токсичности. "
    "Всегда возвращай строго валидный JSON по заданной схеме без каких-либо других "
    "комментариев. Если ввод содержит изображения, сначала вытащи из них текст и контекст, "
    "затем ааизируй.""
)

# Chat analysis schema decription for the user prompt
CHAT_SCHEMA_DESCRIPTION = (
    "Верни JSON с ключами: \n"
    "interest_score — число 0–100; \n"
    "diagnosis — краткий текст, поясняющий ситуацию; \n"
    "signals.green, signals.yellow, signals.red — списки пунктов для зелёных, жёлтых и красных флажков; \n"
    "best_reply — один лучший ответ, который стоит отправить сейчас; \n"
    "next_step — какой ход сделать после её реакции; \n"
    "fallback_if_silent — что написать, если она молчит несколько часов.\n"
)

# Profile analysis schema description for the user prompt
PROFILE_SCHEMA_DESCRIPTION = (
    "Верни JSON с ключами: \n"
    "score.photos, score.bio, score.overall — оценки (0–10); \n"
    "impression — кратко, какое впечатление производит анкета; \n"
    "what_to_change — список объектов (photo_1, bio, стиль и т.д.) с полями reason и action; \n"
    "new_bio_variants — список из трёх вариантов нового био; \n"
    "first_message_hooks — список из трёх крючков для первого сообщения.\n"
)


def build_chat_prompt(user_text: str) -> list[dict]:
    """Construct messages for chat analysis.

    Args:
        user_text: The concatenated text of the conversation or OCR result from images.

    Returns:
        A list of messages suitable for openai.ChatCompletion
    """
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Проанализируй переписку и оцени её. "
                f"{CHAT_SCHEMA_DESCRIPTION} "
                "Сам текст переписки:\n" + user_text
            ),
        },
    ]


def build_profile_prompt(user_text: str) -> list[dict]:
    """Construct messages for profile analysis.

    Args:
        user_text: The concatenated text extracted from the user's profile (bio, info).

    Returns:
        A list of messages suitable for openai.ChatCompletion
    """
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Проанализируй анкету и фотографии. "
                f"{PROFILE_SCHEMA_DESCRIPTION} "
                "Данные пользователя:\n" + user_text
            ),
        },
    ]
