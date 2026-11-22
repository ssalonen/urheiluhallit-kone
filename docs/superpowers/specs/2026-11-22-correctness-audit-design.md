# Oikean kaupan yhteensopivuus ja toiminnan oikeellisuus

## Tavoite

Käyttäjä pyysi laadunvarmistusta, koska paikallinen simulaattori näyttää aivan erilaiselta kuin oikea kauppa. Tarkastus erottaa toisistaan ohjelman sisäisen toimintalogiikan, simuloidun selainpolun, julkisesti todennettavan tuotantoyhteensopivuuden ja kirjautumista vaativan varaus- ja maksuketjun.

Lähtökohta on `main`-haaran commit `e22710c`. Tarkastuksessa luetaan oikean kaupan julkinen HTML, JavaScript ja kurssirajapinta. Oikeaa varausta, jonoon liittymistä tai maksua ei tehdä.

## Ratkaisutapa

Pelkkä simulaattorin ulkoasun muuttaminen ei todista rajapintojen toimintaa. Koko kaupan kopiointi kasvattaisi ylläpidettävää toteutusta eikä tuottaisi riippumatonta palvelintodistetta. Tässä tarkastuksessa valittu tekninen ratkaisu on korjata toistetut virheet, lisätä niitä vastaavat regressiotestit ja tuoda erillinen julkisen tuotantosopimuksen tarkistus simulaattoritestien rinnalle. Tämä on tarkastuksen toteuttajan ratkaisu käyttäjän antaman laadunvarmistustehtävän puitteissa.

## Vaadittu toiminta

- Oikean kaupan suhteelliset maksun paluuosoitteet hyväksytään vain, kun niiden ratkaistu kohde pysyy oikeassa kaupassa. Vieraat alkuperät ja väärät paluureitit torjutaan.
- Ennen lähetystä epäonnistunut lukupyyntö erotetaan mahdollisesti lähetetystä varauksesta tai maksusta. Vain varmasti lähettämätöntä toimintoa saa yrittää uudelleen.
- Olemassa oleva keskeneräinen varaus estää uuden lisäyspyynnön myös juuri ennen toimintoa tehtävässä tarkistuksessa.
- Varaus-, jono- ja maksupyyntöä ei lähetetä määräajan jälkeen, vaikka käyttöliittymän valmistelu olisi vienyt aikaa.
- Kurssin hyväksytty viikoittainen aikataulu ja ostoskorin myyntituote tarkistetaan tunnuksen, osallistujan ja hinnan lisäksi.
- Yhden vielä valitsemattoman vaihtoehdon liian korkea hinta ei estä muiden sallittujen vaihtoehtojen seurantaa. Keskeneräisen varauksen tai maksun rajoituksia ei höllennetä.
- Tulevaisuudessa avautuva vaihtoehto ei hidasta jo avoimen vaihtoehdon normaalia seurantaa.
- Uuden maksun onnistuminen vaatii saman varauksen ja siihen liittyvän kuitin. Pelkkä uusi samanhintainen kuitti ei riitä. Puuttuva maksutieto ei todista maksua. Ilman maksupyyntöä löytynyt vahvistettu varaus ilmoitetaan erikseen jo vahvistettuna, ilman väitettä tämän ajon maksusta.
- Katselmoinnissa tarkennettu: lähetys saa käyttää vain ohjaimen hyväksymää varaustunnusta ja tarkkaa summaa. Tuntematon maksutieto ei osoita maksamattomuutta. Myöhäiset varaushavainnot säilytetään ennen uudelleenyritystä tai kurssin vaihtamista. Kirjoitusaie erotetaan havaitusta lähetyksestä.
- Julkinen lähde ei osoita kurssivarauksen `receipt_event_id`-kentän ja istunnon `receipt_id`-kentän yhteistä tunnistejoukkoa. Simulaattorin numeerinen yhtäläisyys on vain testisopimus. Oikean kaupan uuden maksun todistetta ei saa muodostaa pelkästä todentamattomasta numeroyhtäläisyydestä.
- Fallback-jonotuksen olemassa oleva rajattu lisäaika välitetään myös verkkovartijalle. Määräaika ei saa siirtyä eteenpäin pelkän uudelleenkäynnistyksen vuoksi.
- CI epäonnistuu, jos vaadittuja testejä ohitetaan. Paikallisen ympäristön selainrajoitukset näkyvät raportissa.
- Julkinen tuotantotarkistus käyttää vain rajattuja GET-pyyntöjä oikeaan alkuperään, ei kirjautumista tai yksityisiä tietoja. Sen läpäisy ei ole kirjautuneen varaus- tai maksuketjun hyväksyntä.

## Rajat

Python 3.11 tai uudempi; nykyiset riippuvuudet. Käyttöliittymän tekstit ja raportti suomeksi. Julkiseen repoon ei tallenneta raakoja tuotantovastauksia, henkilötietoja, selainprofiileja tai kuvakaappauksia. Julkaistava näyttö sisältää vain tarkistusten tulokset ja lähteiden tarkistussummat. Nykyinen epäselvän maksun uusimisen esto säilyy. Muutokset tehdään erillisessä korjaushaarassa.

## Hyväksymisnäyttö

Jokainen korjattu virhe toistetaan ennen korjausta. Sen jälkeen ajetaan koko paikallinen testijoukko, Ruff, ty, julkisen Git-puun tarkistus ja uusi julkinen tuotantotarkistus. Riippumaton katselmointi arvioi muutokset. Raportissa kerrotaan erikseen läpäisseet, ohitetut ja edelleen todentamatta olevat vaiheet.
