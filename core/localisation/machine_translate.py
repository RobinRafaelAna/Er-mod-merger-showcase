"""
machine_translate.py — English -> Finnish draft translation via argostranslate.

argostranslate is fully offline (downloads a translation model once, no API
key, no per-request network call, no rate limits or ongoing cost) - same
"free, no account needed" philosophy as edge-tts elsewhere in this app.
Quality tested directly against real game lines this session: grammatically
correct and natural-sounding, though as with any MT it can lose nuance/tone,
so translated lines are tracked as pending review (see mt_review_status.py)
rather than treated as finished.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

_translation = None  # lazily loaded — argostranslate.translate.translate() handle

GLOSSARY_FILENAME = "translation_glossary.csv"

# Tags like <PLAYER_NAME> and parentheticals are masked because
# argostranslate mangles/mistranslates them in place (confirmed directly:
# "<PLAYER_NAME>" loses its underscore, parentheticals get folded into the
# sentence or dropped). Swap them out for a token the model tends to pass
# through untouched, then restore afterward. Stage-direction asides like
# "(laughs)" restore verbatim; parentheticals with real content — item-
# description effect text like "(increases by 10% for 30 seconds)" — are
# translated *separately* and restore to the translation, so they no longer
# stay English (they used to). The same masking mechanism implements the
# user glossary: a matched glossary term's placeholder restores to the
# *target* text instead of the original.
# Token format: bare "XQ<n>" — bracketed tokens like "[PH0]" get dropped
# mid-sentence by the model far more often (tested: 1/3 vs 3/3 survival).
_PROTECTED_RE = re.compile(r"<[^>]*>|\([^)]*\)")


def _is_aside(inner: str) -> bool:
    """Stage direction ("(laughs)", "(heavy breathing)") vs real content
    ("(Increases stamina recovery)", "(FP 10)"): short, digit-free,
    lowercase-leading parentheticals are asides and stay verbatim."""
    words = inner.split()
    return (len(words) <= 2
            and not any(c.isdigit() for c in inner)
            and inner.lstrip()[:1].islower())
# The model writes Finnish case endings onto the token acronym-style
# ("XQ0:n", sometimes "XQ0: lle") — capture *known case endings only*, so a
# genuine colon followed by a real word is never swallowed into the name.
_CASE_ENDINGS = (
    "n|t|a|ä|ta|tä|tta|ttä|an|en|in|lle|lla|llä|lta|ltä|ssa|ssä|sta|stä|"
    "ksi|na|nä|seen|een|iin|hin|hun|hyn"
)
_TOKEN_RE = re.compile(rf"XQ(\d+)(:\s?(?:{_CASE_ENDINGS})\b)?")


# ---------------------------------------------------------------------- #
# Finnish inflection helpers — attach model-produced case endings to
# glossary replacements grammatically instead of gluing them onto the
# base form ("Rajahauta" + "ssa" -> "Rajahaudassa", not "Rajahautassa").
# Rule-based on purpose: covers the regular common cases; anything odd is
# still caught in pending review like all MT output.
# ---------------------------------------------------------------------- #

# Case endings that trigger the weak consonant grade in Finnish (genitive,
# nominative plural, inessive, elative, adessive, ablative, allative,
# translative). Partitive/essive/illative endings take the strong stem.
_WEAK_GRADE_ENDINGS = {
    "n", "t", "ssa", "ssä", "sta", "stä", "lla", "llä", "lta", "ltä", "lle", "ksi",
}
# Rightmost-match consonant gradation pairs, applied to the cluster just
# before the final vowel. Single k between vowels (VkV: joki->joen) is
# deliberately skipped — foreign names keep their k ("Marika" must stay
# "Marikan", not "Marian") — but the unambiguous k-clusters are handled:
# rk->r ("Turku" -> "Turussa"), lk->l ("jalka" -> "jalalla"), and the
# u_u/y_y single-k -> v special case is done separately in
# _fi_attach_ending ("puku" -> "puvun").
_GRADATION_PAIRS = [
    ("kk", "k"), ("pp", "p"), ("tt", "t"),
    ("nk", "ng"), ("mp", "mm"), ("nt", "nn"),
    ("lt", "ll"), ("rt", "rr"), ("ht", "hd"),
    ("rk", "r"), ("lk", "l"),
    ("t", "d"), ("p", "v"),
]
_HARMONY_SWAP = str.maketrans("aou", "äöy")
# Illative endings the model may pick ("XQ0:een") — the regular illative of
# a vowel-final word is its own final vowel lengthened + n ("Rajahautaan").
_ILLATIVE_ENDINGS = {
    "een", "aan", "iin", "oon", "uun", "yyn", "ään", "öön", "han", "hän", "seen", "siin",
}


def _fi_attach_ending(word: str, ending: str) -> str:
    """Attach a Finnish case ending to (the last word of) a glossary
    replacement: weak-grade gradation when the ending calls for it, vowel
    harmony fixed to match the word, triple vowels collapsed at the seam."""
    head, sep, last = word.rpartition(" ")
    lower = last.lower()

    # Consonant-final words need a vowel stem before most endings:
    # -nen -> -se ("Kalanen" -> "Kalasen"), -us/-ys -> -ukse/-ykse
    # ("Virtaus" -> "Virtauksen"), other consonant-final (foreign names)
    # get the standard epenthetic -i- ("Gostoc" -> "Gostocin").
    # The partitive attaches to the consonant stem instead ("virtausta").
    partitive = ending in ("ta", "tä")
    if lower and lower[-1] not in "aäoöuyie":
        caps = last.isupper() and len(last) > 1
        if lower.endswith("nen"):
            stem_add = "S" if caps else "s"
            last = last[:-3] + (stem_add if partitive else stem_add + ("E" if caps else "e"))
        elif lower.endswith(("us", "ys")) and len(lower) >= 3:
            if not partitive:
                last = last[:-1] + ("KSE" if caps else "kse")
        elif not partitive:
            last = last + ("I" if caps else "i")
        lower = last.lower()

    if ending in _ILLATIVE_ENDINGS and lower and lower[-1] in "aäoöuyie":
        ending = lower[-1] + "n"

    # vowel harmony: pick the ending variant matching the word's last
    # back/front vowel (e/i alone -> front)
    back = ""
    for ch in reversed(lower):
        if ch in "aou":
            back = "back"
            break
        if ch in "äöy":
            back = "front"
            break
    if back == "back":
        ending = ending.translate(str.maketrans("äöy", "aou"))
    elif back == "front":
        ending = ending.translate(_HARMONY_SWAP)

    if ending in _WEAK_GRADE_ENDINGS and len(lower) >= 3 and lower[-1] in "aäoöuyie":
        stem, final_vowel = last[:-1], last[-1]
        # single k between identical close round vowels weakens to v
        # ("puku" -> "puvun", "kyky" -> "kyvyn") — checked before the
        # cluster pairs so "uk" isn't misread as a cluster
        low = stem.lower()
        if (len(low) >= 2 and low[-1] == "k"
                and ((low[-2] == "u" and final_vowel.lower() == "u")
                     or (low[-2] == "y" and final_vowel.lower() == "y"))):
            v = "V" if stem[-1].isupper() else "v"
            stem = stem[:-1] + v
            last = stem + final_vowel
        else:
            for strong, weak in _GRADATION_PAIRS:
                if stem.lower().endswith(strong):
                    cluster = stem[len(stem) - len(strong):]
                    if cluster.isupper():
                        weak = weak.upper()  # keep ALL-CAPS terms ALL-CAPS ("MAHDIN")
                    stem = stem[: len(stem) - len(strong)] + weak
                    break
            last = stem + final_vowel

    if last.isupper() and len(last) > 1:
        ending = ending.upper()
    joined = last + ending
    # collapse a triple vowel at the seam ("linnake" + "een" -> "linnakeen")
    if len(ending) >= 2 and ending[0] == ending[1] and joined[-len(ending) - 1].lower() == ending[0]:
        joined = last + ending[1:]
    return (head + sep if sep else "") + joined


def _fi_genitive(word: str) -> str:
    return _fi_attach_ending(word, "n")


def _expand_glossary(glossary: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Auto-derive English possessives: for each (X, Y) add ("X's", genitive
    of Y) in both apostrophe styles, unless the user defined their own row.
    Case variants ("ELDEN", "elden") don't need rows — matching is
    case-insensitive and the replacement's capitalization follows the
    matched occurrence (see _match_case)."""
    defined = {src.lower() for src, _ in glossary}
    out = list(glossary)
    for src, dst in list(glossary):
        if src.endswith(("'s", "’s")):
            continue  # already a possessive — don't derive "X's's"
        # a user-defined possessive in either apostrophe style covers both
        user_poss = next(
            (d for s, d in glossary if s.lower() in (f"{src.lower()}'s", f"{src.lower()}’s")), None,
        )
        dst_poss = user_poss if user_poss is not None else _fi_genitive(dst)
        for apo in ("'", "’"):
            poss = f"{src}{apo}s"
            if poss.lower() not in defined:
                out.append((poss, dst_poss))
    return out


def _match_case(occurrence: str, replacement: str) -> str:
    """Shape the replacement's capitalization after the matched text:
    "TORRENT" -> "VIRTAUS", "Torrent" -> "Virtaus", "torrent" -> "virtaus".
    Only the first letter is lowered for a lowercase occurrence, so internal
    capitals in multi-word replacements survive."""
    if not replacement:
        return replacement
    if occurrence.isupper() and len(occurrence) > 1:
        return replacement.upper()
    if occurrence[:1].isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement[0].lower() + replacement[1:]


def load_glossary(csv_path: str | Path) -> list[tuple[str, str]]:
    """Load [(english_term, finnish_replacement), ...] from a glossary CSV,
    or [] if missing. Matching is whole-word and case-insensitive; the
    replacement's capitalization follows the matched occurrence ("TORRENT"
    -> "VIRTAUS", "torrent" -> "virtaus"). An exact-case row overrides that
    for its own spelling. Longest terms are matched first."""
    csv_path = Path(csv_path)
    pairs: list[tuple[str, str]] = []
    if not csv_path.exists():
        return pairs
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            src = (row.get("english") or "").strip()
            dst = (row.get("finnish") or "").strip()
            if src and dst:
                pairs.append((src, dst))
    return pairs


def load_vanilla_name_candidates(vanilla_path: str | Path) -> list[str]:
    """All character and place names from the vanilla game's NpcName/PlaceName
    FMGs (base + DLC) — offered in the glossary editor so a name can be picked
    exactly as the game spells it instead of typed from memory."""
    vanilla_path = Path(vanilla_path)
    msg_dir = None
    for candidate in (vanilla_path / "msg" / "engus",
                      vanilla_path / "Game" / "msg" / "engus"):
        if candidate.is_dir():
            msg_dir = candidate
            break
    if msg_dir is None:
        return []

    from soulstruct.containers import Binder
    from soulstruct.base.text.fmg import FMG

    names: set[str] = set()
    for bnd_name in ("item.msgbnd.dcx", "item_dlc01.msgbnd.dcx", "item_dlc02.msgbnd.dcx"):
        bnd_path = msg_dir / bnd_name
        if not bnd_path.exists():
            continue
        try:
            bnd = Binder.from_path(bnd_path.as_posix())
        except Exception:
            continue
        for entry in bnd.entries:
            if not re.match(r"^(NpcName|PlaceName)(_dlc\d+)?\.fmg$", entry.name):
                continue
            try:
                fmg = FMG.from_bytes(entry.data)
            except Exception:
                continue
            for v in fmg.entries.values():
                if v and v.strip():
                    names.add(v.strip())
    return sorted(names)


def save_glossary(csv_path: str | Path, pairs: list[tuple[str, str]]) -> None:
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["english", "finnish"])
        for src, dst in pairs:
            if src.strip() and dst.strip():
                writer.writerow([src.strip(), dst.strip()])


def _protect(text: str, glossary: list[tuple[str, str]] | None = None,
             paren_translator=None) -> tuple[str, list[str]]:
    placeholders: list[str] = []

    def _mask(replacement: str) -> str:
        placeholders.append(replacement)
        return f"XQ{len(placeholders) - 1}"

    def _mask_protected(m: re.Match) -> str:
        token = m.group(0)
        if token.startswith("(") and paren_translator is not None:
            inner = token[1:-1]
            if inner.strip() and not _is_aside(inner):
                # Real content, not a stage direction: translate it on its
                # own (in place, argos folds/drops it) and restore to the
                # translation instead of the English original.
                try:
                    return _mask("(" + paren_translator(inner) + ")")
                except Exception:
                    pass
        return _mask(token)

    # Structural tags/asides first so glossary terms can't split one in half.
    text = _PROTECTED_RE.sub(_mask_protected, text)
    if glossary:
        # Case-insensitive matching; an exact-case row wins as typed, any
        # other case form gets the replacement reshaped to the occurrence's
        # capitalization ("TORRENT" -> "VIRTAUS", "torrent" -> "virtaus").
        exact = dict(glossary)
        ci = {}
        for src, dst in glossary:
            ci.setdefault(src.lower(), dst)

        def _glossary_repl(m: re.Match) -> str:
            occ = m.group(0)
            if occ in exact:
                return _mask(exact[occ])
            return _mask(_match_case(occ, ci[occ.lower()]))

        pattern = re.compile("|".join(
            rf"(?<!\w){re.escape(src)}(?!\w)"
            for src, _ in sorted(glossary, key=lambda p: -len(p[0]))
        ), re.IGNORECASE)
        text = pattern.sub(_glossary_repl, text)
    return text, placeholders


def _restore(text: str, placeholders: list[str]) -> str:
    found: set[int] = set()

    def _repl(m: re.Match) -> str:
        idx = int(m.group(1))
        found.add(idx)
        replacement = placeholders[idx]
        suffix = m.group(2) or ""
        if suffix and not replacement.startswith(("<", "(")):
            # Finnish case ending the model wrote as "XQ0:n" — attach it
            # grammatically to a glossary term (gradation + vowel harmony:
            # "Rajahauta" + "ssa" -> "Rajahaudassa"); keep the colon form
            # for game tags, where it isn't ours to reshape.
            return _fi_attach_ending(replacement, suffix.lstrip(": "))
        return replacement + suffix

    restored = _TOKEN_RE.sub(_repl, text)
    # If the model dropped a token outright (observed for standalone
    # parentheticals/vocatives), don't silently lose the original text.
    missing = [placeholders[i] for i in range(len(placeholders)) if i not in found]
    if missing:
        restored = (restored.rstrip() + " " + " ".join(missing)).strip()
    return restored


# Signal words for the EN-vs-FI vote in looks_english(). Words spelled the
# same in both languages ("on", "me", "he", "no") are deliberately left out
# of the EN side — they'd just cancel against the FI side. The archaic set
# (thee/thou/hath/shalt...) matters: Elden Ring dialogue is full of it.
_EN_STOPWORDS = frozenset(
    "the of and to a in is you that it with for this are was be as at "
    "from by an or not your its has have will can who when what my we "
    "she him his her they them their there then than these those been "
    "had did does do but if so all one out up down now here why how "
    "must may shall should would could might into some any most more "
    "very still were i "
    "thee thou thy thine ye hath doth shalt wilt art unto upon oft "
    "naught nary ere".split()
)
_FI_STOPWORDS = frozenset(
    "ja on ei että joka sinä olet mutta kun niin sen hän myös vain se jos "
    "ole kanssa mikä tämä ovat sinun voi kuin ne oli jonka "
    "minä minun minua minulle minut sinua sinulle sinut hänen hänet "
    "hänelle meidän meidät meille teidän teille heidän heidät heille "
    "tämän tätä tuo tuon nämä nuo siellä täällä tänne sinne nyt sitten "
    "koska vielä jo eikä tai sekä jotta kunnes kuinka miksi missä mihin "
    "mistä mitä kuka kenen joku jokin kaikki olla olit olivat ollut "
    "olleet tulee tuli voit voidaan täytyy pitää ilman mukaan luona "
    "kohti asti ennen jälkeen aina koskaan paljon hyvin kuten "
    "olen olemme olette anteeksi kiitos hei terve kyllä ehkä siis vaan "
    "miten näin oi lordi leidi herra rouva kuningas kuningatar vuoksi "
    "tässä tästä tähän sitä siitä".split()
)

# Language-neutral vocalizations that appear verbatim in both languages'
# game text — never evidence of untranslated English, in either the
# shared-word signal or the untranslated-word capture.
_INTERJECTIONS = frozenset(
    "ahh aah ah hmm hm mhm heh hah haha hahaha ooh oohh woah whoa mmm "
    "mm ngh hrr grr urgh ugh agh gah guh argh aargh oof phew tsk psst "
    "shh ehh eh hmph bah ha ho hoho ohh oh ohhh aha oho huh hush".split()
)


def looks_english(text: str, *, default: bool = True) -> bool:
    """Rough English-vs-Finnish call, used to decide whether a mod-authored
    line (one that differs from vanilla, or has no vanilla counterpart at
    all — overhaul mods like Convergence add thousands) still needs
    translating. Stopword vote decides when either side scores; otherwise
    å/ä/ö means Finnish, and a signal-free line (bare names, numbers)
    falls back to `default` — callers pick the safer assumption for their
    context.
    """
    words = re.findall(r"[a-zåäö']+", text.lower())
    en = sum(1 for w in words if w in _EN_STOPWORDS)
    fi = sum(1 for w in words if w in _FI_STOPWORDS)
    if en != fi:
        return en > fi
    if any(c in text for c in "åäöÅÄÖ"):
        return False
    return default


# A subtitle that is nothing but a voice-direction note ("<Hard panting
# while taking damage>", "<sleeping>") — describes a sound, is not spoken
# dialogue. Excluded from translation AND from voice generation: TTS would
# read the description aloud. <font ...> markup and <?placeholder?> tokens
# are real content and never match.
_STAGE_DIRECTION_RE = re.compile(r"\s*<(?!font\b|img\b|\?)[^<>]*>\s*", re.DOTALL)


def is_stage_direction(text: str) -> bool:
    return bool(text) and bool(_STAGE_DIRECTION_RE.fullmatch(text))


def has_translatable_text(text: str) -> bool:
    """False for lines with nothing to translate: punctuation-only ("..."),
    tag/aside-only ("<sleeping>"), or pure vocalizations ("Hah!"). These
    must not count as untranslated — MT can only no-op on them."""
    stripped = _PROTECTED_RE.sub(" ", text)
    words = re.findall(r"[^\W\d_]+", stripped)
    return any(w.lower() not in _INTERJECTIONS for w in words)


def translation_is_noop(translated: str, source: str) -> bool:
    """True when MT effectively translated nothing: output equals the
    source, or every content word of the source survived verbatim and the
    output shows no Finnish. Partial translations ("Neula Knight Leda")
    are NOT no-ops — they're better surfaced as pending review than
    silently dropped — and clearly Finnish output is never a no-op even
    when it keeps every source name ("Onko tuo hän, Torrent?")."""
    if translated.strip().lower() == source.strip().lower():
        return True
    if looks_finnish(translated):
        return False
    words = {w.lower() for w in _WORD_RE.findall(source)
             if w.lower() not in _EN_STOPWORDS and w.lower() not in _INTERJECTIONS}
    if not words:
        return True   # nothing translatable to begin with
    t_lower = translated.lower()
    return all(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", t_lower) for w in words)


def looks_finnish(text: str) -> bool:
    """Positive Finnish evidence only: Finnish stopwords outvote English
    ones, or the text has å/ä/ö. Unlike ``not looks_english(...)`` this
    never guesses from a default — a signal-free line returns False. Used
    to protect already-translated lines from the shared-word signal when
    they legitimately keep English names ("ja Herra Gideon Ofnir,
    Kaikkitietävä." shares Gideon+Ofnir with the vanilla English)."""
    words = re.findall(r"[a-zåäö']+", text.lower())
    en = sum(1 for w in words if w in _EN_STOPWORDS)
    fi = sum(1 for w in words if w in _FI_STOPWORDS)
    return fi > en or any(c in text for c in "åäöÅÄÖ")


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")


def looks_english_by_vocab(text: str, en_vocab: set[str] | None) -> bool:
    """English evidence from the game's own vocabulary: when (nearly) all of
    a line's words appear in the vanilla ENGLISH corpus, the line is English
    game text — catches renamed-in-place item names ("Velvet Flurry",
    "Bloodflame Scythe") that have no stopword/umlaut signal and share no
    words with their vanilla slot. Requires ≥2 words: single words are
    unreliable (Finnish blood-message words like "suo"/"alas"/"ilo" collide
    with English vocabulary)."""
    if not en_vocab:
        return False
    words = [w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", text)]
    if len(words) < 2:
        return False
    hits = sum(1 for w in words if w in en_vocab)
    return hits / len(words) >= 0.75


def retains_vanilla_english(mod_text: str, van_text: str) -> bool:
    """True when a mod's edited line still carries the vanilla English —
    i.e. most of the vanilla line's content words appear whole-word in the
    mod text. A translation drops the English content words, so this is a
    strong per-entry "still untranslated" signal even for short lines with
    no stopword/umlaut evidence at all (Convergence's "Vanilla: Crouching"
    vs vanilla "Crouching").

    Names must not flag finished translations, so besides the majority
    threshold ("Neulan ritari Leda" shares only "Leda"), on prose lines —
    ones where vanilla has lowercase content words — a word capitalized in
    BOTH texts is treated as a surviving proper noun and doesn't count
    ("Nepheli Loux. Tapasimme Myrskyhunnussa." shares only the name).
    Title-style lines (every word capitalized) keep the plain ratio, since
    there the capitals say nothing. Neutral vocalizations ("Ahh", "Hmm")
    are never evidence.
    """
    van_words = [w for w in _WORD_RE.findall(van_text)
                 if w.lower() not in _EN_STOPWORDS
                 and w.lower() not in _INTERJECTIONS]
    if not van_words:
        return False
    mod_lower = mod_text.lower()
    denom = {w.lower() for w in van_words}
    shared = {w for w in denom
              if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", mod_lower)}
    if any(w[:1].islower() for w in van_words):  # prose, not a title line
        van_caps = {w.lower() for w in van_words if w[:1].isupper()}
        mod_caps = {w.lower() for w in _WORD_RE.findall(mod_text) if w[:1].isupper()}
        shared -= van_caps & mod_caps  # surviving proper nouns don't count
    return len(shared) / len(denom) >= 0.5


def find_untranslated_words(source: str, translated: str,
                            glossary: list[tuple[str, str]] | None = None) -> set[str]:
    """Words the model passed through unchanged — fantasy vocabulary argos
    doesn't know (Glintstone, Larval Tear), collected as glossary candidates
    so the user doesn't have to sift the output for them by hand.

    A word counts when it appears whole-word, case-identical, in both the
    English source and the Finnish output — allowing a short lowercase tail
    in the output, because the model inflects survivors Finnish-style
    ("Glintstonen taikuutta", "Raya Lucarian"). Excluded: <...> tags and
    stage-direction asides (restored verbatim by design, so they'd always
    false-positive), existing glossary terms and replacements (already
    handled), and EN/FI stopwords. Compound survivals still match
    ("Glintstone-noituus" contains whole-word "Glintstone").
    """
    src_clean = _PROTECTED_RE.sub(
        lambda m: m.group(0) if m.group(0).startswith("(") and not _is_aside(m.group(0)[1:-1]) else " ",
        source)
    skip = {w.lower() for pair in (glossary or []) for w in _WORD_RE.findall(" ".join(pair))}
    found: set[str] = set()
    for word in set(_WORD_RE.findall(src_clean)):
        lw = word.lower()
        if lw in _EN_STOPWORDS or lw in _FI_STOPWORDS or lw in _INTERJECTIONS \
                or lw in skip:
            continue
        if re.search(rf"(?<!\w){re.escape(word)}[a-zåäö]{{0,5}}(?!\w)", translated):
            found.add(word)
    return found


def is_installed() -> bool:
    try:
        import argostranslate.translate  # noqa: F401
        return True
    except ImportError:
        return False


def ensure_language_pack() -> None:
    """Download + install the en->fi model if not already present. One-time, ~50MB."""
    import argostranslate.package
    import argostranslate.translate

    installed = argostranslate.translate.get_installed_languages()
    has_en_fi = any(
        lang.code == "en" and any(t.to_lang.code == "fi" for t in lang.translations_from)
        for lang in installed
    )
    if has_en_fi:
        return

    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()
    pkg = next((p for p in available if p.from_code == "en" and p.to_code == "fi"), None)
    if pkg is None:
        raise RuntimeError("No en->fi argostranslate package found in the package index.")
    path = pkg.download()
    argostranslate.package.install_from_path(path)


# Line ends that close a thought — a physical line ending in one of these is
# a real line break, not an FMG display wrap that split a sentence.
_LINE_END_PUNCT = (".", "!", "?", ":", ";", '"', "”", "’", "…", ")")


def _merge_wrapped_lines(text: str) -> list[tuple[str, int, int]]:
    """Split into logical lines, joining FMG display wraps back together.

    ER descriptions hard-wrap mid-sentence ("...oils secreted\\nfrom the
    roots..."); argos translates each physical line as its own sentence and
    drops/fragments words at the seam (confirmed: "secreted" vanished). A
    line is a continuation of the previous one when the previous doesn't
    end a thought (no terminal punctuation) and it starts lowercase, or the
    previous ends with a comma. Uppercase starts stay separate on purpose —
    stat blocks like "Skill: Impaling Thrust\\nFP Cost: 8" must not join.

    Returns [(logical_line, physical_line_count, max_physical_width), ...].
    """
    merged: list[tuple[str, int, int]] = []
    for line in text.split("\n"):
        if merged:
            prev, n, width = merged[-1]
            ps, cs = prev.rstrip(), line.strip()
            if ps and cs and (ps.endswith(",") or
                              (not ps.endswith(_LINE_END_PUNCT) and cs[:1].islower())):
                merged[-1] = (ps + " " + cs, n + 1, max(width, len(line)))
                continue
        merged.append((line, 1, len(line)))
    return merged


def _wrap_line(text: str, width: int) -> list[str]:
    """Greedy word wrap for re-breaking a translated logical line to roughly
    the original FMG display width."""
    lines: list[str] = []
    cur = ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


# Standalone translations of words the model refused to translate in
# context — argos knows many words alone that it passes through mid-line
# ("Vanilla: Introspection" keeps "Introspection", yet "Introspection"
# alone gives "Esitarkastelu"). None = no usable standalone translation
# (unknown word or a name — those must stay as-is).
_STANDALONE_WORD_CACHE: dict[str, str | None] = {}


def _translate_word_standalone(word: str) -> str | None:
    key = word.lower()
    if key not in _STANDALONE_WORD_CACHE:
        import argostranslate.translate
        out = argostranslate.translate.translate(
            word[:1].upper() + word[1:], "en", "fi").strip().rstrip(".")
        usable = (out and out.lower() != key and "\n" not in out
                  and len(out) <= 40 and len(out.split()) <= 2)
        _STANDALONE_WORD_CACHE[key] = out if usable else None
    return _STANDALONE_WORD_CACHE[key]


def _substitute_survivors(text: str, result: str,
                          glossary_expanded: list[tuple[str, str]] | None) -> str:
    """Replace English words that survived both translation passes with
    their standalone translations, keeping the occurrence's capitalization
    and any Finnish case ending the model wrote onto the survivor. Names
    stay put naturally: their standalone "translation" comes back unchanged,
    which _translate_word_standalone reports as unusable."""
    for word in find_untranslated_words(text, result, glossary_expanded):
        repl = _translate_word_standalone(word)
        if not repl:
            continue

        def _repl(m: re.Match) -> str:
            tail = m.group(1) or ""
            base = _fi_attach_ending(repl, tail) if tail else repl
            return _match_case(m.group(0), base)

        result = re.sub(rf"(?<!\w){re.escape(word)}([a-zåäö]{{1,5}})?(?!\w)",
                        _repl, result)
    return result


def _lower_except_tokens(s: str) -> str:
    """Lowercase text without touching the XQ<n> mask tokens."""
    parts = re.split(r"(XQ\d+)", s)
    return "".join(p if re.fullmatch(r"XQ\d+", p) else p.lower() for p in parts)


def _restore_case_from(original: str, translated: str) -> str:
    """Re-apply the original's capitalization to a translation of the
    lowercased text: words that survived translation get their original
    casing back ("leda" -> "Leda"), and the line's leading capital returns."""
    caps = {w.lower(): w for w in _WORD_RE.findall(original) if w[:1].isupper()}
    out = re.sub(r"[a-zåäö][a-zåäö'’\-]*",
                 lambda m: caps.get(m.group(0), m.group(0)), translated)
    for i, ch in enumerate(original):
        if ch.isalpha():
            if ch.isupper():
                for j, out_ch in enumerate(out):
                    if out_ch.isalpha():
                        out = out[:j] + out_ch.upper() + out[j + 1:]
                        break
            break
    return out


def _translate_segment(text: str, glossary_expanded: list[tuple[str, str]] | None,
                       translate_parens: bool = True) -> str:
    import argostranslate.translate
    paren_translator = None
    if translate_parens:
        # One level of recursion: content inside "(...)" is translated as its
        # own segment (never re-entered — inner parens stay verbatim).
        paren_translator = lambda inner: _translate_segment(
            inner, glossary_expanded, translate_parens=False)
    protected, placeholders = _protect(text, glossary_expanded, paren_translator)
    translated = argostranslate.translate.translate(protected, "en", "fi")
    result = _restore(translated, placeholders)

    # Title-case retry: argos treats capitalized words as proper nouns and
    # passes them through ("Needle Knight Leda" -> "Neula Knight Leda"), so
    # name/title lines barely translate. When English words survive, retry
    # with the text lowercased (mask tokens intact — glossary/tag casing is
    # preserved by the placeholders) and keep whichever version left fewer
    # English words; real names survive both passes and keep their casing.
    leftover = find_untranslated_words(text, result, glossary_expanded)
    if leftover:
        lowered = _lower_except_tokens(protected)
        if lowered != protected:
            alt = _restore(
                argostranslate.translate.translate(lowered, "en", "fi"),
                placeholders)
            alt = _restore_case_from(text, alt)
            if len(find_untranslated_words(text, alt, glossary_expanded)) < len(leftover):
                result = alt
        # Whatever still survived both passes gets translated word-by-word
        # ("Vanilja: Introspection" -> "Vanilja: Esitarkastelu"); names
        # come back unchanged from the standalone pass and stay put.
        result = _substitute_survivors(text, result, glossary_expanded)
    return result


def translate_en_to_fi(text: str, glossary: list[tuple[str, str]] | None = None) -> str:
    """Translate a single English string to Finnish. Raises if the language pack isn't installed.

    Text inside <...> (placeholders like <PLAYER_NAME>) is restored verbatim,
    as are stage-direction asides like "(laughs)"; parentheticals with real
    content ("(increases by 10% for 30 seconds)") are translated separately.
    Mid-sentence FMG display wraps are joined before translating and the
    translation re-wrapped to the original width, so no words fall into the
    line-break seams.

    glossary: optional [(english_term, finnish_replacement), ...] pairs. Each
    whole-word occurrence of an english_term (any capitalization) is replaced
    with its finnish_replacement instead of being machine-translated (names,
    invented terms, running gags — anything that must stay consistent); the
    replacement takes the occurrence's capitalization. English possessives
    are derived automatically ("Limgrave's" → genitive of the replacement)
    unless the user defined their own row for them.
    """
    expanded = _expand_glossary(glossary) if glossary else None
    out: list[str] = []
    for line, physical_count, width in _merge_wrapped_lines(text):
        if not line.strip():
            out.append(line)
            continue
        translated = _translate_segment(line, expanded)
        if physical_count > 1:
            out.extend(_wrap_line(translated, max(width, 20)))
        else:
            out.append(translated)
    return "\n".join(out)
