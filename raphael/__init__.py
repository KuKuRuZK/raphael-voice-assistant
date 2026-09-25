"""
Рафаель: голосовий асистент. Точка входу: lin.py у корені проєкту.

Карта пакета:
    settings      налаштування, секрети, config.json (у коді: cfg)
    runtime       спільний стан процесу: вікно, прапорець дії, маркер виходу
    tts, stt      озвучка і розпізнавання мови
    llm, prompt   моделі з резервом і системний промпт
    assistant     головний цикл, розбір команд, розмова з моделлю
    actions       виконання дій з тегів [ACTION:тип:параметр]
    hotkeys       гарячі клавіші, push-to-talk
    dictation     диктовка під курсор і у сховище
    notes         нотатки з нагадуваннями, памʼять між сесіями
    brain         другий мозок: сховище Obsidian
    music         Spotify (дозволи спільні з spotify_auth.py: spotify_common)
    gmail         пошта, сортування листів (mail_triage)
    agenda        Google Календар
    slack_chat    Slack
    pc            програми, вікна, процеси, буфер обміну, екран, вимкнення
    web           погода, валюти, тихий пошук
    claude_code   Claude Code і claude.ai
    sysmon        стан системи і журнал Windows
    timers        таймери і режим фокусу
    monitor       фонові сповіщення про пошту й Slack
    briefing      ранковий дайджест
    ui, orb       вікно, трей, анімований орб
    voice_rules   чистий розбір фраз (з тестами)
"""
