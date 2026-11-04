"""With "Detect automatically", captions in the spoken language are not sent for a paid translation."""

from __future__ import annotations

import pytest

from vedit.captions import guess_language, translation_target

SAID = {
    "en": "This is the finished oak bowl, and it is ready for the table. We sand it with care so the grain shows.",
    "es": "Este es el cuenco de roble terminado y está listo para la mesa. Lo lijamos con cuidado para que se vea la veta.",
    "fr": "Voici le bol en chêne fini et il est prêt pour la table. Nous le ponçons avec soin pour que le grain soit visible.",
    "de": "Das ist die fertige Schale aus Eiche und sie ist bereit für den Tisch. Wir schleifen sie mit Sorgfalt.",
    "ja": "これは完成したオークのボウルで、テーブルに出す準備ができています。木目が見えるように丁寧に磨きます。",
    "zh": "这是完成的橡木碗，可以放在桌子上了。我们仔细打磨，让木纹显现出来。",
    "ko": "이것은 완성된 오크 그릇이고 식탁에 올릴 준비가 되었습니다. 결이 보이도록 정성껏 다듬습니다.",
    "ar": "هذا هو وعاء البلوط المنتهي وهو جاهز للطاولة. نقوم بصنفرته بعناية حتى تظهر العروق.",
    "hi": "यह तैयार ओक का कटोरा है और यह मेज़ के लिए तैयार है। हम इसे ध्यान से घिसते हैं।",
    "th": "นี่คือชามไม้โอ๊คที่เสร็จแล้วและพร้อมสำหรับวางบนโต๊ะ เราขัดอย่างระมัดระวัง",
}


@pytest.mark.parametrize("code", sorted(SAID))
def test_the_spoken_language_is_recognised(code: str) -> None:
    assert guess_language([SAID[code]]) == code


@pytest.mark.parametrize("text", ["", "OK.", "Hi Tom", "1 2 3 4 5 6 7 8"])
def test_too_little_to_tell_is_unsure(text: str) -> None:
    assert guess_language([text]) is None


AUTO = {"language": "auto"}


def test_english_captions_on_english_speech_are_not_translated() -> None:
    assert translation_target({"language": "en"}, AUTO, heard=[SAID["en"]]) is None
    assert translation_target({"language": "en-US"}, AUTO, heard=[SAID["en"]]) is None


def test_a_different_caption_language_is_still_translated() -> None:
    assert translation_target({"language": "es"}, AUTO, heard=[SAID["en"]]) == "es"
    assert translation_target({"language": "en"}, AUTO, heard=[SAID["ja"]]) == "en"


def test_when_unsure_the_captions_are_translated_as_before() -> None:
    assert translation_target({"language": "en"}, AUTO, heard=["OK."]) == "en"


def test_the_plan_transcript_is_read_when_no_cues_are_given() -> None:
    plan = {"analysis": {"transcript": [{"start": 0, "end": 3, "text": SAID["fr"]}]}}
    assert translation_target({"language": "fr"}, AUTO, plan=plan) is None
    assert translation_target({"language": "de"}, AUTO, plan=plan) == "de"


def test_an_explicit_spoken_language_still_decides() -> None:
    assert translation_target({"language": "pt"}, {"language": "pt-BR"}) is None
    assert translation_target({"language": "es"}, {"language": "en"}) == "es"
    assert translation_target({}, {"language": "en"}) is None
