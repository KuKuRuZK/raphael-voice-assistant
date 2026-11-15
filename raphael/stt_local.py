"""
Локальний Whisper на відеокарті: розпізнавання без інтернету й без денного ліміту.

Чому: з WAKE_ENGINE "whisper" кожна почута поруч фраза йде в розпізнавання, щоб
знайти в ній імʼя, і 2026-11-15 Groq вичерпав денні 2000 запитів. Далі був Google,
а він чув «під вейп» замість «під вайб».

Як: transformers + torch (ROCm) на RX 9070, whisper-large-v3-turbo у float16, ≈0,3 с
на фразу 2-4 с і ≈1,6 ГБ відеопамʼяті. Увага (sdpa) на ROCm під Windows падає з
hipErrorInvalidValue, тому "eager". На процесорі та сама модель думає 5,6 с, а small
плутає слова, тож без відеокарти локальний просто не вмикається і працює Groq.

Модель вантажиться у фоні після старту (≈10-20 с); поки не готова, transcribe()
повертає None, і черга йде далі (Groq, Google, Vosk).
"""
import logging
import threading
import time

from raphael import settings as cfg

log = logging.getLogger("Лін")

_lock = threading.Lock()          # одна фраза за раз: генерація на GPU не потокобезпечна
_whisper = {"model": None, "proc": None, "prompt": None, "failed": False, "loading": False}


def available() -> bool:
    return _whisper["model"] is not None


def load(prompt_text: str = "") -> bool:
    """Завантажити модель (блокує). → чи готова. Провал запамʼятовується до перезапуску."""
    if _whisper["model"] is not None:
        return True
    if _whisper["failed"] or not cfg.STT_LOCAL:
        return False
    with _lock:
        if _whisper["model"] is not None:
            return True
        _whisper["loading"] = True
        t = time.perf_counter()
        try:
            import torch
            if not torch.cuda.is_available():
                raise RuntimeError("відеокарти для torch нема (на процесорі надто повільно)")
            import transformers
            from transformers import WhisperForConditionalGeneration, WhisperProcessor
            transformers.logging.set_verbosity_error()      # інакше попередження на кожну фразу
            transformers.logging.disable_progress_bar()     # під pythonw stderr нема
            try:                                            # з кешу: без інтернету теж стартує
                proc, model = _from_pretrained(WhisperProcessor, WhisperForConditionalGeneration, torch, True)
            except OSError:                                 # ще не завантажена: ≈1,6 ГБ з Hugging Face
                proc, model = _from_pretrained(WhisperProcessor, WhisperForConditionalGeneration, torch, False)
            _whisper["prompt"] = (proc.get_prompt_ids(prompt_text, return_tensors="pt").to("cuda")
                                if prompt_text else None)
            # Прогрів: перша генерація на ROCm компілює ядра ≈12 с; хай це буде тут, у фоні,
            # а не на першій фразі Влада (перевірено 2026-11-15: 12,5 с, далі 0,3 с)
            import numpy as np
            feats = proc(np.zeros(16000, dtype=np.float32), sampling_rate=16000,
                         return_tensors="pt").input_features.to("cuda", torch.float16)
            kw = {"prompt_ids": _whisper["prompt"]} if _whisper["prompt"] is not None else {}
            with torch.inference_mode():
                model.generate(feats, language="uk", task="transcribe", max_new_tokens=8, **kw)
            _whisper["proc"], _whisper["model"] = proc, model
            log.info(f"Локальний Whisper готовий за {time.perf_counter() - t:.0f} с "
                     f"({torch.cuda.get_device_name(0)}, {torch.cuda.memory_allocated() / 2**30:.1f} ГБ)")
            return True
        except Exception as e:
            _whisper["failed"] = True
            log.warning(f"Локальний Whisper не запустився, лишаюсь на Groq: {e}")
            return False
        finally:
            _whisper["loading"] = False


def _from_pretrained(proc_cls, model_cls, torch, local_only: bool):
    proc = proc_cls.from_pretrained(cfg.STT_LOCAL_MODEL, local_files_only=local_only)
    model = model_cls.from_pretrained(cfg.STT_LOCAL_MODEL, torch_dtype=torch.float16, attn_implementation="eager",
                                      local_files_only=local_only).to("cuda").eval()
    return proc, model


def load_in_background(prompt_text: str = ""):
    if cfg.STT_LOCAL and not _whisper["failed"] and _whisper["model"] is None and not _whisper["loading"]:
        threading.Thread(target=load, args=(prompt_text,), daemon=True, name="stt-local").start()


def _strip_prompt(text: str, prompt_text: str) -> str:
    """transformers інколи повертає підказку разом із текстом: прибрати її."""
    text = (text or "").strip()
    if prompt_text and text.startswith(prompt_text.strip()):
        text = text[len(prompt_text.strip()):]
    return text.strip()


def transcribe(samples, prompt_text: str = ""):
    """samples: float32 моно 16 кГц. → текст ("" = нічого не сказано) або None (локальний
    не готовий чи впав: хай розпізнає наступний у черзі)."""
    if _whisper["model"] is None:
        return None
    try:
        import torch
        with _lock:
            proc, model = _whisper["proc"], _whisper["model"]
            feats = proc(samples, sampling_rate=16000, return_tensors="pt").input_features.to("cuda", torch.float16)
            kw = {"prompt_ids": _whisper["prompt"]} if _whisper["prompt"] is not None else {}
            with torch.inference_mode():
                ids = model.generate(feats, language="uk", task="transcribe", max_new_tokens=160, **kw)
            text = proc.batch_decode(ids, skip_special_tokens=True)[0]
        return _strip_prompt(text, prompt_text)
    except Exception as e:
        log.warning(f"Локальний Whisper: {e}")
        return None
