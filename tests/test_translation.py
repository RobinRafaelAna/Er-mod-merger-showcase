"""Translation: glossary masking, Finnish inflection, and the LLM engine's request/checks.

No model is needed: the LLM engine is pointed at a small fake OpenAI-compatible server.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from core.localisation import machine_translate as mt
from core.localisation.llm_translate_engine import (
    LLMTranslateEngine, has_runaway_repetition, tags_preserved)


# --- Finnish inflection of glossary terms -------------------------------------------

@pytest.mark.parametrize("word, ending, expected", [
    ("Rajahauta", "ssa", "Rajahaudassa"),   # consonant gradation t -> d
    ("Pöytä", "ssa", "Pöydässä"),           # gradation + front-vowel harmony
    ("Virtaus", "n", "Virtauksen"),         # -us stem -> -ukse-
    ("Kalanen", "n", "Kalasen"),            # -nen stem -> -se-
    ("puku", "n", "puvun"),                 # k -> v between round vowels
    ("Gostoc", "n", "Gostocin"),            # foreign consonant-final name
    ("linnake", "een", "linnakeen"),        # no triple vowel at the seam
])
def test_case_endings(word, ending, expected):
    assert mt._fi_attach_ending(word, ending) == expected


def test_glossary_derives_possessives_and_keeps_case():
    expanded = mt._expand_glossary([("Torrent", "Virtaus")])
    assert ("Torrent's", "Virtauksen") in expanded
    assert mt._match_case("TORRENT", "Virtaus") == "VIRTAUS"
    assert mt._match_case("torrent", "Virtaus") == "virtaus"


def test_masking_protects_tags_and_restores_glossary_with_case_ending():
    masked, ph = mt._protect("Summon <?itemName?> and ride Torrent (quietly).",
                             [("Torrent", "Virtaus")])
    assert "<?itemName?>" not in masked and "Torrent" not in masked
    # The MT model writes a case ending after the token; it is attached grammatically.
    i_tag, i_term, i_aside = ph.index("<?itemName?>"), ph.index("Virtaus"), ph.index("(quietly)")
    out = mt._restore(f"Kutsu XQ{i_tag} ja ratsasta XQ{i_term}:lla XQ{i_aside}.", ph)
    assert out == "Kutsu <?itemName?> ja ratsasta Virtauksella (quietly)."
    # A token the MT model dropped is put back rather than lost.
    dropped = mt._restore(f"Kutsu XQ{i_tag} ja ratsasta XQ{i_term}:lla.", ph)
    assert dropped.endswith("(quietly)")


# --- LLM output checks -----------------------------------------------------------------

def test_tag_and_repetition_checks():
    assert tags_preserved("Read <?name?>'s message", "Lue <?name?>:n viesti")
    assert not tags_preserved("Read <?name?>'s message", "Lue viesti")
    assert has_runaway_repetition("of the clan", "suvun suvun suvun suvun suvun")
    assert not has_runaway_repetition("of the clan", "suvun")


# --- LLM engine against a fake llama-server -------------------------------------------

class _FakeServer:
    def __init__(self, reply: str):
        self.reply, self.requests = reply, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append(json.loads(body))
                data = json.dumps({"choices": [{"message": {"content": outer.reply}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.httpd.server_address[1]


class _Running:          # stands in for the llama-server process handle
    def poll(self):
        return None


def engine_for(server: _FakeServer) -> LLMTranslateEngine:
    e = LLMTranslateEngine()
    e._proc, e._port = _Running(), server.port
    return e


def test_only_glossary_terms_in_the_line_are_sent():
    srv = _FakeServer("Ratsasta Virtauksella")
    out = engine_for(srv).translate("Ride Torrent",
                                    glossary=[("Torrent", "Virtaus"), ("Rune", "Riimu")])
    assert out == "Ratsasta Virtauksella"
    user_msg = srv.requests[0]["messages"][-1]["content"]
    assert "Torrent = Virtaus" in user_msg and "Riimu" not in user_msg
    srv.httpd.shutdown()


@pytest.mark.parametrize("reply", ["Lue viesti", "suvun suvun suvun suvun suvun"])
def test_bad_llm_output_is_rejected_so_the_caller_falls_back(reply):
    srv = _FakeServer(reply)
    e = engine_for(srv)
    assert e.translate("Read <?name?>'s message of the clan") is None
    assert e.last_error
    srv.httpd.shutdown()
