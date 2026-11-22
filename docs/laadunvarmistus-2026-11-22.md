# Laadunvarmistus ja correctness-tarkastus 22.11.2026

## Johtopäätös

Huoli simuloidun kaupan erilaisuudesta oli perusteltu. Paikallinen testikauppa on hyödyllinen varausohjaimen häiriöiden, uudelleenkäynnistyksen ja päällekkäisten pyyntöjen testaamiseen. Sen läpäisy ei osoita, että selainadapteri toimii oikeassa Urheiluhallien kaupassa. Tarkastuksessa löytyi oikean kaupan julkisesta JavaScriptistä konkreettinen pyyntömuoto, jonka vanha adapteri hylkäsi ja jota testikauppa ei käyttänyt.

Myös tilakoneessa oli virheitä: uusi samanhintainen kuitti saattoi kelvata maksun todisteeksi ilman yhteyttä oikeaan varaukseen, ja ristiriitaiset peräkkäiset lukuvastaukset saattoivat johtaa tarpeettomaan maksuyritykseen. Korjaushaara tiukentaa näitä ehtoja ja lisää regressiotestit.

**Automaattisen maksun tuotantokelpoisuutta ei ole tällä tarkastuksella osoitettu.** Oikean kirjautuneen tilin varaus–kuitti-yhteys, osallistujan valinta ja maksupalvelun koko ketju ovat edelleen todentamatta. Tarkastuksessa ei tehty oikeita varauksia, jonoon liittymisiä tai maksuja.

## Mitä lähtötilanteen testit todistivat?

Tarkastuksen lähtökohta oli `main`-commit `e22710cf27535db24f8eb6c19e4a38db66a5c669`.

| Kerros | Lähtötilanteen sisältö | Todistusvoiman raja |
| --- | --- | --- |
| Yksikkötestit | Kurssien suodatus, hintarajat, tilakone, pysyvä journal, jonotus ja usean kurssin koordinointi | Suuri osa syötteistä oli testien itse rakentamia havaintoja. Ne eivät testanneet oikean kaupan vastauksien tulkintaa. |
| Paikalliset HTTP-testit | 18 testiä omaa HTTP-testikauppaa ja SQLitea vasten | Oikea HTTP ja pysyvä tietokanta; rajapinnan semantiikan määritti silti oma simulaattori. |
| Selainfixturet | Kaksi varausketjun testiä, viisi osallistujavalinnan testiä ja yksi staattisen esimerkin testi | Varausfixture sieppasi verkkopyynnöt ja korvasi adapterin lukurajapinnan. Oikean kaupan kirjautunut tila ja palvelin eivät olleet mukana. |
| Koko paikallinen selainajo | `scripts/smoke_browser.py`: oikea Chromium, paikallinen HTTP-kauppa, jonot, yksi varaus ja maksu sekä uudelleenkäynnistys | Mielekäs paikallinen integraatiotesti, mutta sitä **ei ajettu CI:ssä**. Se käytti samaa omaa testikauppaa. |
| Kurssiaineisto | 139 kurssin julkinen aineisto, tallennettu 18.10.2026 | Todellinen julkinen data, mutta ei tämänhetkinen kauppa eikä kirjautuneen tilin vastaus. |
| Tuotannon varaus ja maksu | Ei löytynyt automaattista kirjautunutta tuotantotestiä | Vihreistä paikallisista testeistä ei voinut päätellä tämän ketjun toimivuutta. |

Lähtötilanteen paikallinen ajo palautti **309 läpäistyä testiä, 8 ohitettua selaintestiä ja 310 läpäistyä alitestiä**. Ruff, ty ja julkisen lähdepuun tarkistus läpäisivät. Chromium ei käynnistynyt tässä työympäristössä: sen tarvitseman paikallisen Unix-pistokkeen luonti estyi käyttöjärjestelmätasolla (`EPERM`). Ohituksia ei lasketa onnistuneeksi selainnäytöksi.

## Varmistetut löydökset ja korjaukset

### 1. Oikean kaupan maksupyyntö estyi suhteellisen paluuosoitteen vuoksi

Oikean kaupan julkinen `Enkora.getCurrentFullPath()` muodostaa polun, kyselyn ja fragmentin ilman palvelimen nimeä. Kaupan maksupyyntö käyttää tätä suhteellista arvoa kentissä `return_url` ja `cancel_url`. Vanha `_BookingGuard` vaati näihin absoluuttisen saman alkuperän URL:n. Se hylkäsi julkisesta lähteestä toistetun maksupyynnön, vaikka sen muut kentät olivat oikein.

Testikauppa ja selainfixture käyttivät absoluuttisia osoitteita. Näin molemmat olivat samaa mieltä virheellisestä oletuksesta.

**Korjaus:** paluuosoite ratkaistaan luotettua kaupan alkuperää vasten, minkä jälkeen tarkistetaan alkuperä, tarkka reitti ja sallitut kyselyparametrit. Vierasta palvelinta tai väärää paluureittiä ei hyväksytä. Testikauppa lähettää nyt suhteellisen osoitteen. Oletussisällöttömät RPC-luvut käyttävät lisäksi julkisesta Enkora-serialisoijasta havaittua tyhjää merkkijonoa JSON-nullin sijaan.

Näyttö: `tests/test_booking_browser.py`-tiedoston checkout-rajauksen ja RPC-sisällön testit sekä `tests/test_booking_flow.py`-selainfixture. Tämä todistaa havaitun julkisen pyyntömuodon tuen; se ei todista oikean maksupalvelun hyväksyvän ja suorittavan maksua.

### 2. Maksunäkymän asynkroninen päivittyminen jäi mallintamatta

Oikean HTML-mallin ostoehdot voivat ilmestyä ennen maksutapojen latautumista. Julkinen JavaScript hakee maksutavat ja saldon erikseen ja piirtää ostoskorin uudelleen. Vanha adapteri odotti ostoehtoja, mutta tarkisti maksupainikkeen heti. Testikauppa piirsi kaiken yhdessä vaiheessa.

**Korjaus:** adapteri odottaa valitun maksutoiminnon valmistumista ja valitsee ehdot lopullisessa näkymässä. Selainfixtureen lisättiin viivästetty maksutapojen renderöinti. Testikauppa mallintaa näkymän vaiheittaisen ilmestymisen.

Näyttö: `tests/test_booking_flow.py`. Näkymän todellinen ajastus ja kaikki kirjautuneen tilin palvelinvasteet ovat silti vielä todentamatta tuotannossa.

### 3. Samansuuruinen kuitti saattoi todistaa väärän maksun

Vanha ohjain hyväksyi uuden suljetun, oikeansummaiseksi tulkitun kuitin ilman todistetta sen yhteydestä valittuun varaukseen. Toistossa toiseen varaukseen kuuluva samanhintainen kuitti johti tulokseen `confirmed`. Ohjain saattoi myös siirtyä maksun täsmäytykseen maksuttoman korilisäyksen jälkeen, vaikka checkoutia ei ollut lähetetty.

**Korjaus:** uuden maksun vahvistus edellyttää kirjattua maksuyritystä, ennen lähetystä tunnistettua varausta, saman varaustunnuksen säilymistä, varauksen maksutietoa, tyhjentynyttä koria, oikeansummaista suljettua uutta kuittia ja varauksella havaittua viitettä samaan kuittiin. Ilman maksuyritystä löytynyt jo vahvistettu varaus palauttaa `already_confirmed` ilman kuittiväitettä. Puuttuva tai ristiriitainen näyttö ei oikeuta uuteen maksupyyntöön.

Adapterin sisäinen `reservation_receipt_id` täytetään kelvollisesta `receipt_event_id`-kentästä. **Tämän kentän saatavuutta ja merkitystä oikean kirjautuneen tilin vastauksessa ei ole varmistettu.** Testikaupan kenttä ei todista tuotannon sopimusta. Jos tuotanto ei anna tarvittavaa yhteyttä, automaatti ei vahvista maksun valmistumista. Tämä voi jättää onnistuneenkin maksun odottamaan manuaalista tarkistusta.

Näyttö: `test_same_price_closed_receipt_without_a_reservation_link_is_pending`, `test_confirmation_after_unpaid_add_never_claims_a_new_payment`, `test_payment_requires_same_reservation_and_attached_receipt_even_after_restart` ja `test_legacy_completed_journal_cannot_reassert_a_misattributed_receipt` tiedostossa `tests/test_booking.py`.

### 4. Vanha ostoskorihavainto saattoi käynnistää maksun jo maksetulle varaukselle

Ostoskori ja omat varaukset luetaan erikseen. Toistettu havainto sisälsi samaan aikaan jo maksetun varauksen ja vanhan ostoskoririvin. Vanha ohjain käsitteli ensin koria ja kutsui checkoutia. Toistossa havaittiin ylimääräinen maksuyritys; oikeassa kaupassa tapahtunutta kaksinkertaista veloitusta ei havaittu eikä väitetä.

**Korjaus:** ristiriitainen havainto jää vain lukevaan täsmäytykseen. Jo havaitun vahvistetun varauksen tunnus säilytetään myös uudelleenkäynnistyksen ja monen välilehden välillä. Myöhempi vanhempi ostoskorihavainto ei avaa maksamista uudelleen. Adapteri tarkistaa myös juuri ennen toimintoa, ettei varaus ole jo maksettu.

Näyttö: `test_seen_paid_reservation_cannot_be_charged_on_a_later_held_only_read` tiedostossa `tests/test_booking.py` sekä yhteisen maksutodisteen ja keskeytyksen testit tiedostossa `tests/test_multi.py`.

### 5. Ennen lähetystä tapahtunut lukuvirhe tulkittiin mahdollisesti lähetetyksi pyynnöksi

Esimerkiksi maksutapaluettelon tilapäinen lukuvirhe saattoi lukita ohjaimen maksun odottamiseen, vaikka lähetysraja ei ollut päästänyt maksupyyntöä verkkoon. Tämä esti turvallisen uuden valmisteluyrityksen.

**Korjaus:** varmasti ennen lähetystä tapahtuneet tilapäiset virheet luokitellaan `ActionNotSent`-tulokseksi ja palvelimen odotusaikaa noudatetaan. Mahdollisesti lähetetyn pyynnön epäselvä tulos pysyy lukittuna täsmäytykseen. Varmasti lähettämätöntä ja mahdollisesti lähetettyä pyyntöä ei rinnasteta.

Näyttö: `ReadOnlyPreflightTests` tiedostossa `tests/test_booking_browser.py` ja `test_proven_unsent_checkout_honors_shop_retry_after` tiedostossa `tests/test_booking.py`.

### 6. Lähetysrajan tarkistukset olivat puutteellisia

Määräaika saattoi ylittyä modaalin tai maksunäkymän valmistelussa ennen varsinaista verkkopyyntöä. Valmistelun aikana ilmestynyt oma keskeneräinen varaus saattoi jäädä huomaamatta. Lisäksi ostoskorin täsmäytys ei tarkistanut myyntituotteen tunnusta, viikkoaikataulun muutos ei aina estänyt vanhan kurssivalinnan käyttöä, ja puuttuva `is_paid`-kenttä tulkittiin liian sallivasti.

**Korjaus:** määräaika tarkistetaan myös varsinaisella verkkopyynnön rajalla, mukaan lukien jonopyynnöt. Varaustila luetaan uudelleen juuri ennen korilisäystä. Ostoskorin tuote, kurssin viikkoaikataulu ja nimenomainen maksutieto tarkistetaan.

Näyttö: määräajan, valmistelun aikana ilmestyvän varauksen, väärän myyntituotteen, muuttuneen aikataulun ja puuttuvan maksutiedon regressiot tiedostossa `tests/test_booking_browser.py`.

### 7. Yksi vaihtoehto saattoi estää muiden toimivan seurannan

Valitsemattoman kurssin hintarajan ylitys saattoi katkaista koko monen kurssin haun ennen sopivan vaihtoehdon käsittelyä. Erillinen ajoitusvirhe hidasti jo avoimen kurssin seurantaa, kun toinen vaihtoehto avautui vasta myöhemmin. Kellolla toistetussa testissä avoimen kurssin lyhyt vapaa paikka jäi huomaamatta.

**Korjaus:** valitsematon liian kallis vaihtoehto ohitetaan, mutta kirjoituksen ja olemassa olevan korin hintarajat säilyvät. Myös jonovaihtoehdot tarkistetaan. Avoin kurssi säilyttää normaalin tiheän seurannan riippumatta myöhemmistä avautumisista. Mahdollisesti lähetetty varaus pitää valinnan edelleen lukittuna.

Näyttö: `test_higher_price_on_unselected_course_keeps_other_candidate_available` tiedostossa `tests/test_multi_browser.py` sekä ajoituksen, hinnannousun ja jonopolitiikkojen regressiot tiedostossa `tests/test_multi.py`.

## Testinäytön parannukset

Tavallinen CI ajaa nyt paikalliset testit valitsimella `--fail-on-skip`. Yksikin ohitus epäonnistuttaa ajon; selaimen puuttuminen ei siten tuota vihreää lopputulosta. Portti tarkistettiin sekä tarkoituksella ohitetulla väliaikaisella testillä että tämän työympäristön kahdeksalla selainohituksella.

`scripts/smoke_browser.py` liitettiin CI:hin. Alkuperäisen usean kurssin jonotus- ja maksuketjun lisäksi se tarkistaa yhden kurssin varsinaisen `open_booking_browser`-adapterin, maksamattoman korin näyttämisen ja uudelleenkäynnistyksen ilman uutta korilisäystä tai maksua. Tämä on kokonainen paikallinen selainajo; se ei aja oikeaa Urheiluhallien ostoa eikä koko komentorivivalikkoa.

Lisäksi `scripts/check_live_shop.py` lukee erikseen oikean kaupan kolme julkista lähdettä HTTPS GET -pyynnöillä: HTML:n, HTML:n ilmoittaman saman alkuperän varausmoduulin JavaScriptin ja kurssiluettelon. Se tarkistaa tuotannossa käytettävällä `parse_course`-funktiolla kurssien tulkinnan, vaadittujen HTML-elementtien rakenteen ja JavaScriptin rajapintamerkit. Se ei kirjaudu, lue yksityisiä päätepisteitä tai lähetä kirjoittavia pyyntöjä.

Tarkistimelle lisättiin 11 testiä. Negatiiviset tapaukset rikkovat esimerkiksi kurssin avainkentän, osallistujavalinnan rakenteen, maksupainikkeen, JavaScript-lähteen alkuperän ja `R2.cart`-määrittelyn. Keskeisiä tarkistuksia poistamalla varmistettiin erillisissä väliaikaisissa kopioissa, että vastaavat regressiotestit todella havaitsevat poiston.

Julkinen tarkistus on omassa käsin käynnistettävässä **Live public shop contract** -työnkulussa. Sitä ei ajastettu. Paikallinen testisarja ei ole riippuvainen tuotantokaupan hetkellisestä saatavuudesta.

## Julkisen lähdenäytön alkuperä

Oikean kaupan käyttöliittymä tarkastettiin selaimessa kirjautumatta. Näkymän rakenne, osallistuja- ja ostoskorimallit sekä pyyntöjen muodostus tarkastettiin julkisesta HTML:stä ja JavaScriptistä. Raakoja tuotantovastauksia tai kuvakaappauksia ei tallenneta repositoryyn.

Automaattinen julkinen tarkistus läpäisi **22.11.2026 klo 8.52.14 Suomen aikaa (06.52.14 UTC)**. Se tulkitsi **143 kurssia**.

| Lähde | SHA-256 tarkistushetkellä |
| --- | --- |
| [Julkinen kauppasivu](https://kauppa.urheiluhallit.fi/webshop/reservations2/) | `6d05b8f574e86e9c2398a237713300d86d8c093b72f991dbdfe0b3ecced57632` |
| [Sivun ilmoittama varausmoduulin JavaScript](https://kauppa.urheiluhallit.fi/webshop/javascript/jsmodule/reservations2/27407640c7bd4974e2eea14d5d7fc06e) | `1e15bbdd38036f6145ec39a3fe21957b03fece9c5df57076134271f044dd2320` |
| [Julkinen uimakoululuettelo](https://kauppa.urheiluhallit.fi/webshop/reservations2/apieventgroups?service_id=9) | `fb962c2bbe98306631ed772ecb19dfc8d98cc0a7539c1d26e48714b93df856a4` |

Tarkistussummat yksilöivät käytetyn lähdeaineiston. Tarkistin ei vaadi tulevilta vastauksilta samaa summaa: muuttuva kurssiluettelo on normaalia. HTML- ja JavaScript-tarkistukset kattavat nimettyjä rakenteita, eivät selaimen ajonaikaista toimintaa tai kaikkia kolmannen osapuolen muutoksia.

## Lopullinen varmennus

Yhdistetyn korjaushaaran paikallinen ajo palautti **345 läpäistyä testiä, 9 ohitettua selaintestiä ja 322 läpäistyä alitestiä**. Lähtötilanteeseen verrattuna mukaan tuli myös viivästetyn FastPay-valikon selainregressio. Ruff, ty, `git diff --cached --check` ja Git-indexin **63 lähdetiedoston** julkisuusraja läpäisivät.

GitHubin pakollisen selainajon tulos ja riippumaton katselmointi täydennetään tähän niiden valmistuttua. Paikallisen ympäristön selainohituksia ei korvata väitteellä läpäistyistä selaintesteistä.

## Jäljellä olevat rajat

- Oikean kirjautuneen tilin osallistujavalinta, ostoskorin vastausrakenteet, jonotus, maksu ja varaus–kuitti-yhteys vaativat erikseen todennettavan integraation. Julkinen GET-tarkistus ei kata niitä.
- Maksun varmistukseen vaadittu kuittiviite voi puuttua oikeasta vastauksesta. Tällöin työkalu jättää maksun vahvistamatta ja seuraa samaa yritystä; se ei turvallisesti voi päätellä epäonnistumista ja lähettää maksua uudelleen.
- Testikauppa on tarkoituksella yksinkertaistettu. Sen ulkoasun yhtenäistäminen ei yksin poistaisi riippumattoman rajapinta- ja selainnäytön tarvetta.
- `koeajo --monta 1` käyttää edelleen monen kurssin poolia, kun varsinainen yhden kurssin ajo käyttää omaa selainadapteria. Uusi smoke kattaa tämän adapterin ja maksamattoman korin näyttämisen erikseen. Koko ohjatun CLI:n läpikäynti ei kuulu siihen.
- Vanha yhteinen varaustallenne estää ehdokasjoukon vaihtamisen myös sellaisen päättyneen ajon jälkeen, jossa ei lähetetty yhtään kirjoitusta. Tätä käytettävyyshaittaa ei muutettu tässä maksuturvallisuuden korjauksessa. Epäselviä varaus- tai maksutallenteita ei pidä poistaa uuden yrityksen saamiseksi.
- Selainympäristön, istunnon tai tuotannon vastauksen poikkeama voi edelleen pysäyttää etenemisen. Korjauksissa epäselvyys painotetaan havaitun varauksen säilyttämiseen ja uuden maksun estämiseen.

## Tarkistuksen toistaminen

```sh
uv sync --locked --all-groups
uv run --locked python -m playwright install chromium
PYTHONPATH=. uv run --locked pytest -q --fail-on-skip
uv run --locked python scripts/smoke_browser.py
uv run --locked ruff check .
uv run --locked ty check
uv run --locked python scripts/check_public_tree.py
uv run --locked python scripts/check_live_shop.py
```

Viimeinen komento käyttää julkista tuotantokauppaa. Muut testiajot käyttävät paikallisia sivuja ja kuvitteellisia osallistujia. Julkisen lähdepuun tarkistin lukee Git-indexin, joten julkaistavat muutokset pitää tarkastaa myös staging-vaiheessa.
