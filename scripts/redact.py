#!/usr/bin/env python3
"""Редакция перед облачным вызовом (ТЗ §8.3).

Два слоя, оба обязательны:
  1. регэкспы на секреты — детерминированно, ловят то, что реально опасно;
  2. Presidio — PII (телефон, почта, карта, IP).

⚠️ Имена людей НЕ вычищаем намеренно. §8.3 требует редакции PII, но §5 и §7
требуют, чтобы дистиллятор доставал людей и заводил карточки в entities/people/.
Вырезав PERSON, мы ломаем этап 4 целиком. Опасно не имя, а секрет; имена и так
уходят в облако, когда Сергей просто работает в Claude Code. Список сущностей
вынесен в PII_ENTITIES — если решение поменяется, правится одна строка.

Если Presidio не поставлен — это НЕ повод отправить как есть. require_chain()
падает, вызывающий держит задачу в очереди (слой 2 §8.4).
"""
import re, sys

# Слой 1. Порядок не важен, применяем все.
SECRETS = [
    (re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----", re.S), "<PRIVATE_KEY>"),
    (re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}"), "<API_KEY>"),          # OpenAI/Anthropic/OpenRouter
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "<GITHUB_TOKEN>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "<GITHUB_TOKEN>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "<SLACK_TOKEN>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<AWS_KEY_ID>"),
    (re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "<JWT>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer <TOKEN>"),
    # key = "значение" / password: значение — общий случай, ловит r2-ключи и пароли
    (re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?key(?:[_-]?id)?|secret(?:[_-]?access[_-]?key)?"
                r"|auth[_-]?token|password|passwd|client[_-]?secret)\s*[:=]\s*)"
                r"[\"']?([A-Za-z0-9._~+/=-]{12,})[\"']?"), r"\1<REDACTED>"),
]

# Слой 2. PERSON, LOCATION, DATE_TIME, NRP, ORG — сознательно не в списке: см. шапку.
PII_ENTITIES = ["EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE",
                "IP_ADDRESS", "CRYPTO", "US_SSN", "MEDICAL_LICENSE"]

_engine = None

def require_chain():
    """Готов ли полный слой §8.3. Падает — значит в облако ничего не уходит."""
    global _engine
    if _engine is None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        # Русский обязателен: волт русскоязычный, английская модель на кириллице
        # не находит ничего и молча пропускает всё.
        nlp = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "ru", "model_name": "ru_core_news_sm"},
                       {"lang_code": "en", "model_name": "en_core_web_sm"}],
        }).create_engine()
        _engine = AnalyzerEngine(nlp_engine=nlp, supported_languages=["ru", "en"])
        # Из коробки телефоны распознаются как US: +7 916 123-45-67 проходит
        # насквозь. Подменяем распознаватель на региональный.
        from presidio_analyzer.predefined_recognizers import PhoneRecognizer
        _engine.registry.remove_recognizer("PhoneRecognizer")   # один раз: снимает все
        for lang in ("ru", "en"):
            _engine.registry.add_recognizer(
                PhoneRecognizer(supported_language=lang, supported_regions=("RU", "US")))
    return _engine

def redact(text, lang="ru"):
    """→ (текст, {что: сколько). Слой 1 всегда, слой 2 требует Presidio."""
    stats = {}
    for rx, repl in SECRETS:
        text, n = rx.subn(repl, text)
        if n: stats[repl.strip("<>").split()[-1] if repl.startswith("<") else "SECRET_ASSIGN"] = \
                  stats.get(repl.strip("<>"), 0) + n
    engine = require_chain()
    # Presidio режет длинный вход по памяти spacy: идём кусками по абзацам.
    out = []
    for chunk in _chunks(text, 40000):
        # 0.4, а не 0.5: телефон через phonenumbers приходит ровно с 0.4, и на
        # пороге 0.5 российские номера уезжали в облако целиком. Ложное
        # срабатывание здесь стоит одного затёртого числа, пропуск — номера.
        res = [r for r in engine.analyze(text=chunk, language=lang, entities=PII_ENTITIES)
               if r.score >= 0.4]
        for r in sorted(res, key=lambda r: r.start, reverse=True):
            chunk = chunk[:r.start] + "<%s>" % r.entity_type + chunk[r.end:]
            stats[r.entity_type] = stats.get(r.entity_type, 0) + 1
        out.append(chunk)
    return "".join(out), stats

def _chunks(s, n):
    while s:
        if len(s) <= n: yield s; return
        cut = s.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        yield s[:cut]; s = s[cut:]

def self_check():
    dirty = (
        "ключ sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF лежал в конфиге\n"
        "GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\n"
        'secret_access_key = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"\n'
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijkl\n"
        "Иван Петров написал на i.petrov@example-corp.ru, телефон +7 916 123-45-67\n"
    )
    # Слой 1 проверяем в отрыве: он обязан работать даже без Presidio.
    only1 = dirty
    for rx, repl in SECRETS: only1 = rx.sub(repl, only1)
    for leak in ("sk-ant-api03-AAAA", "ghp_ABCDEF", "9f8e7d6c5b4a", "MIIEow", "eyJhbGciOi"):
        assert leak not in only1, leak
    assert "Иван Петров" in only1        # имена не трогаем: §5, см. шапку

    try: require_chain()
    except Exception as e:
        print("redact self-check: слой 1 ок, Presidio НЕ готов (%s) — в облако нельзя" % type(e).__name__)
        return 1
    clean, stats = redact(dirty)
    assert "i.petrov@example-corp.ru" not in clean, clean
    assert "916 123-45-67" not in clean, clean
    assert "Иван Петров" in clean
    for leak in ("sk-ant-api03-AAAA", "ghp_ABCDEF", "MIIEow"): assert leak not in clean, leak
    print("redact self-check ok:", stats)
    return 0

if __name__ == "__main__":
    if "--self-check" in sys.argv: sys.exit(self_check())
    t, st = redact(sys.stdin.read())
    sys.stdout.write(t); print("\n--- вычищено:", st, file=sys.stderr)
