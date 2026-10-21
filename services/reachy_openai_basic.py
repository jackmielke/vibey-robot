"""The "basic" voice brain: OpenAI Realtime with everything optional removed.

Same engine, model and voice as reachy_openai_realtime, but no tools, no
memory/context in the prompt and no background nudges — just voice in, voice
out. The fallback for when the full kit is getting in its own way.
"""
import reachy_openai_realtime as _rt


def run(*args, **kwargs):
    _rt.BASIC["on"] = True
    try:
        return _rt.run(*args, **kwargs)
    finally:
        _rt.BASIC["on"] = False
