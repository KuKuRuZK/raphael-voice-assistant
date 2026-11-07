"""Стан Лін без старого вікна: тіло дізнається про нього, а vibes мовчить, поки вона зайнята."""


def test_state_goes_to_body_and_quiets_vibes(env, lin):
    from raphael import runtime, vibes
    sent = []
    env.patch("mind", lambda state, text="", *a, **k: sent.append((state, text)))
    env.patch("VIBE_ENABLED", True)

    runtime.show_state("thinking", "яка погода")
    assert runtime.LIN_STATE == "thinking"
    assert sent == [("thinking", "яка погода")]
    assert not vibes._can_talk()                 # Лін зайнята: без реплік від себе

    runtime.show_state("speaking", "Сонячно.")   # «говорю» тілу шле сама озвучка, з гучністю
    assert sent == [("thinking", "яка погода")]

    runtime.show_state("idle")
    assert runtime.LIN_STATE == "idle"
    assert sent[-1] == ("idle", "")
    assert vibes._can_talk()                     # вільна: знову може сказати щось від себе
