# Correctness Audit Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for scoped implementation and independent review. Steps use checkboxes for tracking.

**Goal:** Korjata toistetut tuotantoyhteensopivuuden ja varauslogiikan virheet sekä näyttää testien todellinen todistusvoima.

**Architecture:** Nykyinen selainadapteri ja pysyvä varausohjain säilyvät. Julkisen kaupan tarkistus muodostaa erillisen, vain lukevan todisteen; simulaattori säilyy häiriötilanteiden testaamista varten.

**Tech Stack:** Python 3.11+, httpx, Playwright, pytest, Ruff, ty.

**Spec:** `docs/superpowers/specs/2026-11-22-correctness-audit-design.md`

## Global Constraints

- Python 3.11 tai uudempi; nykyiset riippuvuudet.
- Ei oikeita varauksia, jonoihin liittymisiä tai maksuja tarkastuksessa.
- Ei raakoja tuotantovastauksia tai henkilötietoja Git-puuhun.
- Epäselvää lähetettyä varausta tai maksua ei saa uusia.
- Julkisen tarkistuksen läpäisy ei todista kirjautuneen varaus- tai maksuketjun toimivuutta.
- Käyttöliittymän tekstit ja tarkastusraportti suomeksi.

## Review Focus

- Suhteellinen paluuosoite ja sitä muistuttava vieras osoite: oikea hyväksytään, vieras estetään (tehtävä 1).
- Määräaika ylittyy valmistelussa tai palvelinviive piilottaa olemassa olevan varauksen: ei uutta kirjoitusta (tehtävä 1).
- Väärä samanhintainen kuitti tai puuttuva maksutieto: ei uuden maksun onnistumisväitettä (tehtävät 1–2).
- Avoin ja vasta myöhemmin avautuva vaihtoehto samassa haussa: avoimen normaali tarkistusväli säilyy (tehtävä 2).
- Selain ei käynnisty tai julkisen kaupan rakenne muuttuu: tarkistus epäonnistuu selvästi eikä vihreä tulos peitä puutetta (tehtävä 3).

### Task 1: Selainadapterin rajat ja oikea pyyntömuoto

**Files:** `uhkone/booking_browser.py`, `uhkone/multi_browser.py`, `uhkone/testshop.html`, `uhkone/testshop.py`, `tests/test_booking_browser.py`, `tests/test_booking_flow.py`, `tests/test_multi_browser.py`, tarvittaessa `tests/test_testshop.py`.

**Interfaces:** Nykyiset `BookingBrowser`-metodit säilyvät. Tehtävä 2 lisää `Observation.reservation_receipt_id: str | None = None`; adapteri täyttää sen varauksen `receipt_event_id`-kentästä vain kelvollisesta tunnuksesta. Kuitin `receipt_id` on nykyinen vertailuarvo. Todentamaton yhteys jättää vahvistuksen avoimeksi.

- [x] Lisää regressiotestit oikean julkisen JavaScriptin suhteellisille paluuosoitteille, vieraalle alkuperälle, paluureitille ja RPC:n tyhjälle oletussisällölle. Aja ja varmista odotetut virheet.
- [x] Korjaa osoitteiden ratkaisu ja kirjattujen paluureittien normalisointi. Mallinna testikaupassa sama suhteellinen pyyntömuoto.
- [x] Odota asynkronisesti valmistuvaa maksupainiketta ennen ehtojen lopullista valintaa. Lisää viivästettyä maksutapaluetteloa käyttävä selainregressio; ehtojen varhainen ilmestyminen ei yksin tarkoita valmista maksunäkymää.
- [x] Lisää regressiotestit tilapäiselle lukuhäiriölle ennen lisäystä/maksua, keskeneräisen varauksen ilmestymiselle juuri ennen lisäystä ja määräajan ylittymiselle ennen lähetystä. Varmista ensin virheet ja korjaa toiminnon luokitus sekä viimeinen verkkoraja.
- [x] Lisää regressiotestit muuttuneelle viikkoaikataululle, väärälle myyntituotteelle, puuttuvalle `is_paid`-tiedolle ja varauksen kuittiviitteelle. Korjaa havaintojen tarkistus.
- [x] Lisää aidon `BookingPool.observations`-rajapinnan testi: yli hintarajan oleva valitsematon vaihtoehto ei keskeytä sallittua vaihtoehtoa. Hinta tarkistetaan aina ennen kirjoitusta ja olemassa olevan korin kohdalla.
- [x] Aja kosketettujen testien lisäksi koko testijoukko, Ruff ja ty. Kirjaa ohitukset. Root kokoaa tehtävän tiedostot yhteiseen korjauscommittiin.

### Task 2: Ohjaimen onnistumistodiste ja seurannan ajoitus

**Files:** `uhkone/booking.py`, `uhkone/multi.py`, `tests/test_booking.py`, `tests/test_multi.py`.

**Interfaces:** Lisää `Observation.reservation_receipt_id: str | None = None`. Uuden maksun hyväksyntä vertaa sitä `receipt_id`-kenttään. Säilytä tunnistettu varaus maksuvaiheen yli; eri varaus ei täytä käynnistettyä maksua.

- [x] Lisää regressiotesti samanhintaiselle mutta toiseen varaukseen kuuluvalle kuitille: `status != 'confirmed'`. Lisää oikeasti linkitetty positiivinen tapaus ja puuttuva linkki.
- [x] Lisää regressiotesti ilman maksua tehdylle lisäykselle, jonka jälkeen ilmestyy vahvistettu varaus: `status == 'already_confirmed'`, `receipt_id is None`, ei checkout-kutsua.
- [x] Lisää regressiotesti maksuvaiheessa vaihtuneelle varaustunnukselle: ei vahvistusta eikä uutta maksua. Toteuta pienet muutokset vasta punaisten testien jälkeen.
- [x] Lisää ristiriitaisen havainnon testi: maksettu/vahvistettu varaus ja vanha koririvi yhtä aikaa eivät saa käynnistää checkoutia tai tulla ilmoitetuksi maksamattomaksi koriksi. Odota johdonmukaista palvelintietoa.
- [x] Lisää kellolla toistettava testi: yksi vaihtoehto on avoin, toinen avautuu 100 sekunnin päästä ja avoimeen vapautuu paikka 3–12 sekunniksi. Paikka havaitaan ajoissa. Korjaa pitkän ennakko-odotuksen ehto.
- [x] Aja koko testijoukko, Ruff ja ty. Kirjaa mahdolliset adapterin rinnakkaiseen työhön liittyvät väliaikaiset riippuvuudet. Root kokoaa tehtävän tiedostot yhteiseen korjauscommittiin.

### Task 3: Riippumaton julkinen tarkistus ja rehellinen CI

**Files:** uusi `uhkone/shop_contract.py`, uusi `scripts/check_live_shop.py`, uusi `tests/test_shop_contract.py`, uusi `tests/conftest.py`, `.github/workflows/checks.yml`, uusi `.github/workflows/live-shop-contract.yml`.

**Interfaces:** `python scripts/check_live_shop.py` suorittaa rajatut GET-luvut julkisesta tuotantokaupasta. Tuloste on turvallinen yhteenveto. Ei lisäriippuvuuksia. `pytest --fail-on-skip` tekee ohitetuista testeistä virheen CI:ssä.

- [x] Lisää negatiiviset testit puuttuvalle kurssirajapinnan avainkentälle, muuttuneelle osallistuja-/maksupainikerakenteelle ja puuttuvalle R2-sopimukselle. Testit koettelevat tarkistimen väärien vihreiden tulosten riskiä.
- [x] Toteuta rajattu lähteiden tarkistus käyttäen nykyistä `parse_course`-adapteria sekä oikean julkisen HTML:n ja JavaScriptin todennettuja rakenteita. Dynaaminen JavaScript-URL luetaan saman alkuperän HTML:stä. Ei kirjautumista, POST-pyyntöjä tai yksityisiä päätepisteitä. Raportoi alkuperä, aika, tarkistussummat, tarkistusten tulokset ja todentamatta jäävät vaiheet; älä tallenna raakaa sisältöä.
- [x] Aja tarkistus oikeaan kauppaan kerran. Käytä erillistä manuaalisesti käynnistettävää GitHub Actions -työnkulkua; älä lisää ajastusta tai tee paikallisia testejä ulkoisesta saatavuudesta riippuviksi.
- [x] Lisää `--fail-on-skip`-portti ja ota se käyttöön nykyisessä CI-testikomennossa. Todista tarkoituksella ohitetulla väliaikaisella testillä, että portti palauttaa virheen.
- [x] Aja koko testijoukko, Ruff ja ty. Root kokoaa tehtävän tiedostot yhteiseen korjauscommittiin.

### Task 4: Katselmointi ja tarkastusraportti

**Files:** `README.md`, uusi `docs/laadunvarmistus-2026-11-22.md`.

- [x] Kirjaa jokainen varmistettu löydös, korjaus, sitä todistava testi ja jäljellä oleva raja. Erottele julkinen sopimustarkistus, paikalliset simulaatiot ja todentamaton kirjautunut ketju.
- [ ] Teetä riippumaton katselmointi koko korjaushaaralle, korjaa olennaiset löydökset ja aja lopulliset portit.
- [ ] Tarkista koko staged-puu ja julkaistavien tekstien yksityisyys. Tallenna työ korjaushaaraan ja luo katselmoitava luonnos-PR, jos repo-yhteys sallii sen. Älä yhdistä mainiin tämän tarkastuksen osana.

### Katselmoinnin tuottama yhtenäinen korjausaalto

Riippumaton katselmointi commitille `599c8de` (etähaaran sama lähdepuu `8a251ea`) löysi vielä seuraavat varmistettavat ja korjattavat kohdat. Yksi toteuttaja käsittelee ne yhtenä muutoksena; root kokoaa raportin ja julkaisut.

- [x] Sido checkoutin lähetys ohjaimen hyväksymään ei-tyhjään varaustunnukseen ja tarkkaan hintaan; myöhempi havainto ei saa muuttaa hyväksyttyä maksua.
- [x] Säilytä maksettu, nimenomaisesti maksamaton ja tuntematon maksutieto erillään. Tuntematon tai ristiriitainen tila ei oikeuta checkoutia tai maksamattoman korin väitettä.
- [x] Säilytä adapterin viimeisellä lukukerralla löytynyt oma varaus tai maksutodiste pysyvästi ennen uutta yritystä tai vaihtoehdon vaihtamista.
- [x] Välitä jonotuksen fallback-tilan rajattu määräaika varsinaiselle verkkovartijalle; uudelleenkäynnistys ei saa avata uutta liukuvaa kirjoitusikkunaa.
- [x] Erota kirjoitusaie havaitusta checkout-lähetyksestä. Ennen lähetystä keskeytyneen ajon jälkeen ulkoisesti maksettua varausta ei saa lukea tämän ajon käynnistämäksi maksuksi.
- [x] Tarkista jonon tila ja hinta vielä osallistujanäkymän valmistelun jälkeen sekä noudata palvelimen uusintaviivettä molemmissa jonopolitiikoissa.
- [x] Kiristä julkisen kurssitarkistimen tuote-, hinta-, aikataulu- ja reittiarvojen tarkistuksia; pelkkä avaimen olemassaolo ei riitä.
- [x] Testaa maksutapojen ja saldon erilliset, kummassakin järjestyksessä valmistuvat renderöinnit aidolla selainfixturellä.
- [x] Rajaa lähetetyn maksupyynnön paluuosoitteen kysely tarkasti ja kuvaa paikallisen smoken jatkaminen/restart täsmällisesti.
- [ ] Tee yksi kohdennettu korjausaallon uusintakatselmointi ja tarkista lopullisen lähdepuun GitHub-ajo.
