"""Interactive setup for one reviewed, visible-browser booking run.

Only public catalogue data is printed. The participant name remains an
in-memory argument to the existing runner; this module never writes files or
prints a command containing personal information.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from .config import ConfigError, Settings
from .models import Course


class _Cancelled(Exception):
    pass


_CANCEL = {"peru", "peruuta", "lopeta", "q", "exit"}


class _Conversation:
    def __init__(self, input_fn: Callable[[], str], output: TextIO):
        self.input = input_fn
        self.output = output

    def say(self, text: str = "") -> None:
        print(text, file=self.output, flush=True)

    def ask(self, prompt: str, *, default: str | None = None) -> str:
        print(prompt, end=" ", file=self.output, flush=True)
        try:
            value = self.input().strip()
        except EOFError as exc:
            raise _Cancelled from exc
        if value.casefold() in _CANCEL:
            raise _Cancelled
        if not value:
            if default is None:
                raise _Cancelled
            return default
        return value

    def choice(self, prompt: str, choices: dict[str, str], *, default: str | None = None) -> str:
        while True:
            value = self.ask(prompt, default=default).casefold()
            if value in choices:
                return choices[value]
            self.say("Valitse jokin annetuista vaihtoehdoista. Peruuta kirjoittamalla peruuta.")

    def show_courses(self, courses: list[Course]) -> None:
        from .cli import print_courses
        with redirect_stdout(self.output):
            print_courses(courses, details=True)


def _inherited_options(args, directory: Path, *, include_search: bool) -> list[str]:
    result = ["--tilahakemisto", str(directory)]
    for field in ("asetukset", "selain"):
        value = getattr(args, field, None)
        if value is not None:
            result.extend(("--" + field, str(value)))
    if include_search:
        for field in ("halli", "paiva"):
            for value in getattr(args, field, None) or []:
                result.extend(("--" + field, str(value)))
        for field in ("aikaisintaan", "arki_aikaisintaan", "viikonloppu_aikaisintaan", "viimeistaan", "alkaen", "asti"):
            value = getattr(args, field, None)
            if value is not None:
                result.extend(("--" + field.replace("_", "-"), str(value)))
    return result


def _targets(value: str) -> list[int]:
    from .cli import course_target
    tokens = []
    for token in value.split():
        if token.startswith(("https://", "http://")):
            # Hall selectors in valid shop URLs can contain commas (1,2).
            tokens.append(token.rstrip(";,"))
        else:
            tokens.extend(part for part in re.split(r"[,;]", token) if part)
    result = list(dict.fromkeys(course_target(token) for token in tokens))
    if not 1 <= len(result) <= 12:
        raise argparse.ArgumentTypeError("Valitse 1–12 kurssia.")
    return result


def _select_courses(conversation: _Conversation, args, settings: Settings, client_factory, *, search: bool) -> list[Course]:
    from .cli import get_filters, ranked
    filters = replace(get_filters(args, settings.filters), available_only=False)
    with client_factory() as client:
        if search:
            conversation.say("Haen julkaistut kurssit. Tässä vaiheessa luetaan vain julkisia tietoja.")
            conversation.say("Hallien toivejärjestys: " + (" → ".join(filters.locations) or "kaikki hallit"))
            conversation.say(
                f"Aikaisin aloitus arkisin {filters.weekday_earliest_time or 'ei rajaa'}, "
                f"viikonloppuisin {filters.weekend_earliest_time or 'ei rajaa'}."
            )
            available = ranked(client.courses(), filters)
            conversation.show_courses(available)
            if not available:
                return []
            by_id = {course.id: course for course in available}
            while True:
                selected = conversation.ask("Anna hyväksymiesi kurssien TUNNUKSET välilyönnein (tai kaikki; tyhjä peruu):")
                try:
                    identifiers = list(by_id) if selected.casefold() == "kaikki" else _targets(selected)
                    if not 1 <= len(identifiers) <= 12 or any(course_id not in by_id for course_id in identifiers):
                        raise argparse.ArgumentTypeError("Valitse 1–12 näytetyistä kursseista.")
                except argparse.ArgumentTypeError:
                    conversation.say("Valitse 1–12 yllä näkyvää kurssitunnusta. Valintaa ei tehty.")
                    continue
                return ranked([by_id[course_id] for course_id in identifiers], filters)
        while True:
            value = conversation.ask("Liitä hyväksymiesi kurssien osoitteet tai tunnukset välilyönnein (tyhjä peruu):")
            try:
                identifiers = _targets(value)
            except argparse.ArgumentTypeError:
                conversation.say("Anna 1–12 Urheiluhallien kurssiosoitetta tai positiivista kurssitunnusta.")
                continue
            conversation.say("Tarkistan kurssien tason, hallit, ajat ja hinnat julkisesta kurssiluettelosta.")
            courses = [client.get_course(course_id) for course_id in identifiers]
            for identifier, course in zip(identifiers, courses):
                if course.id != identifier or not filters.matches(course):
                    raise ConfigError("Valittu kurssi ei vastaa kurssitasoa, halli- tai aikarajoja. Varausajoa ei aloitettu.")
            return ranked(courses, filters)


def _budget(conversation: _Conversation, courses: list[Course]) -> Decimal:
    from .cli import euro_amount
    if any(course.price is None or not course.price.is_finite() for course in courses):
        raise ConfigError("Valitulta kurssilta puuttuu varmistettu hinta. Varausajoa ei aloitettu.")
    highest = max(course.price for course in courses if course.price is not None)
    while True:
        value = conversation.ask(f"Yhden kurssin enimmäishinta euroina [{highest:.2f}]:", default=f"{highest:.2f}")
        try:
            amount = euro_amount(value)
            if amount < highest:
                conversation.say("Raja alittaa jonkin valitun kurssin hinnan. Anna suurempi raja tai peruuta ja muuta kurssivalintaa.")
                continue
            return amount
        except argparse.ArgumentTypeError:
            conversation.say("Anna euromäärä, esimerkiksi 76,00.")


def _opening(conversation: _Conversation, courses: list[Course]) -> datetime | None:
    from .cli import HELSINKI, opening_time
    if all(course.registration_start is not None for course in courses):
        return None
    published = [course.registration_start for course in courses if course.registration_start is not None]
    while True:
        value = conversation.ask(
            "Ilmoittautumisen alkuaika puuttuu. Anna tiedossa oleva aika VVVV-KK-PPTHH:MM "
            "(Suomen aika; nyt = aloita nyt, tyhjä peruu):"
        )
        try:
            result = datetime.now(HELSINKI) if value.casefold() == "nyt" else opening_time(value)
            if published and result < max(published):
                conversation.say("Aika on aikaisempi kuin jonkin kurssin julkaisema avausaika. Anna myöhempi aika.")
                continue
            return result
        except argparse.ArgumentTypeError:
            conversation.say("Anna päivä ja kellonaika, esimerkiksi 2030-01-12T07:00.")


def _duration(conversation: _Conversation) -> int:
    while True:
        value = conversation.ask("Varausyrityksen kesto avautumisesta minuutteina [30]:", default="30")
        try:
            number = int(value)
            if 1 <= number <= 240:
                return number
        except ValueError:
            pass
        conversation.say("Anna kesto 1–240 minuuttia.")


def _review_and_run(conversation: _Conversation, courses: list[Course], args, settings, directory, parse_args, run_reservation) -> int:
    if not courses:
        return 0
    conversation.show_courses(courses)
    child = conversation.ask("Lapsen tarkka nimi asiakastilillä (vain tämän ajon käyttöön; tyhjä peruu):")
    budget = _budget(conversation, courses)
    payment = conversation.choice(
        "Maksutapa: 1 FastPay, 2 verkkomaksu, 3 saldo [1]:",
        {"1": "fastpay", "fastpay": "fastpay", "2": "verkkomaksu", "verkkomaksu": "verkkomaksu", "3": "saldo", "saldo": "saldo"},
        default="1",
    )
    if payment == "fastpay":
        conversation.say("FastPay voi käyttää kauppaan tallennettua korttia; varmista maksutapa näkyvässä selaimessa. Mahdollinen lisävahvistus tehdään selaimessa.")
    opening = _opening(conversation, courses)
    duration = _duration(conversation)
    queue_default = "k" if settings.queue_fallback else "e"
    use_queue = conversation.choice(
        "Yritetäänkö jonopaikkoja kaikille hyväksymillesi sopiville kursseille heti jonojen auetessa, "
        "samalla kun varsinaista paikkaa haetaan? "
        + ("[K/e]:" if settings.queue_fallback else "[k/E]:"),
        {"k": "yes", "kyllä": "yes", "kylla": "yes", "e": "no", "ei": "no"},
        default=queue_default,
    ) == "yes"
    step_mode = conversation.choice(
        "Ohjaustapa: 1 automaattinen eteneminen, 2 kysy ennen varausta ja maksua [2]:",
        {"1": "auto", "2": "ask"}, default="2",
    ) == "ask"
    conversation.say("\nTarkista hyväksymäsi kurssit:")
    conversation.show_courses(courses)
    conversation.say(f"Tavoite: yksi varsinainen kurssipaikka {len(courses)} hyväksytystä vaihtoehdosta.")
    conversation.say(f"Yhden kurssin enimmäishinta: {budget:.2f} €. Maksutapa: {payment}.")
    conversation.say("Osallistuja: syöttämäsi asiakastililtä valittava lapsi. Nimeä ei kirjoiteta asetustiedostoon.")
    for course in courses:
        starts = opening or course.registration_start
        assert starts is not None  # Opening times were validated above.
        conversation.say(f"Kurssi {course.id}: ilmoittautuminen {starts.isoformat()}; yritysaika {duration} min.")
    conversation.say(
        "Jonopaikat: kaikille hyväksymillesi sopiville kursseille heti jonojen auetessa; "
        "varsinaisen paikan haku jatkuu. Jonopaikkoja ei lasketa kurssipaikoiksi."
        if use_queue else "Jonopaikkaa ei yritetä."
    )
    conversation.say(
        "Kysyn hyväksynnän ennen varausta, maksua ja jonopyyntöä. Hyväksyntä voi hidastaa paikan saamista."
        if step_mode else "Etenen hyväksytyillä ehdoilla automaattisesti. Maksu voidaan tehdä ilman uutta kysymystä."
    )
    conversation.say("Näet selaimen ja jatkuvat tilaviestit. Ajon aikana käytössä: tila, tauko, jatka, lopeta.")
    if conversation.ask("Käynnistä kirjoittamalla täsmälleen aloita (muu vastaus peruu):").casefold() != "aloita":
        raise _Cancelled
    command = [
        "varaa-yksi", *(str(course.id) for course in courses), "--lapsi", child,
        "--enintaan", str(budget), "--maksutapa", payment, "--kesto", str(duration),
        "--keskustele", "--jono" if use_queue else "--ei-jono",
    ]
    if step_mode:
        command.append("--kysy-toiminnot")
    if opening is not None:
        command.extend(("--avautuu", opening.isoformat()))
    command.extend(_inherited_options(args, directory, include_search=True))
    parsed = parse_args(command)
    conversation.say("Avaan näkyvän selaimen. Kirjaudu tarvittaessa itse; varaus tarkistetaan vielä ennen lähettämistä.")
    return run_reservation(parsed, settings, directory)


def _demo(conversation: _Conversation, args, settings, directory, parse_args, run_test_shop) -> int:
    scenario = conversation.choice(
        "Paikallinen koeajo: 1 normaali varaus, 2 jonopaikka, 3 jonovastaus katoaa [1]:",
        {"1": "normaali", "2": "jono", "3": "jono-vastaus-katoaa"}, default="1",
    )
    command = [
        "koeajo", "--monta", "3", "--maksutapa", "fastpay", "--skenaario", scenario,
        "--keskustele", "--aikaraja", "10", "--avautuu-sekunnissa", "3",
    ]
    if scenario != "normaali":
        command.append("--jono")
    command.extend(_inherited_options(args, directory, include_search=False))
    conversation.say("Avaan paikallisen testikaupan näkyvään selaimeen. Käytössä ovat keksityt asiakastiedot ja testikortti; oikeaa maksua ei tehdä.")
    return run_test_shop(parse_args(command), settings, directory)


def run_wizard(
    args, settings: Settings, directory: Path, *, parse_args, run_reservation,
    run_test_shop, run_login, client_factory,
    input_fn: Callable[[], str] | None = None, output: TextIO | None = None,
    interactive: bool | None = None,
) -> int:
    """Ask for a concrete task and run at most one reviewed booking/demo.

    Injected runner signatures are ``runner(parsed, settings, directory)`` for
    reservation/demo, and ``run_login(parsed, directory)`` for login. All input
    and argument assembly stays in memory. EOF/blank cancellation never starts
    a paid run. Noninteractive callers should use the ordinary CLI commands.
    """
    conversation = _Conversation(input if input_fn is None else input_fn, sys.stdout if output is None else output)
    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    if not is_interactive:
        conversation.say("Ohjattu käyttö tarvitsee vuorovaikutteisen päätteen. Käytä muussa tapauksessa haku-, varaa-yksi- tai koeajo-komentoa.")
        return 2
    conversation.say("Ohjattu Urheiluhallit-kone: valitse tehtävä, tarkista ehdot ja seuraa näkyvää selainta.")
    conversation.say("Voit perua milloin tahansa kirjoittamalla peruuta. Henkilötietoja ei tallenneta asetuksiin tai lähdekoodiin.")
    try:
        choice = conversation.choice(
            "Mitä haluat tehdä? 1 hae kurssit, 2 käytä kurssiosoitetta/tunnusta, 3 paikallinen koeajo, 4 kirjaudu selaimeen, 0 lopeta:",
            {"1": "search", "2": "targets", "3": "demo", "4": "login", "0": "exit"},
        )
        if choice == "exit":
            return 0
        if choice == "demo":
            return _demo(conversation, args, settings, directory, parse_args, run_test_shop)
        if choice == "login":
            command = ["kirjaudu", *_inherited_options(args, directory, include_search=False)]
            conversation.say("Avaan näkyvän selaimen kirjautumista varten. Tämä vaihe ei varaa kursseja tai maksa.")
            result = run_login(parse_args(command), directory)
            return 0 if result is None else result
        courses = _select_courses(conversation, args, settings, client_factory, search=choice == "search")
        return _review_and_run(conversation, courses, args, settings, directory, parse_args, run_reservation)
    except _Cancelled:
        conversation.say("Peruttu. Varausajoa ei käynnistetty.")
        return 0
