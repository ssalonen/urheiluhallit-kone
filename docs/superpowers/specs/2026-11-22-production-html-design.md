# Oikean kaupan HTML-rakenteet paikallisessa testikaupassa

## Tavoite

Käyttäjä pyysi oikean kaupan HTML:ää simulaattorin pohjaksi sekä rakenteellisten ja toiminnallisten erojen tarkastusta. Nykyinen käsin tehty, yksinkertaistettu näkymä ei toista koririvejä, vahvistamatonta varausta eikä ostoskorin automaattista avautumista.

## Ratkaisu

Poimitaan nimetyistä julkisista Enkora-malleista kurssikortin, kurssitietojen, osallistujavalinnan, vahvistusikkunan, varausrivin, ostoskorin ja koririvin rakenteet. Poiminta on toistettava paikallinen muunnos: alkuperäiset JavaScript-lausekkeet korvataan nimetyillä tietopaikoilla, ehtojen tarpeettomat haarat poistetaan ja jäljelle jäänyt DOM sekä lähdemallien tiivisteet tarkistetaan. Julkista raakavastausta ei lisätä Gitiin. Mallien rakenne tulee lähteestä; tyylit ja testipalvelimen toiminta ovat paikallisia.

Paikallinen JavaScript täydentää mallit julkisella kurssidatalla ja kuvitteellisilla tileillä. Kurssin ostaminen avaa osallistujavalinnan, vahvistus lisää yhden paikan koriin, kori avautuu ja kurssirivillä näkyy Ei vahvistettu. Korista poistaminen poistaa vain kyseisen paikallisen istunnon maksamattoman varauksen ja vapauttaa paikan. Maksutavat ja saldo voivat valmistua eri aikaan ja renderöidä korin uudelleen, kuten julkisessa asiakaskoodissa.

## Rajat

- Python 3.11+; ei uusia ajonaikaisia riippuvuuksia.
- Kaikki pyynnöt kohdistuvat testipalvelimen tarkkaan loopback-osoitteeseen.
- Kiinteät CSS-, JavaScript- ja malliresurssit sijaitsevat polun /webshop/resource/testshop/ alla.
- CSP sallii vain paikalliset resurssit; sivulla ei suoriteta kaupan JavaScriptiä.
- Testeissä käytetään vain kuvitteellisia osallistujia.
- Ei oikeaa varausta eikä maksua tässä työvaiheessa.
- Oikean kaupan maksutilan ja kuittikytkennän todistusvaatimuksia ei heikennetä.
- Tuotannosta poikkeavat rajaukset, lähde ja todennetut erot kirjataan raporttiin. Ulkoasua ei kutsuta täydelliseksi kopioksi.

## Hyväksyntä

Poiminnan testit osoittavat, että valittu lähde-HTML vaikuttaa oikeasti lopputulokseen ja odottamaton rakenne hylätään. HTTP-testit todentavat resurssit, CSP:n ja rajatun poiston. Chromium-testit todentavat näkyvän kurssin, osallistujan, koririvin, maksamattoman tilan, poiston ja paikan vapautumisen sekä nolla ulkoista pyyntöä. Olemassa olevat varaus- ja jatkamistestit säilyvät. Paikallisen Chromiumin ympäristöesto kirjataan; täydellinen selaintodennus tehdään CI:ssä.
