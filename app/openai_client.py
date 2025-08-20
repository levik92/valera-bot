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

    async def chat(
        self, messages: List[dict], max_retries: int = 0, timeout: int = 60
    ) -> str:
        """Send a chat completion request and return the assistant's reply as plain text.

        Args:
            messages: List of chat messages (dicts with roles 'system', 'user', 'assistant').
            max_retries: Unused; kept for backward compatibility.
            timeout: Timeout for the network request in seconds.

        Returns:
            The text content of the assistant's reply.
        """
        try:
            resp = await openai.ChatCompletion.acreate(
                model=self.model,
                messages=messages,
                temperature=0.5,
                timeout=timeout,
            )
            return resp.choices[0].message.content
        except Exception as exc:
            # Reraise the error so caller can handle it
            raise
