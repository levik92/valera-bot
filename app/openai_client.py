"""
Wrapper around the OpenAI API (>=1.40) to perform chat completions, including vision.
"""
from __future__ import annotations

import asyncio
from typing import Any, List, Dict, Union

from openai import OpenAI

class OpenAIClient:
    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        self.client = OpenAI(api_key=api_key)
        self.model = model

    async def acomplete(self, messages: List[Dict[str, Any]], temperature: float = 0.5, timeout: float = 60.0) -> str:
        """
        Call the chat.completions API (supports text+image messages on vision models).
        """
        loop = asyncio.get_event_loop()
        def _call():
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        return await loop.run_in_executor(None, _call)

    def build_user_content(self, text: str | None = None, image_b64: str | None = None) -> Dict[str, Any]:
        """
        Helper to build a user message content array with optional base64 image.
        """
        if image_b64:
            return {
                "role": "user",
                "content": [
                    {"type": "text", "text": text or "Проанализируй это изображение и помоги с перепиской."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }
        else:
            return {"role": "user", "content": text or ""}
