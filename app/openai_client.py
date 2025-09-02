"""
Wrapper around the OpenAI API (>=1.40) to perform chat completions with retries.
Supports text+image messages on vision models like gpt-4o.
"""
from __future__ import annotations

import asyncio
from typing import Any, List, Dict

from openai import OpenAI

class OpenAIClient:
    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        self.client = OpenAI(api_key=api_key)
        self.model = model


    
    async def chat(self, messages: List[Dict[str, Any]], temperature: float = 0.5, timeout: float = 60.0) -> str:
        loop = asyncio.get_event_loop()
        def _call():
                    # Ensure all image_url entries include 'detail': 'auto'
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "image_url":
                        image_data = item.get("image_url")
                        if isinstance(image_data, str):
                            item["image_url"] = {"url": image_data, "detail": "auto"}
                        elif isinstance(image_data, dict) and "detail" not in image_data:
                            image_data["detail"] = "auto"

            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        return await loop.run_in_executor(None, _call)

    def build_user_content(self, text: str | None = None, image_b64: str | None = None) -> Dict[str, Any]:
        if image_b64:
            return {
                "role": "user",
                "content": [
                    {"type": "text", "text": text or "Проанализируй изображение в контексте переписки."},
                {"type": "image_url",
 "image_url": {
     "url": f"data:image/jpeg;base64,{image_b64}",
     "detail": "auto"
 }},
                ],
            }
        return {"role": "user", "content": text or ""}
