# Urheiluhallien ilmoittautumiskone

Paikallinen komentoriviohjelma, joka ohjaa **oikeaa näkyvää Chromium-selainta** ja pitää ihmisen mukana varaamisessa. Ohjelma kertoo etenemisestään, odottaa ilmoittautumisen avautumista ja tavoittelee yhtä maksettua kurssipaikkaa etukäteen valituista vaihtoehdoista. Oletushaku on leikki-ikäisten jatkouimakoulu: ensin Mäkelänrinne, sitten Kallio, arkipäivisin aloitus aikaisintaan 17.20 ja viikonloppuisin aikaisintaan 9.30.

Versiossa **0.5** on ohjattu käyttötila, jatkuvat tilaviestit sekä komennot ajon pysäyttämiseen ja jatkamiseen. Tavoitteena on yksi vahvistettu kurssipaikka. Haluttaessa ohjelma liittyy myös sopivien kurssien jonoihin varsinaisen paikan etsimisen aikana.

Ohjelma ei ole Urheiluhallien tai Enkoran virallinen tuote. **Repon automaatiolla ei ole vielä todennettu varausta tai FastPay-maksua oikealla asiakastilillä.** Oikean kaupan käyttöliittymässä tehty erillinen maksamaton varauskoe läpäisi 22.11.2026: aikuisen oma osallistujavalinta, yksi kurssi ostoskoriin, keskeneräinen varaus ja testivarauksen poistaminen varmistettiin. Tämä koe ei käyttänyt repon selainadapteria eikä testannut lapsitiliä tai maksamista. Testikaupassa onnistuva ajo ei vielä osoita oikean kaupan kaikkien vaiheiden toimivan. Palvelun ruuhka tai loppuvat paikat voivat estää varaamisen.

**Laadunvarmistus 22.11.2026:** oikean kaupan julkisen selainkoodin vertailu paljasti maksupyynnön muotoeron, jota simulaattori ei testannut. Lisäksi tarkastuksessa löytyi virheitä maksun vahvistamisessa ja häiriöistä palautumisessa. [Tarkastusraportti](docs/laadunvarmistus-2026-11-22.md) kertoo löydökset, korjaukset, testinäytön ja vielä todentamatta olevat vaiheet. Automaattisen maksun tuotantokelpoisuutta ei ole osoitettu.

**Kirjautuneen JSON-kokeen löydös:** oikean kaupan maksamaton varaus lisättiin ja poistettiin, ja ostoskorin sekä kurssivarausten JSON otettiin talteen kolmessa vaiheessa. Havaitussa kurssivastauksessa ei ole `is_paid`-kenttää. Maksuton pysäytys tunnistaa tämän vuoksi erikseen täsmällisen ostoskoririvin ja saman osallistujan keskeneräisen kurssivarauksen; tuntematonta maksutilaa ei muuteta maksuluvaksi. Koe koski Kurssit-palvelun aikuista, eikä se ole repon Uimakoulut-adapterin tuotantoajo.

**Tuotantomaksun vahvistusraja:** tässä korjauksessa oikean kaupan uuden maksun `confirmed`-tulosta ei muodosteta todentamattoman kuittiyhteyden perusteella. Maksupyyntö voi silti tulla lähetetyksi kerran, jos muu varaustieto sallii sen. Onnistunutkin maksu voi jäädä ohjelmassa vahvistamatta: tarkista tilanne oikean kaupan omista varauksista ja kuitista. Puuttuva vahvistus ei tarkoita maksun epäonnistumista eikä oikeuta uuteen maksuyritykseen.

Interaktiivinen esimerkkiajo: [avaa HTML](docs/esimerkkiajo.html) (lataa tiedosto ja avaa selaimessa). Esimerkki toimii paikallisesti kuvitteellisilla kursseilla ja osallistujalla.
Esimerkin ohjauspaneelissa voi valita myös pysähtymisen maksamattomaan ostoskoriin. HTML on itsenäinen havainnollistus eikä käynnistä komentoriviohjelmaa tai tee verkkopyyntöjä.

## Käyttöönotto

Tarvitset Python 3.11:n tai uudemman sekä graafisen työpöytäympäristön.

```sh
git clone https://github.com/ssalonen/urheiluhallit-kone.git
cd urheiluhallit-kone
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
```

Windowsissa aktivointi on `.venv\Scripts\Activate.ps1` ja Python-komento yleensä `python`. Linuxissa ja macOS:ssä myös `./kaynnista.sh ohjattu` käynnistää ohjelman ja asentaa tarvittaessa Python-riippuvuudet. Chromium asennetaan erikseen yllä olevalla komennolla. Päivityksen jälkeen aja `python -m pip install -e .` uudelleen.

## Aloita ohjatusti

```sh
uhkone ohjattu
```

Ohjattu tila käy kanssasi läpi kurssivaihtoehdot, osallistujan, enimmäishinnan, maksutavan ja toiminnan hyväksymisen. Hae kurssit niiden julkaisun jälkeen; ilmoittautumisen ei tarvitse vielä olla avoinna. Voit käyttää etukäteen kopioituja kurssiosoitteita tai kurssitunnuksia.

Anna lapsen nimi vain oman koneesi ohjattuun kyselyyn. Näin nimi ei päädy käynnistyskomennon mukana päätteen komentohistoriaan. Lapsitilin pitää olla jo valmiina Urheiluhallien asiakastilillä. Työkalu ei luo lapsitilejä tai tallenna salasanoja asetuksiin.

Selain avautuu omaan pysyvään profiiliin. **Kirjaudu itse kauppaan näkyvässä selainikkunassa.** Istunto säilyy seuraavia ajoja varten. Työkalu käyttää samaa istuntoa sekä käyttöliittymän ohjaamiseen että varauksen tilan lukemiseen. Mahdollinen pankin tai kortinmyöntäjän lisätunnistautuminen tehdään itse selaimessa.

Valitse toimintatapa ennen ilmoittautumisen alkamista:

- **Automaattinen ajo:** hyväksyt rajatun suunnitelman etukäteen. Ohjelma saa varata ja maksaa hyväksytyn kurssin enimmäishintaan asti. Tiedot ja eteneminen näkyvät jatkuvasti päätteessä.
- **Vaiheittaiset hyväksynnät:** `--kysy-toiminnot` pyytää hyväksynnän ennen varaamista, jonoon liittymistä ja maksamista. Vastaa `kyllä` tai `ei`. Vastauksen odottaminen voi hidastaa paikan saamista.

Hyväksyntä koskee valittuja kursseja, osallistujaa, maksutapaa ja hintarajaa. Ohjelma ei vaihda puuttuvaa FastPayta toiseen maksutapaan tai osta muita korissa olevia tuotteita.

### Ohjaus ajon aikana

Kirjoita komento päätteeseen ja paina Enter:

| Komento | Toiminta |
| --- | --- |
| `tila` | Tulostaa nykyisen työvaiheen. |
| `tauko` | Pysäyttää etenemisen seuraavassa ohjauspisteessä. |
| `jatka` | Jatkaa tauolta. |
| `lopeta` | Lopettaa ajon ja säilyttää etenemistilan. |
| `apu` | Näyttää käytettävät komennot. |

Ohjelma kertoo myös ilman kysymistä, odottaako se avautumista, selvittääkö se palvelinvirhettä vai tarkistaako se varausta tai maksua. Tauko tai lopetus **ei peru jo lähetettyä varausta tai maksua**; palvelin voi käsitellä pyynnön loppuun taustalla. Jatka keskeytynyttä varausta samoilla kurssivalinnoilla ja samalla selainprofiililla. Älä poista etenemistietoja epäselvän maksun uusimiseksi.

Valitsimet `--keskustele` ja `--kysy-toiminnot` tuovat ohjauksen myös suoriin varauskomentoihin. Ohjattu käyttö on suositeltu tapa antaa osallistujan tiedot; suoran komennon `--lapsi` voi jättää nimen oman koneesi komentohistoriaan.

## Useita kurssipolkuja, yksi maksettu paikka

### Varaa ostoskoriin ilman maksua

`--ilman-maksua` käy läpi saman kurssi- ja lapsivalinnan sekä varausyrityksen, mutta pysähtyy, kun täsmällinen maksamaton kurssirivi on tarkistettu ostoskorissa. Maksutapaa ei anneta, eikä ohjelma aloita maksua. Ostoskorissa oleva paikka voi vanhentua eikä ole maksettu tai lopullisesti vahvistettu kurssipaikka.

Oikeasta kaupasta havaitussa muodossa tämä edellyttää yhtä oikean kurssin, myyntituotteen ja osallistujan koririviä, hyväksyttyä hintaa, vastaavaa yksilöityä tilan 7 kurssivarausta, nimenomaista `payment_event_id: null` -kenttää ja lukitsematonta koria. Ristiriitainen tieto estää hyväksymisen. Puuttuvan `is_paid`-kentän maksutila pysyy tuntemattomana: tämä näyttö riittää vain maksuttoman ajon pysäytykseen, eikä se salli maksun aloittamista.

```sh
uhkone varaa-yksi KURSSITUNNUS --lapsi "Lapsen tarkka nimi" --enintaan 76 --ilman-maksua --ei-jono
```

Valitsin toimii myös `varaa`-komennossa. Ohjatussa tilassa valitse maksutavan sijaan "pysähdy maksamattomaan ostoskoriin". Työkalu avaa tarkistetun ostoskorin näkyvään selaimeen ja odottaa, että suljet ikkunan; tämän ajon selainverkkoesto estää maksupyynnöt myös ostoskorin näkymässä. Samaa ajoa voi jatkaa samalla selainprofiililla ja tilahakemistolla ostoskorin tilanteen tarkistamiseksi; tämä tila ei muutu myöhemmässä ajossa automaattisesti maksulliseksi.
Tässä tilassa jonoihin ei liitytä oletuksena; `--jono` sallii sen erikseen.

`varaa-yksi` hyväksyy useita etukäteen valittuja kurssiosoitteita tai tunnuksia **antamassasi etusijajärjestyksessä**. Mitään muita kursseja ei haeta ehdokkaiksi. Annettujen tunnusten kurssitiedot tarkistetaan palvelusta, ja kurssitason, hallin sekä aloitusajan hakuehdot ovat edelleen turvarajoja. Ohjelma avaa niille erilliset välilehdet samassa selainprofiilissa. Julkisia kurssitietoja luetaan rinnakkain rajatulla määrällä pyyntöjä. Varsinaiset varaus- ja maksutoiminnot käyttävät yhteistä vuoroa.

Jos useampi sopiva paikka on samalla tarkistuksella saatavilla, **syöttämäsi kurssilistan järjestys** ratkaisee. Pelkästään Kallion paikan ollessa valmis sitä voidaan tavoitella heti. Ohjelma ei odota mahdollista myöhempää Mäkelänrinteen paikkaa.

**Kun varsinainen varauspyyntö on voinut lähteä, muut kurssien ostot lukitaan.** Valinta tallennetaan levylle ennen lähettämistä. Työkalu selvittää tämän varauksen ja sen maksun tuloksen ennen muuta etenemistä. Se ei käynnistä toisen kurssin ostoa pelkän aikakatkaisun tai tyhjän tilavastauksen perusteella. Maksua ei lähetetä uudelleen epäselvän vastauksen jälkeen.

Onnistuminen edellyttää oikean osallistujan oikean kurssin vahvistettua, maksetuksi merkittyä varausta. Uuden maksun osalta suljetun ja oikeansummaiseksi tarkistetun kuitin pitää lisäksi liittyä samaan varaukseen. Pelkkä uusin samanhintainen kuitti, ostoskoririvi, onnistumissivu tai ”Ei vahvistettu” -varaus ei riitä. Jos kaupan vastaus ei todista varauksen ja kuitin yhteyttä, ohjelma jättää maksun vahvistamatta eikä lähetä uutta maksua. Oikean kirjautuneen kurssirajapinnan kuittiyhteys on vielä todennettava; simulaattorin kenttä tai kahden todentamattoman tunnisteen numeroyhtäläisyys ei ole siitä todiste. Julkisen kauppakoodin kuittikenttiä käyttävä muu varausnäkymä rajaa kurssit pois. Työkalu ei voi hallita käsin, toisella laitteella tai toisessa paikallisessa tilahakemistossa tehtyjä ostoksia.

Oletusrajat ovat asetustiedostossa `asetukset.toml`. `haku` ja `vahti` eivät tee varauksia tai tarvitse kirjautumista:

```sh
uhkone haku
uhkone haku --vapaat
uhkone haku --arki-aikaisintaan 17:30 --viikonloppu-aikaisintaan 09:30
uhkone kurssi KURSSITUNNUS
uhkone vahti
```

Viikonpäivät ovat ma=1 … su=7. Raja-ajat hyväksytään mukaan, ja päiväkohtainen kellonaikaraja koskee kaikkia kurssin ilmoitettuja tapaamisia. Oletuksena näytetään tänään tai myöhemmin alkavat kurssit. Puuttuva tieto ei läpäise siihen kohdistuvaa rajausta. `--vapaat` tarkoittaa varsinaisia kurssipaikkoja, ei jonopaikkoja.

Vahti tarkistaa tilanteen oletuksena viiden minuutin välein. Sen pitää olla käynnissä omalla tietokoneella; se ei luo pilviajastusta eikä lähetä sähköpostia. Myös varausajon aikana koneen on pysyttävä hereillä ja verkkoyhteyden toimittava.

## Jonopaikat

`[varaus] jono_varavaihtoehtona = true` sallii jonotuksen. `--jono` ja `--ei-jono` ohittavat asetuksen yksittäiselle ajolle. Jonovaihtoehtojen pitää täyttää samat kurssitason, hallin, viikonpäivän ja kellonajan rajat kuin varsinaisten paikkojen.

Sopivan täyden kurssin jonoon voidaan liittyä heti jonon ollessa avoinna. Ohjelma voi liittyä **usean hyväksytyn kurssin jonoon**, samalla kun varsinaisen paikan etsintä jatkuu. Jono ei ole maksettu kurssipaikka eikä se estä varsinaisen paikan tavoittelua. Varsinainen vapaa paikka saa etusijan, kun molemmat ovat samalla tarkistuksella tarjolla.

Julkisen näkymän ”1 varapaikkaa” tarkoittaa yhtä vapaata jonopaikkaa. Se ei ole vapaa kurssipaikka tai osallistujan henkilökohtainen jonosija. Jonoon liittyminen käyttää omaa toimintoa; siitä ei käynnistetä kurssin maksua. Työkalu tarkistaa syntyneen jonovarauksen omista varauksista. Epäselvä jonovastaus selvitetään ilman toista samalle kurssille lähetettävää jonopyyntöä.

Jonot ilmoitetaan erikseen vahvistetusta kurssipaikasta. **Olemassa olevia jonovarauksia ei peruta automaattisesti**, vaikka varsinainen kurssipaikka löytyisi. Jonoon liittymisen ja perumisen ehdot tarkistetaan Urheiluhalleilta; työkalu ei päättele niitä kurssin hinnasta.

[Urheiluhallien uimakoulujen FAQ:n](https://www.urheiluhallit.fi/palvelut/uimakoulut/uimakoulujen-faq/) mukaan vapautuneesta paikasta lähetetään sähköposti samanaikaisesti jonottajille: ”Nopeiten varauksen tehnyt saa paikan. Jononumerollasi ei ole merkitystä.” Jonotus ei tarkoita automaattista siirtoa maksetulle kurssille. Tämä ohjelma ei lue sähköpostia eikä aloita myöhempää ostoa viestin perusteella.

## Maksutavat ja selainistunnot

- **FastPay:** käyttää Enkoran asiakastunnukseen liitettyä maksukorttia. [Enkoran ohjeen](https://enkora.fi/fi/tuote/fast-pay/) mukaan kortti liitetään vahvalla tunnistautumisella ja myöhempi maksu voi onnistua yhdellä klikkauksella. Työkalu valitsee kaupan FastPay-painikkeen kerran. **Oman kortin valmius ja oikean kaupan toiminta pitää vielä varmistaa omalla koneella.**
- **Saldo:** käyttää kaupan tarjoamaa saldoa, jos se riittää koko kurssiin.
- **Verkkomaksu:** avaa kaupan maksupalvelun. Tee maksuvälineen valinta ja mahdollinen tunnistautuminen selaimessa; ohjelma seuraa lopputulosta.

Korttinumeroa, turvakoodia tai pankkitunnuksia ei anneta komentoriviohjelmaan. Niitä käsitellään vain kaupan tai maksupalvelun omassa käyttöliittymässä. Jos maksutapavaihtoehtoja on useita, valitse sopiva ennen ilmoittautumisen avautumista.

Kahdella kirjautuneella selaimella tehdyssä havainnossa **ostoskoreilla oli eri sisältö**, mutta saman asiakastilin keskeneräiset varaukset näkyivät selainten välillä. Ohjelman malli erottaa siksi istuntokohtaisen ostoskorin ja tilikohtaiset varaukset. Ostoa jatketaan siinä profiilissa, jossa kori syntyi. Toisessa profiilissa näkyvä oma keskeneräinen varaus ei tuo sen ostoskoria tai kuittia tähän profiiliin eikä oikeuta lisäämään varausta uudelleen.

`uhkone istuntotesti` avaa kaksi erillistä profiilia ja vertaa jo olemassa olevia koreja lukemalla. Kirjaudu itse molempiin samalla asiakastilillä. Komento ei lisää tuotteita, varaa, peru tai maksa. Kaksi tyhjää koria jättää tuloksen avoimeksi. Älä julkaise testin mahdollista raporttia.

## Kokeile paikallisessa testikaupassa

```sh
uhkone koeajo
uhkone koeajo --ilman-maksua
uhkone koeajo --monta 4 --maksutapa fastpay --keskustele
uhkone koeajo --monta 4 --maksutapa fastpay --kysy-toiminnot
uhkone koeajo --monta 4 --skenaario maksu-vastaus-katoaa --maksutapa fastpay
uhkone koeajo --monta 4 --skenaario jono --jono --aikaraja 5
```

Koeajo käynnistää oman HTTP-palvelimen osoitteeseen `127.0.0.1`, avaa erillisen testiselaimen ja käyttää kuvitteellista osallistujaa, saldoa ja maksukorttia. Ohjelma suorittaa saman varauskoodin oikeita paikallisia HTTP-pyyntöjä ja SQLite-tietokantaa vasten. `--headless` sopii paikalliseen automaattiseen testaukseen; oikean kaupan varaus käyttää näkyvää selainta.

`uhkone testikauppa` jättää simulaattorin käyntiin käsin selailtavaksi. Muita skenaarioita ovat `ruuhka`, `varaus-vastaus-katoaa`, `viimeinen-paikka`, `taynna`, `fastpay-hylatty`, `fastpay-kortti-puuttuu`, `ensisijainen-taynna`, `jono-vastaus-katoaa` ja `jono-taynna`. Koeajon `--raportti` sekä testikaupan `--tietokanta` on tarkoitettu vain paikalliseen käyttöön: anna niille polku tämän julkisen lähdekansion ulkopuolelta.

Mukana on **139 julkisesta kurssirajapinnasta 18.10.2026 talteen otettua kurssia**. `uhkone/data/courses.json` sisältää lähdeosoitteen, talteenottoajan ja tarkistussumman. Aineistoon sallitaan vain kurssien julkiset kentät: esimerkiksi kurssitunnukset, nimet, hallit, opetuspäivät, kellonajat, hinnat ja paikkamäärät. Ohjaajien nimet, asiakastiedot ja osallistujat eivät kuulu aineistoon. `uhkone paivita-testidata` päivittää sen kiinteällä julkisella lukupyynnöllä.

Simulaattorin paikkamäärät ja avautumisajat muutetaan tarkoituksella paikallisiksi. Testihenkilöt ja kuitit ovat kuvitteellisia. Testitila hyväksyy vain oman paikallisen palvelimen eikä päästä oikeaan kauppaan tai oikeaan maksupalveluun.

**Simulaattori on tämän projektin oma toteutus.** Se ei ole Enkoran lisensoitu palvelinohjelmisto tai itse asennettu Nexus-kauppa. Urheiluhallien käyttämän alustan osoittavat [kaupan Nexus-sivu](https://kauppa.urheiluhallit.fi/nexus/) ja [Enkoran asiakasreferenssi](https://enkora.fi/fi/referenssit/urheiluhallit-oy/). Julkista itse asennettavaa Enkora-jakelua ei löytynyt. Toimittajan mahdollinen testiympäristö olisi selvitettävä erikseen.

Testikaupan kurssi-, osallistuja-, varaustila- ja korinäkymät käyttävät oikean kaupan julkisesta HTML:stä poimittuja, puhdistettuja malleja. Kahdeksan lähdemallin tunnisteet ja tarkistussummat ovat tiedostossa `uhkone/testshop_templates.html`. Poimija säilyttää valittujen elementtien hierarkian, poistaa muuttuvat arvot ja korvaa ne paikallisilla tietopaikoilla. Kurssilisäys avaa korin, kurssilla näkyy osallistujan **Ei vahvistettu** -rivi, ja **Poista** vapauttaa testipaikan. Maksutavat ja saldo päivittyvät erillisinä vastauksina kuten kaupan selainkoodissa.

Kurssivarausten ja ostoskorin JSON-rakenne perustuu lisäksi 22.11.2026 kirjautuneessa kaupassa talteen otettuun maksuttomaan kokeeseen. Kurssivastaukseen ei lisätä keksittyä `is_paid`- tai kuittiviitettä. Simuloitujen maksujen todistusaineisto välitetään erillisellä, vain paikallisessa testitilassa hyväksyttävällä lukurajapinnalla. Se ei kuvaa tuotannon maksusopimusta. Simulaattori käyttää edelleen omia pysyviä koririvitunnisteitaan; oikeassa yhden rivin näytteessä `item_id` oli 0, ja sen lukeminen katetaan erikseen regressiolla.

Tyylit ja palvelin ovat omia paikallisia toteutuksia. Kirjautuminen, henkilötiedot ja maksut ovat kuvitteellisia; kaupan JavaScriptiä, kuvia, fontteja tai muita ulkoisia resursseja ei ladata. CSP rajaa myös käsin avatun testikaupan resurssit paikalliseen palvelimeen. Testikaupan läpäisy ei yksin todista oikean kirjautuneen rajapinnan vastausten tai maksupalvelun vastaavuutta. Myös `koeajo --monta 1` käyttää usean vaihtoehdon selainpohjaa; erillinen Chromium-smoke tarkistaa lisäksi yhden kurssin selainpohjan, näkyvän maksamattoman koririvin ja osallistujan varaustilan.

Mallit voi poimia uudelleen anonyymisti tallennetusta julkisesta sivusta komennolla `python scripts/extract_shop_templates.py /tmp/kauppa-julkinen.html --output uhkone/testshop_templates.html`. Pidä raakatallenne lähdekansion ulkopuolella. Poiminta pysähtyy, jos tarvittava rakenne puuttuu tai muuttuu epäselväksi. Tarkista tulos ja suorita julkisen puun tarkistin ennen julkaisemista.

## Paikalliset tiedot ja julkinen repository

Selainprofiili, kirjautumisistunto ja varausajon etenemistila tallennetaan oletuksena **lähdekansion ulkopuolelle**, hakemistoon `~/.local/state/uhkone` tai `$XDG_STATE_HOME/uhkone`. `--tilahakemisto` vaihtaa paikan. Valitse yksityinen hakemisto tämän repon ulkopuolelta. Profiili sisältää kirjautumisen ja etenemistila voi sisältää osallistujaan sekä varaukseen liittyviä tietoja. Älä jaa niitä.

Tähän julkiseen repoon ei kuulu henkilötietoja, evästeitä, profiileja, asiakastunnisteita, maksutietoja, raakamuotoisia selainkaappauksia, HAR-tiedostoja, raportteja tai lokeja. Sama koskee Git-historiaa sekä commitien tekijä- ja kommenttitietoja. Testien henkilöt ovat kuvitteellisia. Asetustiedosto sisältää vain hakutoiveita; älä lisää siihen nimiä tai tunnuksia.

`.gitignore` sulkee pois tavalliset paikalliset tiedostot. Ennen committia tarkista **koko staged-sisältö**:

```sh
git diff --cached
python scripts/check_public_tree.py
```

Tarkistin lukee Git-indexin tallentamat tiedostot, ei vain työskentelyhakemiston tämänhetkisiä versioita. Se sallii lähdekoodin, ohjeet ja rajatun julkisen kurssidatan sekä etsii tavallisia henkilötieto- ja salaisuusmuotoja. Löydöksissä näytetään vain tiedosto, rivi ja kategoria. Tarkistin on lisäsuoja: **tarkista muutokset myös itse**, koska kaikkia henkilötietoja ei voida päätellä automaattisesti.

Voit ottaa tarkistuksen paikalliseksi Git-hookiksi:

```sh
git config core.hooksPath .githooks
```

Tarkista ennen ensimmäistä committia myös `git var GIT_AUTHOR_IDENT` ja `git var GIT_COMMITTER_IDENT`. Käytä halutessasi GitHubin tarjoamaa yksityisyyttä suojaavaa nimeä ja sähköpostiosoitetta. Älä liitä näiden komentojen tuloksia julkiseen issueen. Henkilötiedon poistaminen myöhemmässä commitissa ei poista sitä historiasta. Katso [SECURITY.md](SECURITY.md).

## Kehitys ja testaus

```sh
python -m pip install -e '.[test]'
python -m playwright install chromium
python -m pytest -q
python scripts/smoke_browser.py
python scripts/check_public_tree.py
```

Testit käyttävät kuvitteellisia osallistujia ja paikallisia kauppanäkymiä. Selainintegraatiot tarvitsevat asennetun Chromiumin, mutta eivät Urheiluhallien kirjautumista tai oikeaa maksupalvelua. Tuotannon tunnuksia ei tarvita CI:ssä. Testit kattavat muun muassa ajastuksen, väärän osallistujan torjunnan, epäselvät varaus- ja maksuvastaukset, yhteisen varausvuoron, pysyvän etenemistilan, jonot ja päätteen ohjaustoiminnot.

`client.py` lukee julkista kurssidataa. `booking.py` ja `multi.py` hallitsevat varaamista ja palautumista, selainliitännät käyttävät kaupan käyttöliittymää ja saman istunnon rajapintoja. `console.py` käsittelee ihmisen ohjauksen. `testshop.py` toteuttaa paikallisen simulaattorin ja `seed.py` rajaa julkaistavan kurssidatan.

Todellinen kirjautuminen, tilikohtaiset osallistujavaatimukset ja maksupalvelun mahdollinen lisätunnistautuminen pitää vielä todentaa omassa työpöytäympäristössä. Tässä kehitysympäristössä ei ole käytetty oikeita asiakastunnuksia, tehty aitoja varauksia tai veloitettu korttia. TLS-varmenteiden tarkistusta ei poisteta käytöstä yhteysongelman korjaamiseksi.

### Tarkista oikean kaupan julkinen sopimus

```sh
uv run --locked python scripts/check_live_shop.py
```

Tämä erillinen tarkistus lukee oikean kaupan julkista HTML:ää, JavaScriptiä ja kurssirajapintaa. Se käyttää nykyistä kurssijäsennintä ja tarkistaa automaation tarvitsemia julkisia rakenteita. Se käyttää vain GET-pyyntöjä eikä kirjaudu, tee varausta tai aloita maksua. Tulosteessa ovat tarkistusten yhteenveto ja lähteiden tarkistussummat; raakavastauksia ei tallenneta.

Julkisen sopimuksen tarkistus ja paikallinen testijoukko ovat erillisiä. Sopimustarkistuksen voi ajaa myös GitHub Actionsin **Live public shop contract** -työnkulusta käsin. Ajoa ei ole ajastettu. CI käyttää paikallisille testeille komentoa `pytest -q --fail-on-skip`: ohitettu testi epäonnistuttaa ajon. CI ajaa lisäksi koko paikallisen Chromium-smoken, joka tarkistaa simuloidun varauksen palvelimen todellisista tapahtumista.

**Julkisen tarkistuksen hyväksytty tulos ei ole päästä päähän tehty varaus- tai maksutesti.** Kirjautuneen tilin osallistujavalinta, varauksen ja kuitin yhteys sekä ulkoisen maksupalvelun paluu on varmennettava erikseen.

Kurssin sopiva taso tarkistetaan [Urheiluhallien leikki-ikäisten uimakoulujen kuvauksesta](https://www.urheiluhallit.fi/palvelut/uimakoulut/leikki-ikaiset/). Ilmoittautuminen tapahtuu [Urheiluhallien omassa verkkokaupassa](https://kauppa.urheiluhallit.fi/webshop/).
