"""
SmAttaker — Templates Package
"""
import importlib

def get_template(language: str, key: str, **kwargs) -> str:
    """
    Get a formatted message template by language and key.
    Falls back to English if translation missing.
    """
    try:
        if language == "ar":
            module = importlib.import_module("backend.bot.templates.ar.messages")
        else:
            module = importlib.import_module("backend.bot.templates.en.messages")
    except ImportError:
        module = importlib.import_module("backend.bot.templates.en.messages")

    template = getattr(module, key, None)
    if template is None:
        # Fallback to English
        en_module = importlib.import_module("backend.bot.templates.en.messages")
        template = getattr(en_module, key, str(kwargs))

    if callable(template):
        return template(**kwargs)
    return str(template)
