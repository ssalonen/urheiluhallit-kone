"""Finnish command line interface for searching and preparing registrations."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
import re

from .config import ConfigError, load_settings, state_directory
from .models import Course, SearchFilters, filter_courses, normalize_location
from .watch import WatchStateError, detect_changes, load_state, save_state


HELSINKI = ZoneInfo("Europe/Helsinki")
DAYS = ("ma", "ti", "ke", "to", "pe", "la", "su")


def iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Päivä annetaan muodossa VVVV-KK-PP.") from exc


def positive_id(value: str) -> int:
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Kurssitunnuksen pitää olla positiivinen kokonaisluku.")


def course_target(value: str) -> int:
    if value.isdigit():
        return positive_id(value)
    try:
        parsed = urlsplit(value)
        match = re.fullmatch(r"/webshop/reservations2/reservations/9/[^/]+/[^/]+/[^/]+/([0-9]+)/?", parsed.path)
        if (parsed.scheme == "https" and parsed.hostname == "kauppa.urheiluhallit.fi"
                and parsed.port in (None, 443) and not parsed.username and not parsed.password and match):
            return positive_id(match[1])
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Anna yksittäisen kurssin verkkokauppaosoite tai positiivinen kurssitunnus.")


def euro_amount(value: str) -> Decimal:
    try:
        amount = Decimal(value.replace(",", "."))
        if amount.is_finite() and amount >= 0 and amount == amount.quantize(Decimal("0.01")):
            return amount
    except (InvalidOperation, ValueError):
        pass
    raise argparse.ArgumentTypeError("Anna euromäärä, esimerkiksi 76,00.")


def opening_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if "T" not in value and " " not in value:
            raise ValueError("Kellonaika puuttuu")
        return parsed.replace(tzinfo=HELSINKI) if parsed.tzinfo is None else parsed.astimezone(HELSINKI)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Anna päivä ja kellonaika, esimerkiksi 2027-01-12T07:00:00+02:00.") from exc


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uhkone", description=(
        "Hae leikki-ikäisten jatkouimakoulut ja varaa valittu kurssi ilmoittautumisen auetessa. "
        "Oletustoiveet: Mäkelänrinne, sitten Kallio."
    ))
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--asetukset", type=Path, help="TOML-tiedosto; oletuksena ./asetukset.toml")
    common.add_argument("--tilahakemisto", type=Path, default=None, help="Vahdin ja selainprofiilin paikallinen hakemisto")
    search = argparse.ArgumentParser(add_help=False)
    search.add_argument("--halli", action="append", help="Rajaa halliin; voi antaa useasti toivejärjestyksessä")
    search.add_argument("--paiva", type=int, choices=range(1, 8), action="append", metavar="1–7", help="Sallittu viikonpäivä (ma=1); voi antaa useasti")
    search.add_argument("--aikaisintaan", metavar="HH:MM", help="Aikaisin kurssin aloituskellonaika")
    search.add_argument("--arki-aikaisintaan", metavar="HH:MM", help="Aikaisin aloitus ma–pe; oletus 17:30")
    search.add_argument("--viikonloppu-aikaisintaan", metavar="HH:MM", help="Aikaisin aloitus la–su; oletus 09:30")
    search.add_argument("--viimeistaan", metavar="HH:MM", help="Myöhäisin kurssin aloituskellonaika")
    search.add_argument("--alkaen", type=iso_date, help="Ensimmäinen sallittu aloituspäivä; oletuksena tänään")
    search.add_argument("--asti", type=iso_date, help="Viimeinen sallittu aloituspäivä")
    search.add_argument("--vapaat", action="store_true", help="Näytä vain nyt varattavat kurssit, joilla on vapaita paikkoja")
    commands = p.add_subparsers(dest="command", required=True)
    guided = commands.add_parser("ohjattu", parents=[common, search], help="Valitse tehtävä ja käy varausasetukset läpi keskustellen")
    guided.add_argument("--selain", help="Chrome/Chromium-ohjelman polku")
    find = commands.add_parser("haku", parents=[common, search], help="Hae oikeat kurssit verkkokaupasta")
    find.add_argument("--json", action="store_true", help="Tulosta koneluettava JSON")
    watch = commands.add_parser("vahti", parents=[common, search], help="Seuraa uusia kursseja ja avautuvia paikkoja")
    watch.add_argument("--vali", type=int, help="Tarkistusväli sekunteina, vähintään 60")
    watch.add_argument("--kerran", action="store_true", help="Tee yksi tarkistus ja tallenna tila")
    watch.add_argument("--alusta", action="store_true", help="Aloita uusi lähtötilanne nykyisistä kursseista")
    course = commands.add_parser("kurssi", parents=[common], help="Näytä yksittäisen kurssin ajantasaiset tiedot")
    course.add_argument("id", type=positive_id)
    enroll = commands.add_parser("ilmoittaudu", parents=[common, search], help="Valitse kurssi ja valmistele ilmoittautuminen selaimessa")
    enroll.add_argument("id", type=positive_id, nargs="?", help="Haku-komennon näyttämä kurssitunnus")
    enroll.add_argument("--lapsi", help="Valitse tililtä olemassa oleva lapsi tarkalla nimellä (ei tallenneta asetuksiin)")
    enroll.add_argument("--selain", help="Chrome/Chromium-ohjelman polku; muuten Playwrightin Chromium")
    book = commands.add_parser("varaa", parents=[common, search], help="Odota valitun kurssin avautumista ja jatka varaus vahvistukseen")
    book.add_argument("kohde", type=course_target, help="Etukäteen valitun kurssin osoite tai tunnus")
    book.add_argument("--lapsi", required=True, help="Olemassa olevan lapsitilin tarkka nimi")
    book.add_argument("--enintaan", type=euro_amount, required=True, help="Kurssista sallittu enimmäissumma euroina")
    book.add_argument("--maksutapa", choices=("saldo", "fastpay", "verkkomaksu"), required=True, help="Saldomaksu, tallennetun kortin FastPay tai verkkopankki/kortti")
    book.add_argument("--maksutunnus", type=positive_id, help="Kaupan maksutapatunnus, jos verkkokaupassa on useita vaihtoehtoja")
    book.add_argument("--avautuu", type=opening_time, help="Ilmoittautumisen alkamisaika; oletuksena kurssin julkaisema aika")
    book.add_argument("--kesto", type=int, default=30, help="Varausyrityksen kesto avautumisesta minuutteina (oletus 30)")
    book.add_argument("--selain", help="Chrome/Chromium-ohjelman polku")
    race = commands.add_parser("varaa-yksi", parents=[common, search], help="Seuraa useita valittuja kursseja ja varaa lapselle yksi")
    race.add_argument("kohteet", nargs="+", type=course_target, help="Hyväksyttyjen kurssien osoitteet tai tunnukset")
    race.add_argument("--lapsi", required=True, help="Olemassa olevan lapsitilin tarkka nimi")
    race.add_argument("--enintaan", type=euro_amount, required=True, help="Yhden kurssin enimmäissumma euroina")
    race.add_argument("--maksutapa", choices=("saldo", "fastpay", "verkkomaksu"), required=True)
    race.add_argument("--maksutunnus", type=positive_id, help="Kaupan maksutapatunnus")
    race.add_argument("--avautuu", type=opening_time, help="Ilmoittautumisen alkuaika, jos yhteinen aika on tiedossa")
    race.add_argument("--kesto", type=int, default=30, help="Yrityksen kesto kunkin kurssin avautumisesta minuutteina")
    race.add_argument("--selain", help="Chrome/Chromium-ohjelman polku")
    for booking_command in (book, race):
        queue_choice = booking_command.add_mutually_exclusive_group()
        queue_choice.add_argument("--jono", dest="jono", action="store_true", help="Liity sopiviin jonoihin ja jatka yhden varsinaisen paikan hakua")
        queue_choice.add_argument("--ei-jono", dest="jono", action="store_false", help="Tavoittele vain varsinaista kurssipaikkaa")
        booking_command.set_defaults(jono=None)
        booking_command.add_argument("--jono-aikaraja", type=int, default=60, help=argparse.SUPPRESS)
    login = commands.add_parser("kirjaudu", parents=[common], help="Kirjaudu paikalliseen selainprofiiliin")
    login.add_argument("--selain", help="Chrome/Chromium-ohjelman polku")
    probe = commands.add_parser("istuntotesti", parents=[common], help="Vertaa kahden kirjautuneen selainprofiilin ostoskoria lukemalla")
    probe.add_argument("--selain", help="Chrome/Chromium-ohjelman polku")
    probe.add_argument("--raportti", type=Path, help="Tallenna vertailuraportti ilman henkilötietoja")
    probe.add_argument("--odotus", type=int, default=600, help="Kirjautumisen odotus kummassakin profiilissa sekunteina")
    simulation = argparse.ArgumentParser(add_help=False)
    simulation.add_argument("--data", type=Path, help="Julkisesta kurssidatasta tehty testidatatiedosto")
    simulation.add_argument("--kurssi", type=positive_id, help="Testattava kurssitunnus testidatasta")
    simulation.add_argument("--skenaario", default="normaali", choices=("normaali", "ruuhka", "varaus-vastaus-katoaa", "maksu-vastaus-katoaa", "viimeinen-paikka", "taynna", "fastpay-kortti-puuttuu", "fastpay-hylatty", "ensisijainen-taynna", "jono", "jono-vastaus-katoaa", "jono-taynna"))
    simulation.add_argument("--avautuu-sekunnissa", type=float, default=5, help="Paikallisen ilmoittautumisen viive sekunteina")
    demo = commands.add_parser("koeajo", parents=[common, simulation], help="Aja koko osto paikallisessa testikaupassa")
    demo.add_argument("--maksutapa", choices=("saldo", "fastpay"), default="saldo", help="Simuloitu saldomaksu tai tallennetun testikortin FastPay")
    demo.add_argument("--monta", type=int, default=1, help="Tavoittele näin montaa sopivaa kurssia; osta yksi")
    demo.add_argument("--jono", action="store_true", help="Liity testissä useisiin jonoihin varsinaisen paikan haun aikana")
    demo.add_argument("--jono-aikaraja", type=int, default=15, help=argparse.SUPPRESS)
    demo.add_argument("--headless", action="store_true", help="Testaa ilman näkyvää ikkunaa; vain paikallinen testikauppa")
    demo.add_argument("--selain", help="Chrome/Chromium-ohjelman polku")
    demo.add_argument("--aikaraja", type=int, default=30, help="Testin aikaraja avautumisesta sekunteina")
    demo.add_argument("--raportti", type=Path, help="Tallenna koeajon tulos JSON-tiedostoon")
    for controlled_command in (book, race, demo):
        controlled_command.add_argument("--keskustele", action="store_true", help="Näytä eteneminen ja salli ohjauskomennot päätteellä (myös oletustoiminta)")
        controlled_command.add_argument("--kysy-toiminnot", action="store_true", help="Pyydä hyväksyntä ennen varausta, maksua ja jonoon liittymistä; tarvitsee päätteen")
    shop = commands.add_parser("testikauppa", parents=[common, simulation], help="Käynnistä paikallinen testikauppa käsin kokeiltavaksi")
    shop.add_argument("--tietokanta", type=Path, help="Paikallinen SQLite-tiedosto; muuten vain muistissa")
    refresh = commands.add_parser("paivita-testidata", parents=[common], help="Päivitä testidata kiinteästä julkisesta GET-rajapinnasta")
    refresh.add_argument("--tiedosto", type=Path, help="Testidatatiedoston kohdepolku")
    return p


def get_filters(args: argparse.Namespace, configured: SearchFilters) -> SearchFilters:
    changes = {"date_from": configured.date_from or datetime.now(HELSINKI).date()}
    for arg, field in (("halli", "locations"), ("paiva", "weekdays"),
                       ("aikaisintaan", "earliest_time"), ("viimeistaan", "latest_time"),
                       ("arki_aikaisintaan", "weekday_earliest_time"), ("viikonloppu_aikaisintaan", "weekend_earliest_time"),
                       ("alkaen", "date_from"), ("asti", "date_to")):
        value = getattr(args, arg, None)
        if value is not None:
            changes[field] = tuple(value) if arg in {"halli", "paiva"} else value
    if getattr(args, "vapaat", False):
        changes["available_only"] = True
    return replace(configured, **changes)


def ranked(courses: list[Course], filters: SearchFilters) -> list[Course]:
    preferences = {}
    for rank, name in enumerate(filters.locations):
        preferences.setdefault(normalize_location(name), rank)
    return sorted(filter_courses(courses, filters), key=lambda course: (
        preferences.get(normalize_location(course.location), len(preferences)),
        course.start.isoformat() if course.start else "9999", course.time_start, course.id,
    ))


def course_json(course: Course) -> dict:
    data = asdict(course)
    data.pop("raw", None)
    data["available"] = course.available
    data["queue_available"] = course.queue_available
    return data


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Tuntematon tietotyyppi: {type(value).__name__}")


def status(course: Course) -> str:
    if course.available:
        return f"{course.free_places} vapaata"
    if course.registration_start and course.registration_start > datetime.now(HELSINKI):
        return "Ilm. alkaa " + course.registration_start.astimezone(HELSINKI).strftime("%d.%m.%Y %H:%M")
    if course.queue_available:
        return f"Täynnä, {course.queue_places} jonopaikkaa"
    if course.free_places == 0:
        return "Täynnä"
    if not course.is_reservable:
        return "Ei nyt varattavissa"
    return "Paikkatieto puuttuu"


def print_courses(courses: list[Course], *, details: bool = False) -> None:
    if not courses:
        print("Sopivia julkaistuja kursseja ei löytynyt. Tulevia julkaisuja voi seurata komennolla: uhkone vahti")
        return
    print(f"{'TUNNUS':<9} {'HALLI':<14} {'ALKAA':<12} {'PV':<8} {'KLO':<13} {'HINTA':<10} TILA")
    for course in courses:
        first = course.start.strftime("%d.%m.%Y") if course.start else "Ei tiedossa"
        days = ",".join(DAYS[d - 1] for d in course.weekdays if d in range(1, 8)) or "–"
        times = f"{course.time_start}–{course.time_end}" if course.time_start else "Ei tiedossa"
        price = f"{course.price:.2f}".replace(".", ",") + " €" if course.price is not None else "Ei tiedossa"
        print(f"{course.id:<9} {course.location:<14} {first:<12} {days:<8} {times:<13} {price:<10} {status(course)}")
        if details:
            period = f" → {course.end.strftime('%d.%m.%Y')}" if course.end else ""
            print(f"  {course.name}")
            if period:
                print(f"  Jakso: {first}{period}")
            print(f"  {course.url}")


def filter_fingerprint(filters: SearchFilters) -> str:
    data = asdict(filters)
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     default=json_default).encode()).hexdigest()[:16]


def run_watch(args, settings, filters, directory: Path, client) -> int:
    interval = args.vali if args.vali is not None else settings.interval
    if interval < 60:
        raise ConfigError("Tarkistusvälin pitää olla vähintään 60 sekuntia.")
    # Keep explicit date restrictions, but not today's rolling default, in scope.
    scope = replace(filters, date_from=args.alkaen or settings.filters.date_from)
    state_path = directory / f"vahti-{filter_fingerprint(scope)}.json"
    state = {"version": 1, "courses": {}} if args.alusta else load_state(state_path)
    first = "checked_at" not in state
    print("Julkaisu- ja paikkavahti: " + (" → ".join(filters.locations) or "kaikki hallit"))
    print(f"Tarkistusväli {interval} s. Tila: {state_path}\nLopeta Ctrl+C. Vahti ei tee varauksia.", flush=True)
    failures = 0
    while True:
        try:
            from .client import ClientError
            # Do not alert for a course whose first meeting has passed overnight.
            current_filters = replace(filters, date_from=max(filters.date_from, datetime.now(HELSINKI).date()))
            observed = ranked(client.courses(), replace(current_filters, available_only=False))
            courses = [c for c in observed if c.available or not current_filters.available_only]
            events, next_state = detect_changes(observed, state, baseline=first)
            if current_filters.available_only:
                events = [(kind, c) for kind, c in events if c.available]
            save_state(state_path, next_state)
            state = next_state
            stamp = datetime.now(HELSINKI).strftime("%d.%m.%Y %H:%M:%S")
            if first:
                print(f"\n[{stamp}] Lähtötilanne ({len(courses)} kurssia):")
                print_courses(courses, details=True)
                first = False
            elif events:
                for kind, course in events:
                    print(f"\a\n[{stamp}] {'UUSI KURSSI' if kind == 'uusi' else 'PAIKKA AVAUTUNUT'}", flush=True)
                    print_courses([course], details=True)
                    print(f"Valmistele valittu kurssi: uhkone ilmoittaudu {course.id}")
            else:
                print(f"[{stamp}] Ei uusia kursseja tai avautuneita paikkoja ({len(courses)} osumaa).", flush=True)
            failures = 0
        except ClientError as exc:
            failures += 1
            print(f"Haun virhe: {exc}. Edellinen seurantatila säilyy.", file=sys.stderr, flush=True)
            if args.kerran:
                return 1
        if args.kerran:
            return 0
        # Back off on outages; each wait remains interruptible.
        time.sleep(min(interval * (2 ** min(failures, 4)), 3600))


def select_course(courses: list[Course]) -> Course | None:
    available = [course for course in courses if course.available]
    print_courses(courses, details=True)
    if not available:
        print("Yhtään hakuehtoihin sopivaa kurssia ei voi nyt valmistella. Käytä julkaisuvahtia.")
        return None
    if not sys.stdin.isatty():
        raise ConfigError("Anna kurssitunnus: uhkone ilmoittaudu TUNNUS")
    selected = input("\nValitse kurssitunnus (tyhjä peruu): ").strip()
    if not selected:
        return None
    for course in available:
        if str(course.id) == selected:
            return course
    raise ConfigError("Valitse listassa oleva nyt varattava kurssi.")


def _check_terminal(args) -> None:
    if getattr(args, "kysy_toiminnot", False) and not sys.stdin.isatty():
        raise ConfigError("Vaiheittainen hyväksyntä tarvitsee vuorovaikutteisen päätteen.")


def controlled_race(specs, pool, directory: Path, args):
    """Keep all browser work on the main thread and expose only public status."""
    from .console import TerminalSession
    from .multi import run_race
    courses = {spec.course.id: spec.course for spec in specs}
    wait = pool.wait
    with TerminalSession(ask_before_actions=getattr(args, "kysy_toiminnot", False)) as terminal:
        previous: dict[int, tuple] = {}

        def progress(phase: str, metadata: dict) -> None:
            course_id = metadata.get("course_id")
            if phase == "candidate":
                # Polling many tabs must not flood the console with identical
                # catalogue observations. A heartbeat still reports progress.
                flags = tuple(bool(metadata.get(key)) for key in (
                    "available", "held", "confirmed", "payment_locked", "queue_available", "queued"))
                if previous.get(course_id) == flags:
                    return
                previous[course_id] = flags
                if not any(flags[:4]):
                    return
            terminal.event(phase, courses.get(course_id))

        return run_race(
            specs, pool, directory, allow_queue=args.jono,
            queue_timeout=args.jono_aikaraja, queue_policy="eager",
            log=lambda _message: None, progress=progress,
            checkpoint=lambda: terminal.checkpoint(wait),
            before_action=(lambda action, course_id: terminal.before_action(
                action, courses.get(course_id), wait=wait)) if terminal.ask_before_actions else None,
        )


def run_reservation(args, filters: SearchFilters, directory: Path, client) -> int:
    from .booking import BookingSpec
    from .booking_browser import open_booking_browser
    from .origin import PRODUCTION_URL
    _check_terminal(args)
    if not args.lapsi.strip():
        raise ConfigError("Lapsen tarkka nimi tarvitaan.")
    if not 1 <= args.kesto <= 240:
        raise ConfigError("Keston pitää olla 1–240 minuuttia.")
    if not 1 <= args.jono_aikaraja <= 300:
        raise ConfigError("Jonovaiheen lisäajan pitää olla 1–300 sekuntia.")
    targets = list(dict.fromkeys(args.kohteet if args.command == "varaa-yksi" else [args.kohde]))
    if len(targets) > 12:
        raise ConfigError("Yhdessä varausajossa voi tavoitella enintään 12 valittua kurssia.")
    selected = []
    for target in targets:
        course = client.get_course(target)
        if course.id != target or not replace(filters, available_only=False).matches(course):
            raise ConfigError(f"Kurssi {target} ei vastaa hakuehtoja. Tarkista halli, kurssitaso, päivät ja kellonajat.")
        if course.price is None or course.price > args.enintaan:
            raise ConfigError(f"Kurssin {target} hinta puuttuu tai ylittää enimmäissumman.")
        selected.append(course)
    selected = ranked(selected, replace(filters, available_only=False))
    now = datetime.now(HELSINKI)
    specs = []
    for course in selected:
        opens = args.avautuu or course.registration_start
        if args.avautuu and course.registration_start and args.avautuu < course.registration_start:
            raise ConfigError("Annettu alkamisaika on aikaisempi kuin kurssin julkaisema ilmoittautumisaika.")
        if opens is None:
            raise ConfigError(f"Kurssin {course.id} ilmoittautumisen alkuaika puuttuu. Anna tiedossa oleva aika --avautuu-valitsimella.")
        specs.append(BookingSpec(course=course, child_name=args.lapsi.strip(), max_price=args.enintaan,
                                opens_at=opens, deadline=max(opens, now) + timedelta(minutes=args.kesto),
                                payment_mode=args.maksutapa, payment_type_id=args.maksutunnus))
    print_courses(selected, details=True)
    print(f"Tavoitteena yksi kurssi {len(specs)} vaihtoehdosta. Enimmäishinta {args.enintaan} €. Maksutapa: {args.maksutapa}.", flush=True)
    print("Samalla tarkistuksella valmiista vaihtoehdoista valitaan hallien toivejärjestyksessä. Muulloin ensimmäinen valmis polku saa varausvuoron.", flush=True)
    if args.jono:
        print("Jonotus: liity kaikkiin valittuihin sopiviin jonoihin heti kun mahdollista. Varsinaisen paikan haku jatkuu; maksetaan enintään yksi kurssi.", flush=True)
    print("Avaan näkyvän selaimen. Kirjaudu tarvittaessa itse. Pidä sama selainprofiili käytössä myös jatkaessasi keskeytynyttä ajoa.", flush=True)

    def report(pool):
        result = controlled_race(specs, pool, directory, args)
        queues = getattr(result, "queue_reservations", ())
        pending = getattr(result, "queue_pending_ids", ())
        if queues:
            print(f"JONOPAIKKA VAHVISTETTU {len(queues)} kurssille: " + ", ".join(str(item[0]) for item in queues) + ".")
            print("Jonot jätetään voimaan. Vapautuneista paikoista ilmoitetaan sähköpostitse; jonotus ei ole maksettu kurssipaikka.")
        if pending:
            print("Jonopyyntö on voinut mennä perille ilman vahvistusta kursseille: " + ", ".join(map(str, pending)) + ". Näitä pyyntöjä ei toisteta.", file=sys.stderr)
        if result.status in {"confirmed", "already_confirmed"}:
            print(f"VARAUS VAHVISTETTU. Kurssi: {result.course_id}. Varaustunnus: {result.reservation_id or 'ei erillistä tunnusta'}")
            if result.receipt_id:
                print(f"Kuitti: {result.receipt_id}")
            return 0
        if result.status == "queued":
            if not queues:
                print(f"JONOPAIKKA VAHVISTETTU. Kurssi: {result.course_id}. Jonovaraus: {result.queue_reservation_id}.")
                if result.queue_position is not None:
                    print(f"Oma jonosija: {result.queue_position}.")
            print("Varsinaista kurssipaikkaa tai maksua ei vahvistettu. Kaupan ohjeen mukaan vapautuneesta paikasta ilmoitetaan sähköpostitse jonottajille.")
            return 1 if pending else 0
        if result.status == "queue_pending":
            print("Jonopyyntö on voinut mennä perille, mutta vahvistusta ei saatu. Jatka samalla komennolla; älä lähetä uutta jonopyyntöä.", file=sys.stderr)
            return 1
        print("Varausta ei ole vahvistettu. Tarkista tallennettu tila ja asiakastilin varaukset.", file=sys.stderr)
        return 1

    if len(specs) == 1:
        with open_booking_browser(specs[0], directory / "selain", executable_path=args.selain) as backend:
            class SinglePool:
                identity = PRODUCTION_URL + "|" + backend.identity.rsplit(":", 1)[0]
                base_url = PRODUCTION_URL
                backends = {specs[0].course.id: backend}
                def observations(self):
                    return {specs[0].course.id: backend.snapshot()}
                def activate(self, course_id):
                    if course_id != specs[0].course.id:
                        raise ConfigError("Väärän kurssin varausvuoro estettiin.")
                    return backend
                def wait(self, seconds):
                    backend.wait(seconds)
                def activate_queue(self, course_id):
                    return self.activate(course_id)
            return report(SinglePool())
    from .multi_browser import open_booking_pool
    with open_booking_pool(specs, directory / "selain", executable_path=args.selain) as pool:
        return report(pool)


def run_test_shop(args, settings) -> int:
    from .seed import load_seed
    from .client import ShopClient, parse_course, ClientError
    from .testshop import TestShop
    _check_terminal(args)
    document = load_seed(args.data)
    rows = document["courses"]
    count = getattr(args, "monta", 1)
    if not 1 <= count <= 12:
        raise ConfigError("Koeajossa voi tavoitella 1–12 kurssia.")
    candidates = ranked([parse_course(row) for row in rows], replace(settings.filters, available_only=False))
    if args.kurssi:
        chosen = next((row for row in rows if row["reservation_event_group_id"] == args.kurssi), None)
        if chosen is None:
            raise ConfigError("Kurssitunnusta ei löytynyt testidatasta.")
    else:
        if not candidates:
            raise ConfigError("Testidatasta ei löytynyt hakutoiveisiin sopivaa kurssia. Anna --kurssi.")
        chosen = next(row for row in rows if row["reservation_event_group_id"] == candidates[0].id)
    candidate_ids = [chosen["reservation_event_group_id"]]
    candidate_ids += [course.id for course in candidates if course.id not in candidate_ids][:count - 1]
    if len(candidate_ids) != count:
        raise ConfigError("Testiaineistossa ei ole pyydettyä määrää sopivia kursseja.")
    by_id = {row["reservation_event_group_id"]: row for row in rows}
    rows = [by_id[course_id] for course_id in candidate_ids] + [row for row in rows if row["reservation_event_group_id"] not in candidate_ids]
    if not 0 <= args.avautuu_sekunnissa <= 3600:
        raise ConfigError("Testikaupan avautumisviiveen pitää olla 0–3600 sekuntia.")
    if args.command == "koeajo" and args.skenaario.startswith("fastpay-") and args.maksutapa != "fastpay":
        raise ConfigError("FastPay-skenaario tarvitsee valinnan --maksutapa fastpay.")
    if args.command == "koeajo" and args.skenaario == "ensisijainen-taynna" and count < 2:
        raise ConfigError("Vaihtoehdon voittaminen tarvitsee vähintään kaksi kurssia: --monta 2.")
    print("TESTIKAUPPA — paikallinen simulaattori. Vain testihenkilöitä ja simuloituja maksuja.", flush=True)
    print(f"Kurssidatan lähde: {document['source_url']}\nTallennettu: {document['fetched_at']} ({len(rows)} kurssia)")
    db = str(args.tietokanta) if getattr(args, "tietokanta", None) else ":memory:"
    with TestShop(rows, opens_in=args.avautuu_sekunnissa, scenario=args.skenaario, db_path=db) as server:
        print(f"Paikallinen osoite: {server.base_url}/webshop/reservations2/", flush=True)
        if args.command == "testikauppa":
            print("Testihuoltaja: 10. Testilapsi: 20. Maksaminen on simuloitua. Lopeta Ctrl+C.", flush=True)
            server.serve_forever()
            return 0
        if not 1 <= args.aikaraja <= 300:
            raise ConfigError("Koeajon aikarajan pitää olla 1–300 sekuntia.")
        if not 1 <= args.jono_aikaraja <= 300:
            raise ConfigError("Jonovaiheen lisäajan pitää olla 1–300 sekuntia.")
        from .booking import BookingSpec
        from tempfile import TemporaryDirectory
        with ShopClient(base_url=server.base_url) as client:
            courses = []
            for course_id in candidate_ids:
                for attempt in range(6):
                    try:
                        courses.append(client.get_course(course_id))
                        break
                    except ClientError:
                        if attempt == 5:
                            raise
                        print("Testikaupan alustava lukupyyntö epäonnistui; uusi yritys 1 s kuluttua.", flush=True)
                        time.sleep(1)
        if any(course.price is None for course in courses):
            raise ConfigError("Testikurssin hinta puuttuu.")
        specs = [BookingSpec(course=course, child_name="Testilapsi", max_price=course.price,
                             opens_at=server.opens_at, deadline=max(server.opens_at, datetime.now(HELSINKI)) + timedelta(seconds=args.aikaraja),
                             payment_mode=args.maksutapa, payment_type_id=5 if args.maksutapa == "fastpay" else 4) for course in courses]
        started = datetime.now(HELSINKI)
        with TemporaryDirectory(prefix="uhkone-koeajo-") as tmp:
            from .multi_browser import open_booking_pool
            with open_booking_pool(specs, Path(tmp) / "testiselain", executable_path=args.selain,
                                   base_url=server.base_url, headless=args.headless) as pool:
                result = controlled_race(specs, pool, Path(tmp) / "yhteinen-tila", args)
            report = {"test_only": True, "real_payment": False, "scenario": args.skenaario, "payment_mode": args.maksutapa,
                      "source_url": document["source_url"], "data_fetched_at": document["fetched_at"],
                      "started_at": started.isoformat(), "finished_at": datetime.now(HELSINKI).isoformat(),
                      "outcome": result.status, "course_id": getattr(result, "course_id", server.course_id),
                      "candidate_ids": candidate_ids, "candidate_pages": len(specs),
                      "reservation_id": result.reservation_id, "receipt_id": result.receipt_id,
                      "queue_fallback": args.jono, "queue_reservation_id": getattr(result, "queue_reservation_id", None),
                      "queue_position": getattr(result, "queue_position", None),
                      "queue_policy": "eager", "queue_reservations": result.queue_reservations,
                      "queue_pending_ids": result.queue_pending_ids,
                      "server": server.stats()}
            success = (result.status == "confirmed" and report["server"]["checkout_posts"] == 1
                       and report["server"]["confirmed"] == 1 and report["server"]["receipts"] == 1)
            if args.skenaario == "taynna":
                success = result.status == "deadline" and report["server"]["writes"] == 0
            if args.skenaario == "fastpay-hylatty":
                success = (result.status == "deadline" and report["server"]["checkout_posts"] == 1
                           and report["server"]["confirmed"] == 0 and report["server"]["receipts"] == 0
                           and report["server"]["fake_card_charges"] == 0)
            if args.maksutapa == "fastpay" and result.status == "confirmed":
                success = (success and report["server"]["fake_card_charges"] == 1
                           and report["server"]["balance_cents"] == 100000)
            if args.skenaario in {"jono", "jono-vastaus-katoaa"}:
                if args.jono:
                    success = (result.status == "queued" and report["server"]["queue_posts"] == count
                               and report["server"]["queued"] == count and not result.queue_pending_ids
                               and len(result.queue_reservations) == count and report["server"]["cart_posts"] == 0
                               and report["server"]["checkout_posts"] == 0 and report["server"]["receipts"] == 0
                               and report["server"]["confirmed"] == 0)
                else:
                    success = result.status == "deadline" and report["server"]["writes"] == 0
            if args.skenaario == "jono-taynna":
                success = result.status == "deadline" and report["server"]["writes"] == 0
            if args.raportti:
                args.raportti.parent.mkdir(parents=True, exist_ok=True)
                args.raportti.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"Testiraportti: {args.raportti}")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print("KOEAJO ONNISTUI — ei oikeaa maksua." if success else "Koeajo ei vahvistanut odotettua lopputulosta.")
            return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if not actual_argv and sys.stdin.isatty():
        actual_argv = ["ohjattu"]
    args = parser().parse_args(actual_argv)
    try:
        settings = load_settings(args.asetukset)
        directory = (args.tilahakemisto or state_directory()).expanduser().resolve()
        if args.command == "ohjattu":
            from .client import ShopClient
            from .wizard import run_wizard

            def reserve(selected_args, selected_settings, selected_directory):
                if selected_args.jono is None:
                    selected_args.jono = selected_settings.queue_fallback
                with ShopClient() as client:
                    return run_reservation(selected_args, get_filters(selected_args, selected_settings.filters),
                                           selected_directory, client)

            def login(selected_args, selected_directory):
                from .browser import login as browser_login
                browser_login(selected_directory / "selain", executable_path=selected_args.selain)
                return 0

            return run_wizard(args, settings, directory, parse_args=parser().parse_args,
                              run_reservation=reserve,
                              run_test_shop=lambda demo_args, demo_settings, _directory: run_test_shop(demo_args, demo_settings),
                              run_login=login, client_factory=ShopClient)
        if args.command in {"testikauppa", "koeajo"}:
            return run_test_shop(args, settings)
        if args.command == "istuntotesti":
            if not 1 <= args.odotus <= 1800:
                raise ConfigError("Kirjautumisen odotuksen pitää olla 1–1800 sekuntia.")
            from .session_probe import inspect_sessions
            report = inspect_sessions(directory / "istuntotesti", executable_path=args.selain,
                                      report_path=args.raportti, login_timeout_seconds=args.odotus)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.command == "paivita-testidata":
            from .seed import DEFAULT_SEED, update_seed
            path = args.tiedosto or DEFAULT_SEED
            document = update_seed(path)
            print(f"Tallennettu {len(document['courses'])} julkista kurssia: {path}")
            return 0
        if args.command == "kirjaudu":
            from .browser import login
            login(directory / "selain", executable_path=args.selain)
            return 0
        from .client import ShopClient, ClientError
        with ShopClient() as client:
            if args.command == "kurssi":
                print_courses([client.get_course(args.id)], details=True)
                return 0
            filters = get_filters(args, settings.filters)
            if args.command == "vahti":
                return run_watch(args, settings, filters, directory, client)
            if args.command in {"varaa", "varaa-yksi"}:
                if args.jono is None:
                    args.jono = settings.queue_fallback
                return run_reservation(args, filters, directory, client)
            if args.command == "haku":
                courses = ranked(client.courses(), filters)
                if args.json:
                    print(json.dumps({"checked_at": datetime.now(HELSINKI).isoformat(),
                                      "courses": [course_json(c) for c in courses]},
                                     ensure_ascii=False, indent=2, default=json_default))
                else:
                    print("Toivejärjestys: " + (" → ".join(filters.locations) or "kaikki hallit"))
                    print_courses(courses, details=True)
                return 0
            if args.id is None:
                course = select_course(ranked(client.courses(), filters))
                if course is None:
                    return 0
            else:
                course = client.get_course(args.id)
                if course.id != args.id:
                    raise ConfigError("Verkkokauppa palautti eri kurssin kuin pyydettiin. Valmistelu keskeytettiin.")
            if not filters.matches(course):
                raise ConfigError("Kurssi ei vastaa hakuehtoja. Tarkista halli, taso ja aloituspäivä haku-komennolla.")
            # Refetch immediately before opening the browser. No silent substitution.
            selected_id = course.id
            course = client.get_course(selected_id)
            if course.id != selected_id or not filters.matches(course) or not course.available:
                raise ConfigError("Valittu kurssi ei ole nyt varattavissa tai se ei enää vastaa hakuehtoja.")
            print_courses([course], details=True)
        from .browser import prepare_course
        prepare_course(course, directory / "selain", child_name=args.lapsi, executable_path=args.selain)
        return 0
    except KeyboardInterrupt:
        print("\nKeskeytetty.", file=sys.stderr)
        if args.command in {"varaa", "varaa-yksi", "ohjattu", "koeajo"}:
            print("Jatka samalla varaa-komennolla. Tallennettua etenemistilaa käytetään varauksen ja maksun tarkistamiseen.", file=sys.stderr)
        return 130
    except (ConfigError, WatchStateError, ValueError, OSError) as exc:
        print(f"Virhe: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Expected client/browser failures use these named exception classes.
        if exc.__class__.__name__ in {"ClientError", "BrowserError", "BookingError", "TransientBookingError", "ActionNotSent", "ActionUncertain"}:
            print(f"Virhe: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
