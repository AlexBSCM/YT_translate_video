"""Перевод названия видео на русский (deep-translator)."""
from utils import has_cyrillic


def _try_translator(engine, text):
    if engine == "google":
        from deep_translator import GoogleTranslator
        result = GoogleTranslator(source="auto", target="ru").translate(text)
    elif engine == "mymemory":
        # MyMemory не понимает source="auto" и короткие коды: только явный
        # язык полными именами (english -> russian).
        from deep_translator import MyMemoryTranslator
        result = MyMemoryTranslator(source="english", target="russian").translate(text)
    else:
        raise ValueError(f"Неизвестный переводчик: {engine}")
    return (result or "").strip()


def translate_to_ru(text):
    """Переводит текст на русский.

    Возвращает (перевод, ошибка): при отсутствии необходимости перевода
    или при успехе ошибка равна None; при неудаче перевод равен исходному
    тексту, а ошибка содержит причину.
    """
    if not text or has_cyrillic(text):
        return text, None
    errors = []
    for engine in ("google", "mymemory"):
        try:
            result = _try_translator(engine, text)
            if result and result.lower() != text.strip().lower():
                return result, None
        except Exception as e:
            errors.append(f"{engine}: {e}")
    return text, ("; ".join(errors) if errors else "переводчики недоступны")


def build_final_title(original):
    """Возвращает (итоговое название, ошибка перевода или None)."""
    original = (original or "").strip()
    if not original:
        return "video", None
    if has_cyrillic(original):
        return original, None
    translated, error = translate_to_ru(original)
    if not translated or translated.strip().lower() == original.strip().lower():
        return original, error
    return f"{translated} ({original})", None
