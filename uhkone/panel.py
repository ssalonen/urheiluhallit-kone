"""Local-only control panel for the existing no-payment booking coordinator.

No checkout, waiting-list or profile-edit endpoint exists. Child's name stays
in process memory and never appears in HTTP responses or a command line.
"""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .browser import BrowserError, _validate_course_url
from .origin import PRODUCTION_URL, is_local_test

HELSINKI = ZoneInfo("Europe/Helsinki")
DEFAULT_IDS = [110980, 110962, 110970]
HTML = r"""<!doctype html><html lang="fi"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Uimakoulun varauspaneeli</title>
<style>
*{box-sizing:border-box}body{font:16px system-ui;background:#f5f7fb;color:#17283c;max-width:920px;margin:30px auto;padding:0 16px}h1{font-size:27px}
small{color:#617287}.layout{display:grid;gap:14px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
section,.card{background:#fff;border:1px solid #dae2eb;border-radius:12px;padding:16px}
.card h3{font-size:19px;margin:8px 0}.muted{color:#5b6c7c}.row{display:flex;gap:8px;flex-wrap:wrap}
button{padding:11px 16px;border:0;border-radius:8px;background:#1156a9;color:white;cursor:pointer;font:inherit;font-weight:600}
button:disabled{opacity:.4;cursor:not-allowed}button.alt{background:#eaf0f8;color:#19334f}button.stop{background:#943b3b}
input,select{width:100%;padding:11px;margin:6px 0 13px;border:1px solid #b9c6d5;border-radius:8px;font:inherit}
label{font-weight:600}#message{min-height:1.5em}.warning{border-left:3px solid #c88b27;padding-left:10px}
</style>
<h1>Uimakoulun varauspaneeli</h1>
<p><small>Paikallinen · 127.0.0.1 · vain maksuton varaus</small> <strong id="clock"></strong></p>
<main class="layout"><section><strong id="phase">VALMIS</strong><p id="health" aria-live="polite"></p><p id="last-checked"></p><p id="note"></p><p id="message" role="status"></p></section>
<div class="cards" id="cards"></div>
<section><h2>Asetukset</h2><p class="muted">Syöttöjärjestys on varausjärjestys; muita kursseja ei haeta.</p>
<form id="form"><label for="ids">Kurssi-ID:t etusijajärjestyksessä</label>
<input id="ids" value="110980 110962 110970" required autocomplete="off">
<label for="price">Enimmäishinta (€)</label><input id="price" value="76" required autocomplete="off">
<label for="child">Lapsen tarkka nimi</label><input id="child" autocomplete="off" placeholder="Valitse tililtä tai kirjoita nimi">
<div class="row"><button type="button" class="alt" id="children">Hae alitilit kirjautuneesta kaupasta</button></div>
<label for="child-select">Valitse lapsitili</label><select id="child-select" aria-label="Lapsitilit"><option value="">Hae alitilit ensin</option></select>
<small>Nimi, kurssit ja hintaraja säilyvät tässä selaimessa myös uudelleenkäynnistyksen jälkeen.</small>
<div class="row"><button id="save">Tallenna tämän ajon asetukset</button></div></form></section>
<section><h2>Manuaalinen varatie</h2><p>Jos automaatio ei voi jatkaa, pysäytä ajo ja tarkista ensin alkuperäisen Chromium-ikkunan ostoskori ja varaukset. Alla oleva linkki avaa kurssin julkisen verkkokauppasivun; eri selainprofiililla ostoskori ei välttämättä ole sama. Älä tee uutta varausta, jos edellinen on voinut onnistua.</p><div id="manual-links" class="row"></div></section>
<section><h2>Ohjaus</h2><div class="row">
<button id="start">Käynnistä ennen klo 7.00</button>
<button id="pause" class="alt">Tauko</button><button id="resume" class="alt">Jatka</button>
<button id="stop" class="stop">Pysäytä</button></div>
<p class="warning">Ei maksuja eikä jonoja. Maksamattoman ostoskorin tarkistus ja mahdollinen myöhempi maksaminen tapahtuvat oikean kaupan kautta erikseen. Paikka voi vanhentua.</p></section></main>
<script>
const token=__TOKEN__, $=x=>document.getElementById(x);let init=true;
const PREF_KEY="uhkone.panel.preferences.v1";
function savedPrefs(){try{const p=JSON.parse(localStorage.getItem(PREF_KEY)||"null");
if(!p||!Array.isArray(p.ids)||typeof p.max_price!=="string"||typeof p.child!=="string")return null;
return {ids:p.ids,max_price:p.max_price,child:p.child};}catch(_){return null}}
function storePrefs(p){try{localStorage.setItem(PREF_KEY,JSON.stringify({ids:p.ids,max_price:p.max_price,child:p.child}))}catch(_){}}

async function req(path,data){const opts=data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Panel-Token':token},body:JSON.stringify(data)};
const r=await fetch(path,{...opts,cache:'no-store'});const v=await r.json();if(!r.ok)throw Error(v.error||'Pyyntö epäonnistui');return v}
function el(tag,t,cls){let x=document.createElement(tag);x.textContent=t;if(cls)x.className=cls;return x}
function draw(s){$('clock').textContent=new Date(s.now).toLocaleTimeString('fi-FI');$('phase').textContent=s.phase.toUpperCase();$('health').textContent='Yhteyden tila: '+s.health.replaceAll('_',' ');$('last-checked').textContent=s.last_checked?'Viimeinen onnistunut tarkistus: '+new Date(s.last_checked).toLocaleTimeString('fi-FI'):'';$('note').textContent=s.note;
if(init){$('ids').value=s.ids.join(' ');$('price').value=s.max_price;init=false}
$('cards').replaceChildren(...s.ids.map((id,i)=>{let d=s.details[String(id)]||{},e=el('article','');e.className='card';
e.append(el('small','VAIHTOEHTO '+(i+1)),el('h3',d.location?d.location+' · '+d.time:'Kurssi '+id),el('p','ID '+id,'muted'),
el('p',d.opens_at?'Ilmoittautuminen '+new Date(d.opens_at).toLocaleString('fi-FI'):'Avausaika tarkistetaan käynnistettäessä','muted'),
el('strong',d.phase==='held'?'Ostoskorissa (maksamatta)':d.phase==='adding'?'Varausyritys':d.phase==='reservation_pending'?'Varauksen tilaa selvitetään':d.confirmed?'Vahvistettu':d.held?'Ostoskorissa (maksamatta)':d.available?'Vapaa paikka':d.available===false?'Ei varattavissa':'Odottaa'));return e}));
for(let k of ['ids','price','child','child-select','children','save'])$(k).disabled=s.running;
$('start').disabled=s.running||s.children_loading;$('children').disabled=s.running||s.children_loading;
const manual=$('manual-links');manual.replaceChildren();
if(!s.running)for(const id of s.ids){const d=s.details[String(id)]||{};if(d.manual_url){const a=el('a','Avaa kurssi '+id+' kaupassa');a.href=d.manual_url;a.target='_blank';a.rel='noopener noreferrer';manual.append(a)}}
else manual.append(el('span','Pysäytä varausajo ennen manuaalista siirtymistä.'));
$('pause').disabled=!s.running||s.paused;$('resume').disabled=!s.running||!s.paused;$('stop').disabled=!s.running}
async function refresh(){try{draw(await req('/api/status'))}catch(e){$('message').textContent='Yhteys paneeliin katkesi'}}
$('form').onsubmit=async e=>{e.preventDefault();try{let ids=$('ids').value.trim().split(/[\s,;]+/).map(Number);
const values={ids,max_price:$('price').value,child:$('child').value};
const saved=await req('/api/settings',values);draw(saved);storePrefs({ids:saved.ids,max_price:saved.max_price,child:values.child});$('message').textContent='Asetukset tallennettu tähän selaimeen'}catch(err){$('message').textContent=err.message}};
$('child-select').onchange=()=>{if($('child-select').value)$('child').value=$('child-select').value};
$('children').onclick=async()=>{try{$('message').textContent='Avaan kirjautumisikkunan ja luen alitilit...';const result=await req('/api/children',{});const sel=$('child-select');sel.replaceChildren();
sel.append(new Option('Valitse lapsitili', ''));
for(const name of result.children)sel.append(new Option(name,name));
if(result.children.includes($('child').value))sel.value=$('child').value;
$('message').textContent=result.children.length+' alitiliä löytyi. Valitse lapsi ja tallenna asetukset.';
}catch(err){$('message').textContent=err.message}};
function currentSettings(){return {ids:$('ids').value.trim().split(/[\s,;]+/).map(Number),max_price:$('price').value,child:$('child').value}}
$('start').onclick=async()=>{try{$('start').disabled=true;const values=currentSettings();const saved=await req('/api/settings',values);
storePrefs({ids:saved.ids,max_price:saved.max_price,child:values.child});draw(await req('/api/start',{}));$('message').textContent='Ajo käynnistetty tallennetuilla asetuksilla';
}catch(err){$('message').textContent=err.message;await refresh()}finally{$('start').disabled=false}};
for(let action of ['pause','resume','stop'])$(action).onclick=async()=>{try{draw(await req('/api/'+action,{}));$('message').textContent=''}catch(err){$('message').textContent=err.message}};
async function initialize(){try{let s=await req('/api/status');const prefs=savedPrefs();
if(prefs){$('ids').value=prefs.ids.join(' ');$('price').value=prefs.max_price;$('child').value=prefs.child;
if(!s.running){s=await req('/api/settings',prefs)}}
draw(s)}catch(e){$('message').textContent='Tallennettujen asetusten lataus epäonnistui: '+e.message}}
initialize();setInterval(refresh,1000);
</script></html>"""


class PanelError(ValueError):
    pass


class PanelState:
    def __init__(self):
        self.lock = threading.RLock()
        self.ids = list(DEFAULT_IDS)
        self.max_price = "76"
        self.child = ""
        self.phase = "valmis"
        self.note = "Valmistele kurssit ja käynnistä ajo ennen ilmoittautumisen avautumista."
        self.details: dict[int, dict[str, Any]] = {}
        self.running = False
        self.paused = False
        self.stop_requested = False
        self.children_loading = False
        self.health = "valmis"
        self.last_checked: str | None = None

    def public(self):
        with self.lock:
            return {
                "ids": list(self.ids), "max_price": self.max_price,
                "has_child": bool(self.child), "phase": self.phase, "note": self.note,
                "health": self.health, "last_checked": self.last_checked,
                "details": {str(k): dict(v) for k, v in self.details.items()},
                "running": self.running, "paused": self.paused,
                "children_loading": self.children_loading,
                "now": datetime.now(HELSINKI).isoformat(timespec="seconds"),
            }

    def configure(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"ids", "max_price", "child"}:
            raise PanelError("Virheelliset asetukset")
        ids, raw = payload.get("ids"), payload.get("max_price")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 12
                or any(type(i) is not int or i <= 0 for i in ids)
                or len(set(ids)) != len(ids)):
            raise PanelError("Kurssitunnusten pitää olla yksilöllisiä kokonaislukuja")
        if not isinstance(raw, str) or len(raw) > 20:
            raise PanelError("Virheellinen enimmäishinta")
        try:
            price = Decimal(raw.replace(",", "."))
        except InvalidOperation as exc:
            raise PanelError("Virheellinen enimmäishinta") from exc
        if (not price.is_finite() or price < 0 or price > 10000
                or price != price.quantize(Decimal("0.01"))):
            raise PanelError("Enimmäishinta on virheellinen")
        child = payload.get("child", "")
        if not isinstance(child, str) or len(child) > 160:
            raise PanelError("Virheellinen osallistuja")
        with self.lock:
            if self.running:
                raise PanelError("Asetuksia ei voi muuttaa kesken varausajon")
            self.ids, self.max_price = list(ids), str(price)
            self.child = child.strip()
            self.details = {}
            self.note = "Asetukset hyväksytty. Ei maksuja eikä jonotusta."

    def course_info(self, courses):
        with self.lock:
            for course in courses:
                row = self.details.setdefault(course.id, {})
                row.update({
                    "location": course.location, "time": course.time_start,
                    "opens_at": course.registration_start.isoformat() if course.registration_start else None,
                })
                row.pop("manual_url", None)
                try:
                    uri = course.url
                    parsed = urlsplit(uri)
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                    base = origin if is_local_test(origin) else PRODUCTION_URL
                    row["manual_url"] = _validate_course_url(course, base_url=base)
                except (AttributeError, BrowserError, TypeError, ValueError):
                    pass

    def progress(self, phase: str, metadata: dict):
        if phase == "heartbeat":
            return
        with self.lock:
            cid = metadata.get("course_id")
            if phase == "waiting" and type(cid) is int and cid in self.ids:
                self.details.setdefault(cid, {})["opens_at"] = metadata.get("opens_at")
            if phase == "candidate" and type(cid) is int and cid in self.ids:
                fields = ("available", "held", "confirmed", "payment_locked")
                self.details.setdefault(cid, {}).update({
                    key: bool(metadata[key]) for key in fields if key in metadata
                })
            if phase in {"adding", "held", "reservation_pending", "confirmed"} and type(cid) is int and cid in self.ids:
                self.details.setdefault(cid, {})["phase"] = phase
            if phase == "candidate":
                self.last_checked = datetime.now(HELSINKI).isoformat(timespec="seconds")
                self.health = "seurataan"
            elif phase == "retry":
                self.health = "ruuhka"
            elif phase in {"reservation_pending", "payment_pending"}:
                self.health = "varaus_epavarma"
            elif phase in {"held", "held_unpaid"}:
                self.health = "maksamaton_kori"
            elif phase == "deadline":
                self.health = "aika_paattyi"
            elif phase == "confirmed":
                self.health = "vahvistettu"
            self.phase = phase
            messages = {
                "waiting": "Odotetaan ilmoittautumista.",
                "candidate": "Kurssien saatavuus tarkistettu.",
                "adding": "Yritetään lisätä yksi kurssi ostoskoriin.",
                "held": "Maksamaton varaus havaittu. Muut kurssit lukittu.",
                "reservation_pending": "Selvitetään lähetetyn varauspyynnön tulos.",
                "retry": "Palvelin ei vastannut. Odotetaan ja yritetään uudelleen.",
                "deadline": "Varausyrityksen määräaika saavutettu.",
                "held_unpaid": "Maksamaton ostoskorivaraus. Ei maksettu eikä lopullisesti vahvistettu.",
            }
            self.note = messages.get(phase, self.note)

    def record_failure(self, exc: Exception) -> None:
        """Only coarse, non-sensitive diagnostics reach the dashboard."""
        message = str(exc).casefold()
        with self.lock:
            self.phase = "toimenpide_tarvitaan"
            if "kirjautum" in message or "authenticated" in message or "session" in message:
                self.health = "kirjaudu_uudelleen"
                self.note = "Kaupan kirjautuminen on tarkistettava. Ajo pysähtyi turvallisesti; tarkista ostoskori ennen uudelleenkäynnistystä."
            elif exc.__class__.__name__ in {"ClientError", "TransientBookingError", "TimeoutError", "ConnectionError"}:
                self.health = "yhteysongelma"
                self.note = "Yhteys kauppaan katkesi. Automaattinen varaus pysähtyi; tarkista tilanne ennen uutta yritystä."
            else:
                self.health = "toimenpide_tarvitaan"
                self.note = "Varausta ei voitu jatkaa luotettavasti. Tarkista alkuperäinen selain ja ostoskori ennen uutta yritystä."

    def checkpoint(self, wait):
        from .console import StopRequested
        while True:
            with self.lock:
                stopped, paused = self.stop_requested, self.paused
            if stopped:
                raise StopRequested("Paneelista pysäytetty")
            if not paused:
                return
            wait(0.25)


def _run(state: PanelState, directory: Path, executable: str | None):
    try:
        from .cli import get_filters, parser, run_reservation
        from .client import ShopClient
        from .config import load_settings
        with state.lock:
            argv = ["varaa-yksi", *map(str, state.ids), "--lapsi", state.child,
                    "--enintaan", state.max_price, "--ilman-maksua", "--ei-jono",
                    "--kesto", "30"]
            if executable:
                argv.extend(["--selain", executable])
        args = parser().parse_args(argv)
        with ShopClient() as client:
            outcome = run_reservation(args, get_filters(args, load_settings().filters),
                                      directory, client, dashboard=state)
        with state.lock:
            state.phase = "ostoskori_suljettu" if outcome == 0 else "epäonnistui"
            state.note = ("Maksamaton ostoskori tarkistettu, selain suljettu. Paikkaa ei ole maksettu eikä vahvistettu."
                          if outcome == 0 else "Varaus ei vahvistunut. Tarkista selain.")
    except KeyboardInterrupt:
        with state.lock:
            state.phase, state.note = "pysäytetty", "Ajo pysäytetty. Tarkista mahdollinen keskeneräinen varaus."
    except Exception as exc:  # noqa: BLE001 - Never expose backend errors through local HTTP.
        state.record_failure(exc)
    finally:
        with state.lock:
            state.running = False
            state.paused = False
            state.stop_requested = False


def serve_panel(directory: Path, *, port: int = 8765, executable: str | None = None,
                open_browser: bool = True, runner: Callable[[PanelState, Path, str | None], None] | None = None,
                on_listening: Callable[[str], None] | None = None,
                stop_event: threading.Event | None = None,
                child_loader: Callable[[Path, str | None], list[str]] | None = None):
    state = PanelState()
    csrf = secrets.token_urlsafe(32)

    port_holder = {"value": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def answer(self, obj, status=200, mime="application/json; charset=utf-8"):
            body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{port_holder['value']}"

        def do_GET(self):
            if not self.valid_host():
                self.answer({"error": "Pyyntö estetty"}, 403)
            elif self.path == "/":
                html = HTML.replace("__TOKEN__", json.dumps(csrf)).encode("utf-8")
                self.answer(html, mime="text/html; charset=utf-8")
            elif self.path == "/api/status":
                self.answer(state.public())
            else:
                self.answer({"error": "Ei löytynyt"}, 404)

        def do_POST(self):
            origin = f"http://127.0.0.1:{port_holder['value']}"
            if (not self.valid_host() or self.headers.get("Origin") != origin
                    or not secrets.compare_digest(self.headers.get("X-Panel-Token", ""), csrf)):
                self.answer({"error": "Pyyntö estetty"}, 403)
                return
            if self.headers.get("Content-Type") != "application/json":
                self.answer({"error": "Virheellinen sisältötyyppi"}, 415)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise PanelError("Virheellinen viestin koko")
                payload = json.loads(self.rfile.read(size))
                if self.path == "/api/settings":
                    state.configure(payload)
                elif self.path == "/api/children":
                    if payload != {}:
                        raise PanelError("Virheellinen pyyntö")
                    with state.lock:
                        if state.running or state.children_loading:
                            raise PanelError("Alitilejä ei voi hakea toisen selainoperaation aikana")
                        state.children_loading = True
                    try:
                        from .linked_accounts import load_linked_children
                        names = (child_loader or load_linked_children)(directory / "selain", executable)
                    finally:
                        with state.lock:
                            state.children_loading = False
                    self.answer({"children": names})
                    return
                elif self.path in {"/api/start", "/api/pause", "/api/resume", "/api/stop"}:
                    if payload != {}:
                        raise PanelError("Virheellinen pyyntö")
                    action = self.path.removeprefix("/api/")
                    with state.lock:
                        if action == "start":
                            if state.running or state.children_loading or not state.child:
                                raise PanelError("Ajo jo käynnissä, alitilihaku kesken tai lapsen nimi puuttuu")
                            state.running = True
                            state.stop_requested = False
                            state.paused = False
                            state.phase = "valmistellaan"
                            threading.Thread(target=runner or _run, args=(state, directory, executable),
                                             daemon=True, name="uhkone-panel-booking").start()
                        elif action in {"pause", "resume", "stop"}:
                            if not state.running:
                                raise PanelError("Ajo ei ole käynnissä")
                            if action == "stop":
                                state.stop_requested, state.paused = True, False
                            else:
                                state.paused = action == "pause"
                        else:
                            raise PanelError("Tuntematon toiminto")
                else:
                    self.answer({"error": "Ei löytynyt"}, 404)
                    return
                self.answer(state.public())
            except (PanelError, ValueError, UnicodeError):
                self.answer({"error": "Virheelliset asetukset tai pyyntö"}, 400)
            except Exception:  # noqa: BLE001 - Never expose browser/identity details to HTTP clients.
                self.answer({"error": "Alitilien haku ei onnistunut. Tarkista kirjautuminen ja selainikkuna."}, 503)

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        port_holder['value'] = server.server_port
        server.daemon_threads = True
        url = f"http://127.0.0.1:{server.server_port}/"
        print("Paikallinen varauspaneeli:", url, "\nEi maksuja. Sulje Ctrl+C.")
        if on_listening is not None:
            on_listening(url)
        if open_browser:
            webbrowser.open(url)
        try:
            if stop_event is None:
                server.serve_forever(poll_interval=0.25)
            else:
                server.timeout = 0.2
                while not stop_event.is_set():
                    server.handle_request()
        except KeyboardInterrupt:
            with state.lock:
                state.stop_requested = True
            print("Paneeli pysäytetty. Tarkista mahdollinen keskeneräinen varaus.")
