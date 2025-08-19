"""
Wrapper around the OpenAI API to perform chat completions with retries.

This module abstracts the details of calling the OpenAI API and parsing the
response.  It performs a basic retry strategy and attempts to repair
unparseable JSON by asking the model to fix its output.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, List

import openai


class OpenAIClient:
    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        openai.api_key = api_key
        self.model = model

    async def chat(self, messages: List[dict], max_retries: int = 2, timeout: int = 30) -> Any:
        """Send a chat completion request and parse the JSON response.

        Args:
            messages: List of chat messages.
            max_retries: Number of attempts to fix invalid JSON responses.
            timeout: Timeout for the network request in seconds.

        Returns:
            Parsed JSON object.

        Raises:
            ValueError: If the model fails to return valid JSON after retries.
        """
        attempt = 0
        content = None
        while attempt <= max_retries:
            try:
                resp = await openai.ChatCompletion.acreate(
                    model=self.model,
                    messages=messages,
                    temperature=0.5,
                    response_format={"type": "json_object"},
                    timeout=timeout,
                )
                content = resp.choices[0].message.content
                return json.loads(content)
            except Exception as exc:
                attempt += 1
                # attempt to repair JSON
                if attempt > max_retries:
                    raise ValueError(f"Failed to parse OpenAI response: {exc}\nRaw content: {content}") from exc
                # add a repair instruction to the messages and retry
                repair_prompt = (
                    "Ответ, который ты вернул, не является валидным JSON. "
                    "Пожалуйста, исправь формат и верни только JSON."
                )
                messages.append({"role": "assistant", "content": content or ""})
                messages.append({"role": "user", "content": repair_prompt})
                await asyncio.sleep(1)
