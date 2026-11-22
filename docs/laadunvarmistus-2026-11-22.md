# Laadunvarmistus ja correctness-tarkastus 22.11.2026

## Johtopäätös

Huoli simuloidun kaupan erilaisuudesta oli perusteltu. Paikallinen testikauppa on hyödyllinen varausohjaimen häiriöiden, tallennetusta tilasta jatkamisen ja päällekkäisten pyyntöjen testaamiseen. Sen läpäisy ei osoita, että selainadapteri toimii oikeassa Urheiluhallien kaupassa. Tarkastuksessa löytyi oikean kaupan julkisesta JavaScriptistä konkreettinen pyyntömuoto, jonka vanha adapteri hylkäsi ja jota testikauppa ei käyttänyt.

Myös tilakoneessa oli virheitä: uusi samanhintainen kuitti saattoi kelvata maksun todisteeksi ilman yhteyttä oikeaan varaukseen, ja ristiriitaiset peräkkäiset lukuvastaukset saattoivat johtaa tarpeettomaan maksuyritykseen. Korjaushaara tiukentaa näitä ehtoja ja lisää regressiotestit.

**Automaattisen maksun tuotantokelpoisuutta ei ole tällä tarkastuksella osoitettu.** Repon automaation kirjautunut varausajo, lapsitilin valinta, varaus–kuitti-yhteys ja maksupalvelun koko ketju ovat edelleen todentamatta. Erillisessä oikean kaupan käyttöliittymäkokeessa 22.11.2026 tehtiin yksi aikuisen maksamaton testivaraus ja poistettiin se onnistuneesti. Jonoon ei liitytty eikä maksua aloitettu. Kokeen tarkka rajaus ja havainnot ovat jäljempänä.

## Mitä lähtötilanteen testit todistivat?

Tarkastuksen lähtökohta oli `main`-commit `e22710cf27535db24f8eb6c19e4a38db66a5c669`.

| Kerros | Lähtötilanteen sisältö | Todistusvoiman raja |
| --- | --- | --- |
| Yksikkötestit | Kurssien suodatus, hintarajat, tilakone, pysyvä journal, jonotus ja usean kurssin koordinointi | Suuri osa syötteistä oli testien itse rakentamia havaintoja. Ne eivät testanneet oikean kaupan vastauksien tulkintaa. |
| Paikalliset HTTP-testit | 18 testiä omaa HTTP-testikauppaa ja SQLitea vasten | Oikea HTTP ja pysyvä tietokanta; rajapinnan semantiikan määritti silti oma simulaattori. |
| Selainfixturet | Kaksi varausketjun testiä, viisi osallistujavalinnan testiä ja yksi staattisen esimerkin testi | Varausfixture sieppasi verkkopyynnöt ja korvasi adapterin lukurajapinnan. Oikean kaupan kirjautunut tila ja palvelin eivät olleet mukana. |
| Koko paikallinen selainajo | `scripts/smoke_browser.py`: oikea Chromium, paikallinen HTTP-kauppa, jonot, yksi varaus ja maksu sekä ajon jatkaminen samassa selainistunnossa | Mielekäs paikallinen integraatiotesti, mutta sitä **ei ajettu CI:ssä**. Se käytti samaa omaa testikauppaa. |
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

**Korjaus:** uuden maksun vahvistus edellyttää havaittua lähetystä, ennen lähetystä tunnistettua varausta, saman varaustunnuksen säilymistä, varauksen maksutietoa, tyhjentynyttä koria, oikeansummaista suljettua uutta kuittia ja todennettua yhteyttä samaan kuittiin. Ilman havaittua lähetystä löytynyt jo vahvistettu varaus palauttaa `already_confirmed` ilman kuittiväitettä. Pelkkä pysyvä maksuaie ei todista lähetystä. Puuttuva tai ristiriitainen näyttö ei oikeuta uuteen maksupyyntöön.

**Oikean kurssivarauksen ja kuitin yhteistä tunnistetta ei ole varmistettu.** Julkisessa JavaScriptissä kurssien `apiusereventgroups` käyttää `R2.EventGroupReservation`-mallia. Maksukenttiä `receipt_event_id` ja `is_paid` käyttävä julkinen näkymä kuuluu erilliseen `apireservations`-malliin ja rajaa kurssit nimenomaisesti pois (`getUserReservations`). Ostoskorin onnistumisnäkymä lukee kuitin ilman liitosta kurssivaraukseen. Tämä ei osoita kenttien puuttuvan kirjautuneesta kurssivastauksesta, mutta ei myöskään vahvista niiden saatavuutta tai tunnisteiden samaa merkitystä.

Testikauppa antaa varauksen kuittiviitteelle ja kuitille saman SQLite-avaimen. Sitä ei hyväksytä tuotannon sopimuksen todisteeksi. Kuittiviite välitetään maksun todisteeksi vain erikseen rajatun paikallisen testikaupan alkuperästä. Oikean kaupan uuden maksun `confirmed`-tulos jää tarkoituksellisesti pois käytöstä, kunnes kirjautunut integraationäyttö tai toimittajan sopimus osoittaa kuittiyhteyden.

Tämä rajoitus koskee vahvistamista: muu nimenomaisesti maksamaton varaustieto voi silti sallia yhden maksupyynnön lähettämisen. Onnistunutkin maksu voi jäädä automaatilta vahvistamatta ja vaatia manuaalista tarkistusta oikean kaupan omista varauksista ja kuitista. Puuttuva vahvistus ei osoita maksun epäonnistuneen. Maksua ei lähetetä uudelleen tämän epäselvyyden vuoksi. CLI kertoo rajan ennen tuotannon maksupolkua ja vahvistamattomassa lopputuloksessa.

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

### 8. Riippumattoman katselmoinnin löytämät lähetyshetken aukot

Ensimmäisen korjauskierroksen vihreät testit eivät kattaneet kaikkia viimeisen lukuhetken ja pysyvän tilan välisiä rajoja. Erillinen katselmointi toi seuraavat tapaukset korjausaaltoon. Testit käyttävät tarvittaessa todellista adapteria ja verkkovartijaa yhdessä ohjaimen kanssa; pelkkä valmiiksi rakennettu myönteinen havainto ei riitä.

| Toistettu puute | Korjaus |
| --- | --- |
| Ohjain hyväksyi varauksen A, mutta adapterin vanhentunut välimuisti päivitti hyväksynnän varaukseen B, puuttuvaan tunnukseen tai eri summaan. | Checkout saa ohjaimen hyväksymän ei-tyhjän varaustunnuksen ja tarkan summan. Molempien pitää säilyä samoina kaikissa valmistelun tarkistuksissa. |
| Puuttuva, virheellinen tai lopputilan kanssa ristiriitainen `is_paid` näytti maksamattomalta korilta. | Tuntematon maksutila erotetaan nimenomaisesta maksamattomuudesta. Vain tunnistettu, kelvollinen maksamaton varaus saa edetä maksuun tai maksamattoman korin luovutukseen. |
| Adapterin viimeisellä lukukerralla löytynyt oma tai maksettu varaus hävisi, kun toiminto palautui lähettämättömänä. | Havainto kulkee virhetuloksen mukana ohjaimelle ja tallennetaan ennen uutta yritystä tai kurssin vaihtamista. Myöhempi vanha vastaus ei pyyhi sitä pois. |
| Keskeytys pysyvän maksuaikeen ja varsinaisen lähetyksen välissä sai muualla maksetun varauksen näyttämään tämän ajon maksulta. | Maksuaie estää kaksoisyrityksen, mutta havaitulla lähetyksellä on erillinen pysyvä merkintä. Ilman lähetysnäyttöä löytynyt maksettu varaus ilmoitetaan ilman tämän ajon maksuväitettä. |
| Fallback-jonotus alkoi tarkoituksella alkuperäisen määräajan jälkeen, mutta verkkovartija käytti alkuperäistä rajaa. | Sama rajattu lisäajan päättymishetki kulkee lähetykseen asti ja säilyy uudelleenkäynnistyksessä. Uusi ajo ei avaa liukuvaa lisäaikaa. |
| Jonon osallistujanäkymän valmistelu ehti vanhentaa hintaa ja varaustietoa; uusintaviive jäi kahteen sekuntiin. | Tila ja hinta tarkistetaan vielä ennen jonopyyntöä. Molemmat jonopolitiikat noudattavat palvelimen ilmoittamaa pidempää odotusaikaa. |
| Julkinen tarkistin hyväksyi olemassa olevia mutta käyttökelvottomia tuote-, hinta-, aika- tai reittikenttiä. | Tarkistus validoi myös arvot ja selainadapterin hyväksymän kurssireitin. Negatiiviset jäljellä olevien paikkojen määrät raportoidaan erikseen; vähintään yksi käyttökelpoinen edustava rivi vaaditaan. |
| Saldo-odotuksen ehto ohittui testifixtureissä kokonaan. | Selainfixture sisältää Enkoran saldoasetuksen, todellisen näkymä–kokoelma-suhteen ja maksutapojen sekä saldon molemmat valmistumisjärjestykset. Lopulliset ehdot ja maksutapa tarkistetaan lähetettäessä. |

Keskeiset uudet regressiot ovat tiedostossa `tests/test_mutation_evidence.py`. Julkisen tarkistimen negatiiviset arvotapaukset ovat tiedostossa `tests/test_shop_contract.py` ja erillisten renderöintien selainregressiot tiedostossa `tests/test_booking_flow.py`.

## Testinäytön parannukset

Tavallinen CI ajaa nyt paikalliset testit valitsimella `--fail-on-skip`. Yksikin ohitus epäonnistuttaa ajon; selaimen puuttuminen ei siten tuota vihreää lopputulosta. Portti tarkistettiin sekä tarkoituksella ohitetulla väliaikaisella testillä että tämän työympäristön kahdeksalla selainohituksella.

`scripts/smoke_browser.py` liitettiin CI:hin. Alkuperäisen usean kurssin jonotus- ja maksuketjun lisäksi se tarkistaa yhden kurssin varsinaisen `open_booking_browser`-adapterin, maksamattoman korin näyttämisen ja tallennetusta tilasta jatkamisen ilman uutta korilisäystä tai maksua. Molemmat smoken jatkot käyttävät samaa avointa selainistuntoa; ne eivät todista suljetun selaimen istuntoevästeiden tai ostoskorin säilymistä. Tämä on kokonainen paikallinen selainajo; se ei aja oikeaa Urheiluhallien ostoa eikä koko komentorivivalikkoa.

Lisäksi `scripts/check_live_shop.py` lukee erikseen oikean kaupan kolme julkista lähdettä HTTPS GET -pyynnöillä: HTML:n, HTML:n ilmoittaman saman alkuperän varausmoduulin JavaScriptin ja kurssiluettelon. Se tarkistaa tuotannossa käytettävällä `parse_course`-funktiolla kurssien tulkinnan, vaadittujen HTML-elementtien rakenteen ja JavaScriptin rajapintamerkit. Se ei kirjaudu, lue yksityisiä päätepisteitä tai lähetä kirjoittavia pyyntöjä.

Tarkistimen ensimmäiset 11 testiä rikkovat esimerkiksi kurssin avainkentän, osallistujavalinnan rakenteen, maksupainikkeen, JavaScript-lähteen alkuperän ja `R2.cart`-määrittelyn. Keskeisiä tarkistuksia poistamalla varmistettiin erillisissä väliaikaisissa kopioissa, että vastaavat regressiotestit todella havaitsevat poiston. Katselmoinnin jälkeen mukaan lisättiin myös kelvottomat kenttäarvot ja erikseen raportoitava negatiivinen jäljellä olevien paikkojen määrä.

Julkinen tarkistus on omassa käsin käynnistettävässä **Live public shop contract** -työnkulussa. Sitä ei ajastettu. Paikallinen testisarja ei ole riippuvainen tuotantokaupan hetkellisestä saatavuudesta.

## Julkisen lähdenäytön alkuperä

Oikean kaupan käyttöliittymä tarkastettiin selaimessa kirjautumatta. Näkymän rakenne, osallistuja- ja ostoskorimallit sekä pyyntöjen muodostus tarkastettiin julkisesta HTML:stä ja JavaScriptistä. Raakoja tuotantovastauksia tai kuvakaappauksia ei tallenneta repositoryyn.

Tiukennettu automaattinen julkinen tarkistus läpäisi **22.11.2026 klo 9.15.37 Suomen aikaa (07.15.37 UTC)**. Se tulkitsi **143 kurssia**, joista kaikilla oli ei-negatiivinen jäljellä olevien paikkojen ja jonopaikkojen määrä. Myös ensimmäinen tarkistus klo 8.52 läpäisi; alla ovat tiukennetun tarkistuksen lähteet.

| Lähde | SHA-256 tarkistushetkellä |
| --- | --- |
| [Julkinen kauppasivu](https://kauppa.urheiluhallit.fi/webshop/reservations2/) | `63ab6f20265591a4303ae38adc16a3c3220ed85f63e901dbb40d30a675168cb1` |
| [Sivun ilmoittama varausmoduulin JavaScript](https://kauppa.urheiluhallit.fi/webshop/javascript/jsmodule/reservations2/27407640c7bd4974e2eea14d5d7fc06e) | `1e15bbdd38036f6145ec39a3fe21957b03fece9c5df57076134271f044dd2320` |
| [Julkinen uimakoululuettelo](https://kauppa.urheiluhallit.fi/webshop/reservations2/apieventgroups?service_id=9) | `fb962c2bbe98306631ed772ecb19dfc8d98cc0a7539c1d26e48714b93df856a4` |

Tarkistussummat yksilöivät käytetyn lähdeaineiston. Tarkistin ei vaadi tulevilta vastauksilta samaa summaa: muuttuva kurssiluettelo on normaalia. HTML- ja JavaScript-tarkistukset kattavat nimettyjä rakenteita, eivät selaimen ajonaikaista toimintaa tai kaikkia kolmannen osapuolen muutoksia.

## Lopullinen varmennus

Katselmoinnin korjausaallon jälkeinen yhdistetty paikallinen ajo palautti **390 läpäistyä testiä, 9 ohitettua selaintestiä ja 323 läpäistyä alitestiä**. Lähtötilanteeseen verrattuna mukaan tuli myös viivästetyn FastPay-valikon selainregressio. Ruff, ty, `git diff --cached --check` ja Git-indexin **64 lähdetiedoston** julkisuusraja läpäisivät.

Lopullisen lähdekoodin [GitHubin pakollinen selainajo](https://github.com/ssalonen/urheiluhallit-kone/actions/runs/44998010031) läpäisi **399 testiä ja 323 alitestiä ilman ohituksia**. Molemmat koko paikallisen Chromium-smoken polut läpäisivät. Monen kurssin palvelintilassa oli kaksi jonopyyntöä, yksi korilisäys, yksi checkout, yksi vahvistettu varaus ja yksi simuloitu korttiveloitus. Maksamattoman yhden kurssin polussa oli yksi korilisäys ja nolla checkoutia tai kuittia.

Riippumaton koko haaran katselmointi löysi ensimmäisen toteutuksen jälkeen vielä lähetyshetken sitomiseen, maksutiedon puuttumiseen, myöhäisen varaushavainnon säilyttämiseen, jonotuksen määräaikaan ja julkisen tarkistimen arvoihin liittyviä puutteita. Kohdennettu uusintakatselmointi hyväksyi yhdeksän kymmenestä korjatusta kohdasta eikä löytänyt uutta olennaista virhettä.

Viimeinen avoin kohta koski palvelimen uusintaviivettä: uusi jonopyyntö odotti, mutta seuraava samaa tiliä lukeva kysely lähti heti. Korjaus laajennettiin koskemaan myös yhteisiä lukuja, rajattuna ajon määräaikaan. Ennen tätä muutosta laajennettu regressio epäonnistui molemmilla jonopolitiikoilla; sen jälkeen kaikki 100 kohdennettua koordinointi- ja lähetysrajatestiä läpäisivät. Myös yllä linkitetty koko CI-ajo kattaa tämän muutoksen. Viimeinen pieni muutos varmennettiin toteuttajan regressiolla, staattisilla tarkistuksilla ja CI:llä; sille ei väitetä erillistä kolmatta koodikatselmointia.

Haaran ajantasaiset portit ovat [luonnos-PR:n tarkistuksissa](https://github.com/ssalonen/urheiluhallit-kone/pull/7/checks). CI ajaa koko joukon ja molemmat smoket jokaiselle julkaistulle muutokselle, myös viimeisen uusintaviivekorjauksen jälkeen.

## Jäljellä olevat rajat

- Repon oman automaation kirjautunut varausajo, lapsitilin valinta, ostoskorin vastausrakenteet, jonotus, maksu ja varaus–kuitti-yhteys vaativat erikseen todennettavan integraation. Julkinen GET-tarkistus ja erillinen aikuisen käyttöliittymäkoe eivät kata niitä.
- Maksun varmistukseen vaadittu kuittiviite voi puuttua oikeasta vastauksesta tai kuulua eri tunnistejoukkoon. Todentamaton numeroyhtäläisyys ei kelpaa tuotannon maksutodisteeksi. Työkalu ei saa päätellä puuttuvasta vahvistuksesta maksun epäonnistuneen ja lähettää maksua uudelleen.
- Testikauppa on tarkoituksella yksinkertaistettu. Sen ulkoasun yhtenäistäminen ei yksin poistaisi riippumattoman rajapinta- ja selainnäytön tarvetta.
- `koeajo --monta 1` käyttää edelleen monen kurssin poolia, kun varsinainen yhden kurssin ajo käyttää omaa selainadapteria. Uusi smoke kattaa tämän adapterin ja maksamattoman korin näyttämisen erikseen. Koko ohjatun CLI:n läpikäynti ei kuulu siihen.
- Vanha yhteinen varaustallenne estää ehdokasjoukon vaihtamisen myös sellaisen päättyneen ajon jälkeen, jossa ei lähetetty yhtään kirjoitusta. Tätä käytettävyyshaittaa ei muutettu tässä maksuturvallisuuden korjauksessa. Epäselviä varaus- tai maksutallenteita ei pidä poistaa uuden yrityksen saamiseksi.
- Selainympäristön, istunnon tai tuotannon vastauksen poikkeama voi edelleen pysäyttää etenemisen. Korjauksissa epäselvyys painotetaan havaitun varauksen säilyttämiseen ja uuden maksun estämiseen.

## Oikean kaupan maksamaton käyttöliittymäkoe 22.11.2026

Koe tehtiin oikeassa Urheiluhallien kaupassa käyttäjän itse avaamalla kirjautuneella istunnolla. Selainkäyttöliittymää ohjattiin suoraan; repon Python-ohjelmaa, selainadapteria tai maksupyyntöjen verkkovartijaa ei ajettu tässä kokeessa. Kohteena oli avoin maksullinen kurssi palveluryhmässä **Kurssit**, osallistujana tilin oma aikuinen. Alkuperäiset kohdeuimakoulut eivät vielä olleet varattavissa. Repon selainadapteri rajaa kurssiosoitteet Uimakoulut-palveluun, joten toisen palveluryhmän koe ei osoita sen tuotantotoimivuutta.

| Vaihe | Käyttöliittymästä havaittu tulos |
| --- | --- |
| Lähtötilanne | Saman kirjautuneen sivun renderöity ostoskoripaneeli näytti 0 tuotetta ja 0 €. Paneeli oli tyhjänä piilotettu. Testikurssilla oli 8 vapaata paikkaa. |
| Osallistujavalinta | Oikean kurssin valintaikkunassa valittiin oma aikuistili. Kurssin ajankohta, paikka ja 35 euron hinta tarkistettiin ennen lisäystä. |
| Korilisäys | **Siirrä ostoskoriin** valittiin kerran. Asynkronisen päivityksen jälkeen ostoskorissa näkyi yksi kurssirivi ja 35 euron kokonaishinta. |
| Syntynyt varaus | Kurssin omassa osallistujataulukossa näkyi valittu osallistuja tilassa **Ei vahvistettu**. Vapaita paikkoja oli 7. Pelkkään koririviin ei tyydytty varauksen todisteena. |
| Maksuraja | Käyttöehtojen hyväksyntä jäi valitsematta. Verkkomaksua, FastPayta tai muuta maksutoimintoa ei käynnistetty. Maksupyyntöjen lukumäärää ei mitattu verkkotasolla. |
| Siivous | Vain testirivin **Poista** valittiin. Kauppa ilmoitti, ettei korissa ole tuotteita, ja keskeneräinen varaus poistui kurssin osallistujataulukosta. |
| Tarkistus uudelleenlatauksen jälkeen | Kirjautuminen säilyi, ostoskorissa oli 0 tuotetta ja 0 €, testivarausta ei näkynyt ja vapaiden paikkojen määrä palautui kahdeksaan. |

**Tulos: oikean kaupan maksamaton kurssivaraus ja sen poistaminen toimivat tässä käyttöliittymäkokeessa.** Testistä ei jäänyt kurssipaikkaa varatuksi. Varaus oli väliaikainen ja vahvistamaton; maksettua tai lopullisesti vahvistettua kurssipaikkaa ei muodostettu tässä kokeessa.

Koe ei osoita, palauttaako kirjautunut kurssirajapinta adapterin vaatiman nimenomaisen `is_paid=false`-kentän tai todennettavan kuittiviitteen. Tilateksti **Ei vahvistettu** on käyttöliittymähavainto. Myös lapsitilin valinta, jonotus, avautumishetken kilpailu ja repon `--ilman-maksua`-ajon `held_unpaid`-tulos jäävät erikseen testattaviksi. Henkilötietoja, tilin tai varauksen tunnisteita, raakavastauksia ja kuvakaappauksia ei sisällytetty repositoryyn.

## Oikeasta HTML:stä poimittu simulaattori ja uusi erovertailu

Käyttäjän pyynnöstä simulaattorin näkymät muutettiin käyttämään oikean kaupan julkisesta HTML:stä poimittuja rakenteita. Lähde on anonyymi `https://kauppa.urheiluhallit.fi/webshop/reservations2/` -tallenne. Käytetyn sivun SHA-256 on `d135fdb5f6cd46f5a8777074fabbb3eece206af984e13eeeda54b38d8ef51a27`, ja julkisen JavaScriptin `1e15bbdd38036f6145ec39a3fe21957b03fece9c5df57076134271f044dd2320`.

`scripts/extract_shop_templates.py` muuntaa tallenteen kahdeksaksi inertiksi HTML-malliksi. Se säilyttää tarkistettujen elementtien alkuperäiset esi-isät ja turvalliset attribuutit, korvaa dynaamisen sisällön tietopaikoilla sekä poistaa muut ehdolliset haarat. Mallien omat lähdetiivisteet tallennetaan tuotettuun `uhkone/testshop_templates.html`-tiedostoon. Poiminta samasta lähteestä tuottaa tavulleen saman tiedoston. Testi muuttaa synteettisen lähteen otsikon ympärillä olevaa elementtiä ja osoittaa muutoksen säilyvän tuloksessa; puuttuva tai suoritettavaa sisältöä sisältävä malli hylätään. Tarkistin hylkää myös malliin jääneen osallistujan nimen, tunnisteen, tapahtumakäsittelijän tai ulkoisen resurssin.

| Lähdemalli | Simulaattorissa käytettävä rakenne |
| --- | --- |
| `template_eventgroup` | Kurssikortti, otsikko, tietojen ja varausten paikat, hinta sekä osto- ja jonopainikkeet. |
| `template__eventgroup_details` | Halli, ajanjakso ja viikkoaikataulu. |
| `template__eventgroup_buy` | Osallistujavalinnan ikkuna ja vahvistuslinkki. |
| `template__participant_select` | Oma tili, lapsilistan avaava valinta ja olemassa olevat lapsitilit. |
| `template__eventgroup_reservations` | Osallistuja ja kurssivarauksen tila taulukkona. |
| `template_cart` | Korin taulukko, hintayhteenveto, saldo, ehdot ja maksutapojen alue. |
| `template__cartitem` | Tuotteen nimi, ALV, hinta ja Poista-painike. |
| `template_cartwidget` | Piilossa oleva tyhjä kori sekä tuotemäärä ja summa. |

### Todennetut erot ja tehdyt korjaukset

| Ero tai virhe | Vaikutus ja korjaus |
| --- | --- |
| Vanha simulaattori näytti korilisäyksen jälkeen vain tekstiviestin. | Kori avautuu onnistuneen lisäyksen palauttamista riveistä, kuten kaupan `addEventGroup`-polussa. Pelkkä käyttöehtojen valintaruutu ei enää kelpaa korinäkymän testiksi. |
| Kurssin osallistujan varaustila ja todelliset koririvit puuttuivat. | Näkymät täyttyvät paikallisen palvelimen oikeasta tilasta. Testi tarkistaa saman kurssin, osallistujan, hinnan ja **Ei vahvistettu** -tilan. |
| Lapsilistan avauskenttä kuului simulaattorissa osallistujaradioiden ryhmään. | Poimittu rakenne säilyttää oikean kaupan kentän ilman `name=participant`-attribuuttia. Pelkkä listan avaaminen poistaa aikuisen valinnan eikä lähetä varausta. |
| Koririvin poistamista ei voinut testata. | Paikallinen `cart/deleteCartItem` hyväksyy vain tarkan istunnon, tuotteen ja maksamattoman koririvin. Poisto vapauttaa varauksen atomisesti. Väärä tuote, toinen istunto, maksettu rivi ja vanhan poiston toisto eivät muuta varausta. |
| Toisen kurssin sivulta avattu kori muodosti maksupaluun väärän kurssin osoitteeseen. | Testipalvelin hylkäsi maksupyynnön, vaikka korin sisältö oli oikea. Paluuosoite on nyt yhteinen paikallisen kaupan osoite. Kahden kurssin regressio tarkistaa, että maksetaan korin kurssi eikä näkyvää toista kurssia. |
| Maksutavat ja saldo eivät aiemmin valmistuneet omina korin uudelleenrenderöinteinä. | Molemmat aidot paikalliset HTTP-lukupyynnöt voivat valmistua eri järjestyksissä. Selainregressiot tarkistavat, että adapteri odottaa molemmat ennen simuloitua FastPay- tai saldomaksua. |
| Korin avaaminen olisi voinut jäädä myöhemmän kurssiluvun onnistumisen varaan. | Onnistuneen POST-vastauksen koririvi avataan heti. Seuraavan GET-pyynnön 503 ei saa piilottaa sitä eikä aiheuttaa uutta korilisäystä. |
| Aloituksen epäonnistunut koriluku saattoi peittää jo ladatut kurssit eikä korilukua uusittu. | Kurssinäkymä säilytetään, ja alkuperäinen koritila luetaan uudelleen. Regressio palauttaa ensimmäiseen korilukuun 503:n ja edellyttää toipumista ilman sivun uudelleenlatausta. |
| Vanha osallistujavalinnan callback ja päällekkäiset lukuvastaukset saattoivat korvata uudemman näkymän. | Modal-päivitys tarkistaa edelleen omistavansa näkymän. Kurssi- ja koriluvuilla on järjestysnumerot, joten vanhempi vastaus ei korvaa uudempaa tilaa. |
| Käsin avattu simulaattori ei asettanut selaimen sisältöturvakäytäntöä. | CSS, JavaScript ja mallit tarjoillaan täsmällisistä paikallisista osoitteista. CSP, `nosniff`, resurssireittien testit ja erillisen selaimen ulkoisten pyyntöjen valvonta täydentävät nykyistä Host/Origin-tarkistusta. |

Lapsilistan epäiltyä asynkronista latausbugia **ei vahvistettu**: julkisessa asiakaskoodissa olemassa olevat lapsiradiot syntyvät samalla renderöinnillä kuin listan avauskenttä. `_select_child`-toimintoa ei muutettu kuvitteellisen viiveen takia. Myöskään vahvistetun varauksen tai kuitin todistusvaatimuksia ei heikennetty.

Paikallinen koko testijoukko läpäisi tämän lisäyksen yhteydessä **408 testiä ja 345 alitestiä**. **15 Chromium-testiä ohitettiin** ympäristön selainkäynnistyseston takia. Ruff, ty, JavaScriptin syntaksitarkistus, resurssien paketointi ja mallien toistettava poiminta tarkistettiin erikseen. Uusi Chromium-kokonaisuus tarkistaa normaalin lisäys–poisto–uudelleenlataus-polun, kurssiluvun 503-virheen lisäyksen jälkeen molemmat maksutietojen valmistumisjärjestykset, toisen kurssin sivulta maksamisen sekä aloituksen epäonnistuneen koriluvun toipumisen. Niiden suoritus on CI:n pakollinen, ohitukset hylkäävä portti; tämän lisäyksen CI-tulos kirjataan tarkistuksen valmistuttua.

Riippumaton katselmointi löysi puuttuvan saatavuuskohdan hyväksymisen poimijassa, toisen kurssin paluuosoitevirheen ja aloituksen koriluvun toipumisvirheen. Kohdennettu uusintakatselmointi hyväksyi korjaukset eikä jättänyt P1/P2-havaintoja avoimeksi. Lisäksi provenance-kommentin päällekkäiset JSON-avaimet estettiin ensin epäonnistuneella regressiolla. Ei-estäväksi käytettävyyshavainnoksi jäi modaali-ikkunan näppäimistökohdistuksen siirtäminen ja palauttaminen.

### Mitä poiminta ei todista

CSS on paikallinen toteutus, eikä koko kaupan teemaa, fontteja, kuvia tai JavaScript-pakettia kopioida. Uusien lapsitilien luonti, lahjakortit, kaikki suodattimet, muiden palveluryhmien näkymät ja ulkoiset maksupalvelut eivät kuulu jäljiteltyyn polkuun. Kori ei näytä osallistujaa tuotteen omana tietona, koska oikeakin kauppa näyttää sen kurssin varausrivillä. ALV-erittely on ilmoitettua testidataa.

Poimittu HTML parantaa rakenteen ja käyttöliittymän vaiheiden vastaavuutta. Se ei osoita kirjautuneen tuotantorajapinnan `is_paid`- tai kuittikenttien saatavuutta. Aiempi oikean kaupan maksamaton käyttöliittymäkoe pysyy erillisenä näyttönä; tässä lisäyksessä ei tehdä uutta oikeaa varausta tai maksua.

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

Viimeinen komento käyttää julkista tuotantokauppaa. Listan muut testikomennot käyttävät paikallisia sivuja ja kuvitteellisia osallistujia. Yllä kuvattu kirjautunut käyttöliittymäkoe oli erillinen kertakoe, eikä sitä ajeta näillä komennoilla tai CI:ssä. Julkisen lähdepuun tarkistin lukee Git-indexin, joten julkaistavat muutokset pitää tarkastaa myös staging-vaiheessa.
